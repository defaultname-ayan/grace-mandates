"""Rules-only baseline (spec 14.2) -- the honest comparator.

This is what a competent engineer writes in an afternoon after reading the
Razorpay docs. It is not a strawman: it knows the rail/status matrix and falls
back sensibly when its first choice is not permitted.

What it deliberately does NOT know, because a rules engine plausibly would not:
  * eMandate in-flight state (so it will try to charge into the double-debit
    window; the integrity guard is what stops it);
  * the card-reissue remap trap, so it churns long-tenured customers on a
    'card_expired' that would have resolved itself;
  * the difference between timing and structural liquidity beyond one threshold.

If this baseline beats the agent, the README says so and this ships as the
default. That sentence is worth more than a fabricated lift.
"""
from __future__ import annotations

from datetime import date

from grace.adjudicate.schema import Decision
from grace.models import Action, Cause, Evidence, Rail, SubStatus
from grace.signals.holidays import HolidayCalendar, default_calendar
from grace.signals.salary_cycle import suggested_resume_date
from grace.sim.vocab import reason_family

TEMPORARY_KEYS = ("travel", "skip", "month", "pause", "later", "salary", "hold", "break")
PRICE_KEYS = ("expensive", "price", "cost", "cheaper", "afford", "discount", "downgrade")


class RulesBaseline:
    name = "rules_baseline"

    def __init__(self, today: date | None = None, calendar: HolidayCalendar | None = None):
        self.today = today or date.today()
        self.cal = calendar or default_calendar()

    def _first_allowed(self, ev: Evidence, *candidates: Action) -> Action:
        allowed = set(ev.allowed_actions)
        for a in candidates:
            if a in allowed:
                return a
        return Action.NOOP

    def decide(self, ev: Evidence) -> Decision:
        m = ev.mandate
        allowed = set(ev.allowed_actions)
        d2s = ev.days_to_salary
        fam = reason_family(m.last_error_reason)

        def out(cause: Cause, action: Action, conf: float = 0.7, **kw) -> Decision:
            kw.setdefault("rationale", f"rules baseline: cause={cause.value}, action={action.value}")
            return Decision(cause=cause, cause_confidence=conf, action=action,
                            action_confidence=conf, evidence_used=["rules_baseline"], **kw).clamped()

        # cancel intent
        if ev.cancel_intent_text:
            t = ev.cancel_intent_text.lower()
            if any(k in t for k in TEMPORARY_KEYS):
                act = self._first_allowed(ev, Action.PAUSE, Action.NOOP)
                return out(Cause.CUSTOMER_INTENT_TEMPORARY, act, 0.7, pause_cycles=1,
                           resume_on=suggested_resume_date(
                               self.today, ev.customer.salary_day, m.cycle_day, self.cal).isoformat())
            if any(k in t for k in PRICE_KEYS) and m.rail == Rail.CARD:
                return out(Cause.CUSTOMER_INTENT_PRICE,
                           self._first_allowed(ev, Action.STEP_DOWN_PLAN, Action.NOOP), 0.7,
                           step_down_target_plan_id="plan_basic")
            return out(Cause.CUSTOMER_INTENT_DONE,
                       self._first_allowed(ev, Action.CANCEL_AT_CYCLE_END, Action.NOOP), 0.75)

        # live failure
        if m.status in (SubStatus.PENDING, SubStatus.HALTED) or m.last_error_reason:
            if fam == "liquidity" and d2s is not None and d2s <= 6:
                # Note: no in-flight check here. That gap is the point.
                act = self._first_allowed(ev, Action.PAUSE, Action.MANUAL_CHARGE, Action.NOOP)
                return out(Cause.LIQUIDITY_TIMING, act, 0.7, pause_cycles=1,
                           resume_on=suggested_resume_date(
                               self.today, ev.customer.salary_day, m.cycle_day, self.cal).isoformat())
            if fam == "technical":
                return out(Cause.BANK_TECHNICAL, Action.NOOP, 0.7)
            if fam == "instrument":
                return out(Cause.INSTRUMENT_INVALID,
                           self._first_allowed(ev, Action.REQUEST_REAUTH, Action.NOOP), 0.75,
                           escalate=Action.REQUEST_REAUTH in allowed,
                           escalate_reason="re-auth needs the customer")
            if fam == "liquidity":
                act = self._first_allowed(ev, Action.MANUAL_CHARGE, Action.NOOP)
                return out(Cause.LIQUIDITY_STRUCTURAL, act, 0.68)
            # The natural naive rule: the payment failed, so charge it again.
            # This is the behaviour that produces double debits on eMandate,
            # because nothing here looks at whether an attempt is still in
            # flight. The integrity guard is what stops it.
            act = self._first_allowed(ev, Action.MANUAL_CHARGE, Action.NOOP)
            return out(Cause.UNKNOWN, act, 0.66)

        return out(Cause.UNKNOWN, Action.NOOP, 0.7)
