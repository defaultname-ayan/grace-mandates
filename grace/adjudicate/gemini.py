"""Gemini adjudicator (default provider).

Surface verified against google-genai 2.22.0 by introspection, and against
ai.google.dev/gemini-api/docs/gemini-3:

  * reasoning depth is `thinking_level` (minimal|low|medium|high). The legacy
    `thinking_budget` still works but sending BOTH is a 400, so this only ever
    sends `thinking_level`.
  * temperature is deliberately NOT set. Google's Gemini 3 guidance is to keep
    it at the default 1.0; lowering it can cause looping and degrades reasoning
    on hard tasks. Determinism comes from the policy layer, not from sampling.
  * structured output is `response_mime_type="application/json"` plus
    `response_schema=Decision`; the parsed object arrives on `response.parsed`.
  * there is no Anthropic-style `cache_control` breakpoint. Gemini caches long
    repeated prefixes implicitly, so the static system prompt still benefits,
    but the saving is not something this code can assert. `cached_content_token_count`
    is reported so it can be observed rather than assumed.

A refusal is not an exception here: it comes back as a finish_reason or a
prompt block, with an empty candidate. Missing that check would look like a
malformed response instead of a safety decision.
"""
from __future__ import annotations

import os
import time

from grace.adjudicate.prompt import SYSTEM, format_evidence
from grace.adjudicate.schema import Decision
from grace.models import Evidence

DEFAULT_MODEL = "gemini-3.8-flash"

#: Ordered fallback chain. The free tier returns `503 UNAVAILABLE - this model
#: is currently experiencing high demand` unpredictably, and no amount of
#: backoff fixes an overloaded model. Rather than fail the mandate (which would
#: escalate it and quietly depress the measured result), Grace re-tries the same
#: evidence on the next model down. Every decision records which model actually
#: served it, and the batch summary reports the distribution -- a run served
#: mostly by the last model in the chain is a different experiment, and the
#: report must not hide that.
#: The lite models sit at the end deliberately: on the free tier the flash
#: models share a daily quota that a single full batch exhausts, after which
#: they return 429 RESOURCE_EXHAUSTED while the lite models still serve. A run
#: that falls through to lite is a weaker experiment, which is exactly why
#: `served_by` is reported rather than assumed.
DEFAULT_FALLBACK_CHAIN = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

#: Grace's effort vocabulary -> Gemini's thinking_level.
EFFORT_TO_THINKING_LEVEL = {
    "minimal": "minimal", "low": "low", "medium": "medium",
    "high": "high", "xhigh": "high", "max": "high",
}

#: `thinking_level` is a Gemini 3 parameter. The 2.x family rejects it with
#: `400 INVALID_ARGUMENT: Thinking level is not supported`, so it must only be
#: sent to models that understand it. Verified live against gemini-2.5-flash.
def supports_thinking_level(model: str) -> bool:
    m = (model or "").lower().removeprefix("models/")
    if not m.startswith("gemini-"):
        return False
    ver = m[len("gemini-"):].split("-", 1)[0]
    try:
        return float(ver) >= 3.0
    except ValueError:
        return False


#: finish_reason values that mean the model declined, not that it failed.
REFUSAL_FINISH_REASONS = {
    "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY",
}


class AdjudicationError(RuntimeError):
    pass


class GeminiRefusal(AdjudicationError):
    """The model declined. Distinct from a transport failure: never retried."""


def _name(x) -> str:
    """Enum-or-string -> plain uppercase name."""
    if x is None:
        return ""
    return str(getattr(x, "name", x)).upper()


class GeminiAdjudicator:
    name = "gemini"

    def __init__(self, model: str | None = None, effort: str | None = None,
                 max_retries: int = 2, max_output_tokens: int = 4000,
                 api_key: str | None = None):
        from google import genai  # lazy: the offline path needs no SDK

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
        self.model = model or os.getenv("GRACE_MODEL") or DEFAULT_MODEL
        chain_env = os.getenv("GRACE_MODEL_FALLBACKS")
        chain = ([m.strip() for m in chain_env.split(",") if m.strip()]
                 if chain_env else list(DEFAULT_FALLBACK_CHAIN))
        # The configured model always goes first; the rest follow, de-duplicated.
        self.model_chain = [self.model] + [m for m in chain if m != self.model]
        eff = (effort or os.getenv("GRACE_EFFORT", "high")).lower()
        self.thinking_level = EFFORT_TO_THINKING_LEVEL.get(eff, "high")
        self.effort = eff
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self.metas: list[dict] = []
        self._meta_lock = __import__("threading").Lock()

    def _config(self, model: str | None = None):
        from google.genai import types

        model = model or self.model
        kwargs = dict(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=Decision,
            max_output_tokens=self.max_output_tokens,
            # temperature intentionally unset -- see module docstring.
        )
        if supports_thinking_level(model):
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=self.thinking_level)
        return types.GenerateContentConfig(**kwargs)

    def _backoff(self, attempt: int) -> float:
        """Jittered exponential backoff, deliberately short.

        The fallback chain is the real redundancy here: a 503 means this model
        is overloaded, and waiting rarely fixes that, whereas the next model
        usually answers immediately. Long per-model backoff just multiplies
        4 models x N retries into minutes of dead time.
        """
        import random

        return min(1.5 * (2**attempt), 10.0) * (0.6 + 0.8 * random.random())

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

    def decide(self, ev: Evidence) -> Decision:
        d, meta = self.adjudicate(ev)
        with self._meta_lock:
            self.metas.append(meta)
        return d

    def adjudicate(self, ev: Evidence) -> tuple[Decision, dict]:
        """Try each model in the chain; within a model, retry transient faults."""
        last_err: Exception | None = None
        for depth, model in enumerate(self.model_chain):
            try:
                return self._call_one(ev, model, depth)
            except GeminiRefusal:
                raise  # a decision, not a fault: do not shop it to another model
            except AdjudicationError as e:
                last_err = e
                continue
        raise AdjudicationError(
            f"all {len(self.model_chain)} models in the chain failed; last: {last_err}"
        )

    def _call_one(self, ev: Evidence, model: str, depth: int) -> tuple[Decision, dict]:
        errors = self._errors
        user = "Evidence for one mandate follows. Decide.\n\n" + format_evidence(ev)
        last_err: Exception | None = None

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
                last_err = e  # 503 "high demand" is the common one on free tier
                time.sleep(self._backoff(attempt))
            except errors.ClientError as e:
                last_err = e
                if getattr(e, "code", None) == 429:
                    time.sleep(self._backoff(attempt))
                else:
                    raise AdjudicationError(f"{getattr(e, 'code', '?')}: {e}") from e
            except AdjudicationError:
                raise
            except Exception as e:  # transport/DNS/timeout
                last_err = e
                time.sleep(self._backoff(attempt))

        raise AdjudicationError(f"{model}: exhausted retries: {last_err}")
