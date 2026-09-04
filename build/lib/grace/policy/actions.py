"""Rail x status action matrix (spec 9.1).

This encodes Razorpay's documented capability matrix. It is the reason the
product exists and it is enforced here, in code, never in the prompt: the model
is told the rules, but the model cannot break them.
"""
from __future__ import annotations

from grace.models import Action, Rail, SubStatus


def allowed_actions(
    rail: Rail,
    status: SubStatus,
    *,
    has_pending_invoice: bool = False,
    pause_initiated_by: str | None = None,
) -> list[Action]:
    A: set[Action] = {Action.NOOP, Action.ESCALATE}

    if status == SubStatus.ACTIVE:
        A |= {Action.PAUSE, Action.CANCEL_AT_CYCLE_END}
        if rail == Rail.CARD:
            A |= {Action.STEP_DOWN_PLAN}

    if status == SubStatus.AUTHENTICATED:
        # Never PAUSE here: Razorpay cancels an authenticated subscription
        # permanently if you pause it. Only cards can shift the start date.
        if rail == Rail.CARD:
            A |= {Action.SHIFT_START, Action.STEP_DOWN_PLAN}
        A |= {Action.CANCEL_AT_CYCLE_END}

    if status in (SubStatus.PENDING, SubStatus.HALTED):
        if has_pending_invoice:
            A |= {Action.MANUAL_CHARGE}
        A |= {Action.REQUEST_REAUTH}
        if status == SubStatus.PENDING and rail != Rail.EMANDATE:
            A |= {Action.CANCEL_AT_CYCLE_END}

    if status == SubStatus.PAUSED:
        # A customer-paused UPI mandate can only be resumed by the customer.
        if not (rail == Rail.UPI_AUTOPAY and pause_initiated_by == "customer"):
            A |= {Action.RESUME}

    return sorted(A, key=lambda a: a.value)
