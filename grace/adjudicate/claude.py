"""The Claude adjudicator (spec 8.4). Alternate provider; Gemini is the default."""
from __future__ import annotations

import time

from grace.adjudicate.base import (
    AdjudicationError,
    LLMAdjudicator,
    backoff,
    user_turn,
)
from grace.adjudicate.prompt import SYSTEM
from grace.adjudicate.schema import Decision
from grace.config import CONFIG
from grace.models import Evidence

DEFAULT_MODEL = "claude-opus-5"


class ClaudeAdjudicator(LLMAdjudicator):
    name = "claude"

    def __init__(self, model: str | None = None, effort: str | None = None,
                 max_retries: int = 2, max_tokens: int = 4000):
        import anthropic  # imported lazily so the offline path needs no SDK

        super().__init__()
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        # `or`, not getenv-with-default: an exported-but-empty GRACE_MODEL must
        # still resolve to the provider default rather than to "".
        self.model = model or CONFIG.model or DEFAULT_MODEL
        self.effort = effort or CONFIG.effort
        self.max_retries = max_retries
        self.max_tokens = max_tokens

    def adjudicate(self, ev: Evidence) -> tuple[Decision, dict]:
        anthropic = self._anthropic
        user = user_turn(ev)
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
                    "model": self.model, "requested_model": self.model, "fallback_depth": 0,
                    "effort": self.effort, "attempt": attempt, "adjudicator": self.name,
                }
                return parsed.clamped(), meta
            except anthropic.RateLimitError as e:
                last_err = e
                time.sleep(backoff(attempt))
            except anthropic.APIStatusError as e:
                last_err = e
                if e.status_code >= 500:
                    time.sleep(backoff(attempt))
                else:
                    raise AdjudicationError(f"{e.status_code}: {e}") from e
            except anthropic.APIConnectionError as e:
                last_err = e
                time.sleep(backoff(attempt))
        raise AdjudicationError(f"exhausted retries: {last_err}", transient=True)
