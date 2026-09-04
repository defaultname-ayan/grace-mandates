"""Shared adjudicator plumbing.

Both providers used to carry their own copy of the exception type, the user-turn
prefix, the backoff and the decide()/metas wrapper -- including two unrelated
classes both called AdjudicationError, so no caller could catch one name for
both. Everything provider-neutral lives here.
"""
from __future__ import annotations

import random
import threading

from grace.adjudicate.prompt import format_evidence
from grace.adjudicate.schema import Decision
from grace.models import Action, Cause, Evidence

USER_PREFIX = "Evidence for one mandate follows. Decide.\n\n"


class AdjudicationError(RuntimeError):
    """The adjudicator could not produce a decision. Never an excuse to act.

    `transient` marks faults that are about the model's availability (429,
    503, transport), as opposed to this specific request (400, unparsable
    output). Only transient faults put a model into cooldown.
    """

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def user_turn(ev: Evidence) -> str:
    return USER_PREFIX + format_evidence(ev)


def backoff(attempt: int, base: float = 1.5, cap: float = 10.0) -> float:
    """Jittered exponential backoff, deliberately short.

    A model fallback chain is the real redundancy: an overloaded model rarely
    recovers within seconds, whereas the next model usually answers at once.
    Long per-model backoff just multiplies models x retries into dead time.
    """
    return min(base * (2**attempt), cap) * (0.6 + 0.8 * random.random())


def safe_default(reason: str) -> Decision:
    """Graceful fallback when the model is unavailable: never act, always escalate."""
    return Decision(
        cause=Cause.UNKNOWN, cause_confidence=0.0,
        action=Action.ESCALATE, action_confidence=0.0,
        rationale=f"adjudicator unavailable: {reason}",
        evidence_used=[], escalate=True, escalate_reason=reason,
    )


class LLMAdjudicator:
    """Base for provider adjudicators. Subclasses implement `adjudicate`."""

    name = "llm"
    model: str
    effort: str

    def __init__(self) -> None:
        self.metas: list[dict] = []
        self._meta_lock = threading.Lock()

    def adjudicate(self, ev: Evidence) -> tuple[Decision, dict]:
        raise NotImplementedError

    def decide(self, ev: Evidence) -> Decision:
        d, meta = self.adjudicate(ev)
        with self._meta_lock:
            self.metas.append(meta)
        return d
