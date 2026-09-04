"""Assemble the evidence bundle handed to the adjudicator.

Ground truth is never read here. The only way truth reaches a decision is
through the simulated event stream, exactly as production evidence would.
"""
from __future__ import annotations

from datetime import date

from grace.models import Evidence, Mandate, SubStatus
from grace.policy.actions import allowed_actions
from grace.signals.bank_health import BankHealth
from grace.signals.holidays import HolidayCalendar
from grace.signals.salary_cycle import days_to_salary, infer_salary_day
from grace.store import Store


def build_evidence(
    store: Store,
    m: Mandate,
    *,
    bank_health: BankHealth,
    calendar: HolidayCalendar,
    today: date,
    p_fail: float = 0.0,
    cancel_intent_text: str | None = None,
    recent_n: int = 8,
) -> Evidence:
    cust = store.get_customer(m.customer_id)
    if cust is None:  # pragma: no cover - cohort always writes the customer
        raise KeyError(f"customer {m.customer_id} missing for mandate {m.id}")

    all_events = store.events_for(m.id)
    events = all_events[-recent_n:]

    # Derived from the event stream, exactly as a merchant could derive them.
    # Per CYCLE, not per event: the retry ladder emits one pending event per
    # attempt, so counting events inflated a single current failure with two
    # retries into "failed twice before" -- which is exactly the signal that
    # decides whether a card_expired is a reissue remap or a dead card.
    open_inv = store.open_invoice(m.id)
    fail_names = {"subscription.pending", "subscription.halted"}
    outcome: dict[str, str] = {}  # invoice_id -> "failed" | "charged", first-seen order
    for e in all_events:
        inv_id = e.invoice_id
        if not inv_id or (open_inv and inv_id == open_inv.id):
            continue  # the cycle under decision is not "prior"
        if e.name == "subscription.charged":
            outcome[inv_id] = "charged"
        elif e.name in fail_names:
            outcome.setdefault(inv_id, "failed")
    prior_fail_count = sum(1 for v in outcome.values() if v == "failed")
    streak = 0
    for v in reversed(list(outcome.values())):
        if v == "failed":
            streak += 1
        else:
            break

    salary_day, inferred = cust.salary_day, False
    if salary_day is None:
        successes = [e.at.date() for e in all_events if e.name == "subscription.charged"]
        salary_day = infer_salary_day(successes)
        inferred = salary_day is not None

    charge_day = m.charge_at.date() if m.charge_at else today

    return Evidence(
        mandate=m,
        customer=cust,
        recent_events=events,
        bank_health=bank_health.get(cust.bank),
        days_to_salary=days_to_salary(today, salary_day),
        is_bank_holiday_on_charge_day=calendar.is_bank_holiday(charge_day),
        in_downtime=bank_health.is_in_downtime(cust.bank, m.charge_at) if m.charge_at else False,
        cancel_intent_text=cancel_intent_text,
        allowed_actions=allowed_actions(
            m.rail, m.status,
            has_pending_invoice=open_inv is not None,
            pause_initiated_by=m.pause_initiated_by,
        ),
        p_fail=p_fail,
        salary_day_inferred=inferred,
        has_pending_invoice=open_inv is not None,
        prior_fail_count_6m=prior_fail_count,
        prior_fail_streak=streak,
        emandate_attempt_in_flight=bool(open_inv and open_inv.attempt_in_flight),
    )


def is_decision_trigger(ev: Evidence, theta_low: float) -> tuple[bool, str]:
    """Should this mandate reach the adjudicator at all? (spec 3 decision loop)"""
    if ev.cancel_intent_text:
        return True, "intent"
    if ev.mandate.status in (SubStatus.PENDING, SubStatus.HALTED):
        return True, "failure"
    if ev.p_fail >= theta_low:
        return True, "predicted"
    return False, "tick"
