"""Gemini adjudicator (default provider).

Surface verified against google-genai 2.22.0 by introspection and against
ai.google.dev/gemini-api/docs/gemini-3, then against the live API:

  * reasoning depth is `thinking_level` (minimal|low|medium|high) on Gemini 3.
    The 2.x family REJECTS it with `400 Thinking level is not supported` (seen
    live on gemini-2.5-flash), and sending both it and the legacy
    `thinking_budget` is a 400 on Gemini 3. So it is sent only to models that
    understand it, and never together with a budget.
  * temperature is deliberately NOT set. Google's Gemini 3 guidance is to keep
    it at the default 1.0; lowering it can cause looping and degrades reasoning.
    Determinism comes from the policy layer, not from sampling.
  * structured output is `response_mime_type="application/json"` plus
    `response_schema=Decision`; the parsed object arrives on `response.parsed`.
  * there is no Anthropic-style cache breakpoint; `cached_content_token_count`
    is reported so implicit caching can be observed rather than assumed.

A refusal is not an exception here: it comes back as a finish_reason or a
prompt block with an empty candidate. Missing that check would look like a
malformed response instead of a safety decision.
"""
from __future__ import annotations

import os
import time
from typing import Any

from grace.adjudicate.base import AdjudicationError, LLMAdjudicator, backoff, user_turn
from grace.adjudicate.prompt import SYSTEM
from grace.adjudicate.schema import Decision
from grace.config import CONFIG
from grace.models import Evidence

DEFAULT_MODEL = "gemini-3.8-flash"

__all__ = ["GeminiAdjudicator", "GeminiRefusal", "AdjudicationError", "DEFAULT_MODEL",
           "DEFAULT_FALLBACK_CHAIN", "supports_thinking_level"]

#: Ordered fallback chain. The free tier returns `503 UNAVAILABLE - high demand`
#: unpredictably, and after roughly one batch of traffic the whole flash tier
#: returns `429 RESOURCE_EXHAUSTED` (the daily quota is shared); the lite models
#: keep serving. Every decision records which model actually served it and the
#: batch summary reports the distribution: a run served mostly by the tail of
#: the chain is a different experiment and the report must not hide that.
DEFAULT_FALLBACK_CHAIN = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

#: How long a model that exhausted its retries on a transient fault is skipped.
#: A daily quota does not recover in seconds; re-probing it per mandate wasted
#: ~3 calls and ~10s of sleep on every single case.
MODEL_COOLDOWN_SECONDS = 120.0

EFFORT_TO_THINKING_LEVEL = {
    "minimal": "minimal", "low": "low", "medium": "medium",
    "high": "high", "xhigh": "high", "max": "high",
}

REFUSAL_FINISH_REASONS = {
    "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY",
}


def supports_thinking_level(model: str) -> bool:
    """`thinking_level` is Gemini 3+. Verified live: 2.5 returns a 400."""
    m = (model or "").lower().removeprefix("models/")
    if not m.startswith("gemini-"):
        return False
    ver = m[len("gemini-"):].split("-", 1)[0]
    try:
        return float(ver) >= 3.0
    except ValueError:
        return False


class GeminiRefusal(AdjudicationError):
    """The model declined. A decision, not a fault: never retried, never
    shopped to another model."""


def _name(x) -> str:
    if x is None:
        return ""
    return str(getattr(x, "name", x)).upper()


class GeminiAdjudicator(LLMAdjudicator):
    name = "gemini"

    def __init__(self, model: str | None = None, effort: str | None = None,
                 max_retries: int = 2, max_output_tokens: int = 4000,
                 api_key: str | None = None, pin: bool = False):
        """
        `pin=True` means: use exactly `model`, no fallbacks. That is what a
        `--model` flag must mean -- otherwise a failing pinned model is silently
        served by the default chain and "verified" against the wrong thing.
        """
        from google import genai  # lazy: the offline path needs no SDK

        super().__init__()
        self._genai = genai
        from google.genai import errors as genai_errors

        self._errors = genai_errors
        key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError(
                "No Gemini API key. Set GEMINI_API_KEY (or GOOGLE_API_KEY).\n"
                "Create one at https://aistudio.google.com/apikey"
            )
        self.client = genai.Client(api_key=key)
        self.model = model or CONFIG.model or DEFAULT_MODEL
        if pin and model:
            self.model_chain = [self.model]
        else:
            fb = CONFIG.model_fallbacks
            chain = list(fb) if fb is not None else list(DEFAULT_FALLBACK_CHAIN)
            self.model_chain = [self.model] + [m for m in chain if m != self.model]
        eff = (effort or CONFIG.effort).lower()
        self.thinking_level = EFFORT_TO_THINKING_LEVEL.get(eff, "high")
        self.effort = eff
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self._cooldown_until: dict[str, float] = {}

    def _config(self, model: str | None = None):
        from google.genai import types

        model = model or self.model
        kwargs: dict[str, Any] = {
            "system_instruction": SYSTEM,
            "response_mime_type": "application/json",
            "response_schema": Decision,
            "max_output_tokens": self.max_output_tokens,
            # temperature intentionally unset -- see module docstring.
        }
        if supports_thinking_level(model):
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=types.ThinkingLevel(self.thinking_level.upper()))
        return types.GenerateContentConfig(**kwargs)

    def _backoff(self, attempt: int) -> float:
        return backoff(attempt)

    @staticmethod
    def _check_refusal(resp) -> None:
        pf = getattr(resp, "prompt_feedback", None)
        blocked = _name(getattr(pf, "block_reason", None))
        if blocked and blocked != "BLOCKED_REASON_UNSPECIFIED":
            raise GeminiRefusal(f"prompt blocked: {blocked}")
        for cand in (getattr(resp, "candidates", None) or []):
            fr = _name(getattr(cand, "finish_reason", None))
            if fr in REFUSAL_FINISH_REASONS:
                raise GeminiRefusal(f"finish_reason={fr}")
            if fr == "MAX_TOKENS":
                raise AdjudicationError(
                    "response hit max_output_tokens; the JSON is truncated and unparsable"
                )

    def adjudicate(self, ev: Evidence) -> tuple[Decision, dict]:
        """Try each model in the chain; within a model, retry transient faults."""
        last_err: Exception | None = None
        now = time.time()
        for depth, model in enumerate(self.model_chain):
            if self._cooldown_until.get(model, 0.0) > now:
                continue
            try:
                return self._call_one(ev, model, depth)
            except GeminiRefusal:
                raise
            except AdjudicationError as e:
                last_err = e
                if e.transient:
                    self._cooldown_until[model] = time.time() + MODEL_COOLDOWN_SECONDS
                continue
        raise AdjudicationError(
            f"all {len(self.model_chain)} models in the chain failed; last: {last_err}",
            transient=True,
        )

    def _call_one(self, ev: Evidence, model: str, depth: int) -> tuple[Decision, dict]:
        errors = self._errors
        user = user_turn(ev)
        last_err: Exception | None = None
        more_models = depth < len(self.model_chain) - 1

        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                resp = self.client.models.generate_content(
                    model=model, contents=user, config=self._config(model),
                )
                self._check_refusal(resp)
                parsed = getattr(resp, "parsed", None)
                if parsed is None:
                    raise AdjudicationError("model returned no parsable decision")
                if not isinstance(parsed, Decision):
                    parsed = Decision.model_validate(
                        parsed if isinstance(parsed, dict) else parsed.model_dump()
                    )
                u = getattr(resp, "usage_metadata", None)
                meta = {
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "input_tokens": getattr(u, "prompt_token_count", 0) or 0,
                    "output_tokens": getattr(u, "candidates_token_count", 0) or 0,
                    "thinking_tokens": getattr(u, "thoughts_token_count", 0) or 0,
                    "cache_read_input_tokens": getattr(u, "cached_content_token_count", 0) or 0,
                    "request_id": getattr(resp, "response_id", None),
                    "model": model,
                    "requested_model": self.model,
                    "fallback_depth": depth,
                    "effort": self.effort,
                    "thinking_level": self.thinking_level if supports_thinking_level(model) else None,
                    "attempt": attempt, "adjudicator": self.name,
                }
                return parsed.clamped(), meta

            except GeminiRefusal:
                raise
            except errors.ServerError as e:
                last_err = e  # 503 "high demand": overloaded, retry briefly
                time.sleep(self._backoff(attempt))
            except errors.ClientError as e:
                last_err = e
                if getattr(e, "code", None) != 429:
                    raise AdjudicationError(f"{getattr(e, 'code', '?')}: {e}") from e
                if more_models:
                    # A quota does not recover in seconds. Do not sleep on it
                    # when another model can answer now.
                    raise AdjudicationError(f"{model}: 429 quota exhausted", transient=True) from e
                time.sleep(self._backoff(attempt))
            except AdjudicationError:
                raise
            except Exception as e:  # transport/DNS/timeout
                last_err = e
                time.sleep(self._backoff(attempt))

        raise AdjudicationError(f"{model}: exhausted retries: {last_err}", transient=True)
