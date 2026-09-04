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

#: Grace's effort vocabulary -> Gemini's thinking_level.
EFFORT_TO_THINKING_LEVEL = {
    "minimal": "minimal", "low": "low", "medium": "medium",
    "high": "high", "xhigh": "high", "max": "high",
}

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
        self.model = model or os.getenv("GRACE_MODEL", DEFAULT_MODEL)
        eff = (effort or os.getenv("GRACE_EFFORT", "high")).lower()
        self.thinking_level = EFFORT_TO_THINKING_LEVEL.get(eff, "high")
        self.effort = eff
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self.metas: list[dict] = []
        self._meta_lock = __import__("threading").Lock()

    def _config(self):
        from google.genai import types

        return types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=Decision,
            max_output_tokens=self.max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_level=self.thinking_level),
            # temperature intentionally unset -- see module docstring.
        )

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
        errors = self._errors
        user = "Evidence for one mandate follows. Decide.\n\n" + format_evidence(ev)
        last_err: Exception | None = None

        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                resp = self.client.models.generate_content(
                    model=self.model, contents=user, config=self._config(),
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
                    "model": self.model, "effort": self.effort,
                    "thinking_level": self.thinking_level,
                    "attempt": attempt, "adjudicator": self.name,
                }
                return parsed.clamped(), meta

            except GeminiRefusal:
                raise  # a decision, not a fault: never retry it
            except errors.ServerError as e:
                last_err = e
                time.sleep(min(2**attempt, 8))
            except errors.ClientError as e:
                last_err = e
                if getattr(e, "code", None) == 429:
                    time.sleep(min(2**attempt, 8))
                else:
                    raise AdjudicationError(f"{getattr(e, 'code', '?')}: {e}") from e
            except AdjudicationError:
                raise
            except Exception as e:  # transport/DNS/timeout
                last_err = e
                time.sleep(min(2**attempt, 8))

        raise AdjudicationError(f"exhausted retries: {last_err}")
