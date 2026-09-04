"""The Claude adjudicator (spec 8.4)."""
from __future__ import annotations

import os
import time

from grace.adjudicate.prompt import SYSTEM, format_evidence
from grace.adjudicate.schema import Decision
from grace.models import Action, Cause, Evidence


class AdjudicationError(RuntimeError):
    pass


def safe_default(reason: str) -> Decision:
    """Graceful fallback when the model is unavailable: never act, always escalate."""
    return Decision(
        cause=Cause.UNKNOWN, cause_confidence=0.0,
        action=Action.ESCALATE, action_confidence=0.0,
        rationale=f"adjudicator unavailable: {reason}",
        evidence_used=[], escalate=True, escalate_reason=reason,
    )


class ClaudeAdjudicator:
    name = "claude"

    def __init__(self, model: str | None = None, effort: str | None = None,
                 max_retries: int = 2, max_tokens: int = 4000):
        import anthropic  # imported lazily so the offline path needs no SDK

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model or os.getenv("GRACE_MODEL", "claude-opus-5")
        self.effort = effort or os.getenv("GRACE_EFFORT", "high")
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self.metas: list[dict] = []
        self._meta_lock = __import__("threading").Lock()

    def decide(self, ev: Evidence) -> Decision:
        d, meta = self.adjudicate(ev)
        with self._meta_lock:
            self.metas.append(meta)
        return d

    def adjudicate(self, ev: Evidence) -> tuple[Decision, dict]:
        anthropic = self._anthropic
        user = "Evidence for one mandate follows. Decide.\n\n" + format_evidence(ev)
        last_err: Exception | None = None

        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                resp = self.client.messages.parse(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    thinking={"type": "adaptive"},
                    output_config={"effort": self.effort},
                    system=[{"type": "text", "text": SYSTEM,
                             "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": user}],
                    output_format=Decision,
                )
                if getattr(resp, "stop_reason", None) == "refusal":
                    raise AdjudicationError(f"refusal: {getattr(resp, 'stop_details', None)}")
                parsed = resp.parsed_output
                if parsed is None:
                    raise AdjudicationError("model returned no parsable decision")
                meta = {
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                    "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                    "request_id": getattr(resp, "_request_id", None),
                    "model": self.model, "effort": self.effort, "attempt": attempt,
                    "adjudicator": self.name,
                }
                return parsed.clamped(), meta
            except anthropic.RateLimitError as e:
                last_err = e
                time.sleep(min(2**attempt, 8))
            except anthropic.APIStatusError as e:
                last_err = e
                if e.status_code >= 500:
                    time.sleep(min(2**attempt, 8))
                else:
                    raise AdjudicationError(f"{e.status_code}: {e}") from e
            except anthropic.APIConnectionError as e:
                last_err = e
                time.sleep(min(2**attempt, 8))
        raise AdjudicationError(f"exhausted retries: {last_err}")
