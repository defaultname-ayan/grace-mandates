"""Deterministic offline adjudicator.

WHY THIS EXISTS, stated plainly: `grace run-batch --offline` must run with no
network so the pipeline is testable and reproducible. This class encodes the
same decision discipline the system prompt states, so the pipeline is exercised
end to end -- but it is NOT the model, and any report produced in offline mode
labels the agent column `offline_stub`. Numbers from an offline run are a
pipeline check, not a model result.

It is deliberately stronger than the rules baseline in three specific ways the
baseline cannot express, so that comparing them is meaningful:
  1. it detects the card-reissue remap trap (RBI 2026) instead of churning;
  2. it respects eMandate in-flight state instead of double-charging;
  3. it distinguishes timing from structural liquidity using the salary signal.
"""
from __future__ import annotations

from datetime import date

from grace.adjudicate.schema import Decision
from grace.models import Action, Cause, Evidence, Rail, SubStatus
from grace.signals.holidays import HolidayCalendar, default_calendar
from grace.signals.salary_cycle import suggested_resume_date
from grace.sim.vocab import reason_family

#: Intent lexicons. Multi-word entries match as substrings; single words match
#: on word boundaries so "over" does not fire inside "cover" or "overpriced".
TEMPORARY_TOKENS = (
    "travel", "travelling", "abroad", "skip", "pause", "paused", "hold", "later",
    "salary", "temporarily", "temporary", "freeze", "break", "defer", "rok",
    "resume", "leave", "exam", "exams", "shift", "shifting", "wait", "holiday",
    "band kar do next", "next month se", "payment holiday", "come back",
    "for a month", "for 30 days", "for a few weeks", "till i shift", "hold it",
    "this month only", "skip this month", "skip that cycle", "1 month", "2 months",
    "45 days", "6 weeks", "2 cycles", "maternity", "medical leave", "emergency",
    "kuch time", "ek mahina", "charge after", "charge later", "wont be using",
    "not using it this month", "out of country", "out of the country",
)
PRICE_TOKENS = (
    "expensive", "price", "priced", "pricey", "cost", "costly", "costing",
    "cheaper", "cheapest", "afford", "discount", "downgrade", "mehenga", "sasta",
    "budget", "overpriced", "zyada", "worth", "smaller plan", "basic plan",
    "basic features", "free tier", "less money", "justify", "expense",
    "too much", "lower tier", "cost cutting", "hike", "affordable",
    "half the price", "lower the amount", "reduce the price", "reduce my subscription",
    "reduce cost", "any discount", "student discount", "annual plan is cheaper",
    "why pay full", "lower amount", "smaller tier",
)
DONE_TOKENS = (
    "cancel", "finished", "unsubscribe", "terminate", "discontinue", "quit",
    "retired", "disbanded", "not using anymore", "no longer", "dont need",
    "don't need", "dont want", "don't want", "not interested", "zarurat nahi",
    "wrap up", "closing this account", "close my account", "close the subscription",
    "end the subscription", "end it", "not renewing", "not required",
    "another vendor", "own solution", "shutting down", "stop charging",
    "not useful", "delete my account", "moving on", "project is over",
    "need is over", "done with", "i am done", "permanently", "for good",
    "hamesha", "no longer relevant", "wont be coming back", "stop billing",
    "stop it", "close the plan", "terminate the plan", "not a pause",
)


def _hits(text: str, tokens: tuple[str, ...]) -> int:
    """Count lexicon hits. Single words match on word boundaries."""
    import re

    n = 0
    for tok in tokens:
        if " " in tok or "'" in tok:
            if tok in text:
                n += 1
        elif re.search(rf"\b{re.escape(tok)}\b", text):
            n += 1
    return n


def classify_intent(text: str) -> str:
    """temporary | price | done.

    Negations are resolved before scoring: "stop auto debit temporarily not
    permanently" and "permanently cancel, not a pause" contain the same two
    keywords and mean opposite things.
    """
    t = (text or "").lower()

    if "not permanently" in t or "not a permanent" in t or "temporarily not" in t:
        return "temporary"
    if "not a pause" in t or "instead of pausing" in t:
        return "done"
    if "pause not cancel" in t or "pause instead of cancel" in t or "dont cancel" in t \
            or "don't cancel" in t or "not cancelling" in t:
        return "temporary"

    price, done, temp = _hits(t, PRICE_TOKENS), _hits(t, DONE_TOKENS), _hits(t, TEMPORARY_TOKENS)
    # A price objection is a reason to leave, so it usually co-occurs with
    # cancel language; it is the more specific label and wins ties against it.
    if price and price >= done and price >= temp:
        return "price"
    if done > temp:
        return "done"
    if temp > 0:
        return "temporary"
    return "done" if done else "temporary"


class OfflineAdjudicator:
    name = "offline_stub"

    def __init__(self, today: date | None = None, calendar: HolidayCalendar | None = None,
                 preemptive_threshold: float | None = None):
        self.today = today or date.today()
        self.cal = calendar or default_calendar()
        self.preemptive_threshold = preemptive_threshold

    def _resume(self, ev: Evidence) -> str:
        return suggested_resume_date(
            self.today, ev.customer.salary_day, ev.mandate.cycle_day, self.cal
        ).isoformat()

    def decide(self, ev: Evidence) -> Decision:
        allowed = set(ev.allowed_actions)
        m, c = ev.mandate, ev.customer
        fam = reason_family(m.last_error_reason)
        d2s = ev.days_to_salary

        def out(**kw) -> Decision:
            kw.setdefault("evidence_used", [])
            return Decision(**kw).clamped()

        # ---------------------------------------------------- cancel intent
        if ev.cancel_intent_text:
            klass = classify_intent(ev.cancel_intent_text)
            ev_used = [f"cancel_intent_text={ev.cancel_intent_text[:48]!r}", f"rail={m.rail.value}"]
            if klass == "done":
                act = Action.CANCEL_AT_CYCLE_END if Action.CANCEL_AT_CYCLE_END in allowed else Action.ESCALATE
                return out(cause=Cause.CUSTOMER_INTENT_DONE, cause_confidence=0.78,
                           action=act, action_confidence=0.74, evidence_used=ev_used,
                           rationale="Customer states the need is over. Cancelling at cycle end "
                                     "honours the paid period instead of terminating immediately.",
                           customer_message="Understood - we'll close your plan at the end of the "
                                            "current cycle. You keep access until then.")
            if klass == "price":
                if Action.STEP_DOWN_PLAN in allowed:
                    return out(cause=Cause.CUSTOMER_INTENT_PRICE, cause_confidence=0.74,
                               action=Action.STEP_DOWN_PLAN, action_confidence=0.70,
                               step_down_target_plan_id="plan_basic",
                               evidence_used=ev_used + ["rail=card supports plan change"],
                               rationale="Price objection on a card mandate, the only rail where the "
                                         "plan can be changed. Stepping down keeps the relationship.",
                               customer_message="We can move you to a smaller plan at a lower monthly "
                                                "amount instead of cancelling.")
                if Action.PAUSE in allowed:
                    return out(cause=Cause.CUSTOMER_INTENT_PRICE, cause_confidence=0.70,
                               action=Action.PAUSE, action_confidence=0.58, pause_cycles=1,
                               resume_on=self._resume(ev),
                               evidence_used=ev_used + [f"rail={m.rail.value} cannot change amount"],
                               rationale="Price objection, but Razorpay does not allow amount changes "
                                         "on this rail. A one-cycle pause buys time to negotiate "
                                         "rather than losing the mandate outright.",
                               customer_message="We've paused your next billing cycle while we sort "
                                                "out a plan that fits your budget.")
                return out(cause=Cause.CUSTOMER_INTENT_PRICE, cause_confidence=0.68,
                           action=Action.ESCALATE, action_confidence=0.5, escalate=True,
                           escalate_reason="price objection with no in-policy lever on this rail/status",
                           evidence_used=ev_used, rationale="Price objection but no permitted action.")
            cycles = 2 if any(k in ev.cancel_intent_text.lower()
                              for k in ("2 month", "two month", "45 day", "6 week", "till nov",
                                        "till dec", "2 cycles", "60 day")) else 1
            if Action.PAUSE in allowed:
                return out(cause=Cause.CUSTOMER_INTENT_TEMPORARY, cause_confidence=0.82,
                           action=Action.PAUSE, action_confidence=0.78, pause_cycles=cycles,
                           resume_on=self._resume(ev), evidence_used=ev_used,
                           rationale=f"Customer asks for a temporary break, not an exit. Pausing "
                                     f"{cycles} cycle(s) preserves the mandate; cancelling would force "
                                     f"re-registration, which frequently fails on Indian rails.",
                           customer_message="No problem - we've paused your billing. Nothing will be "
                                            "debited until you're back.")
            return out(cause=Cause.CUSTOMER_INTENT_TEMPORARY, cause_confidence=0.80,
                       action=Action.ESCALATE, action_confidence=0.5, escalate=True,
                       escalate_reason=f"temporary-break request but pause not permitted from "
                                       f"{m.status.value}",
                       evidence_used=ev_used,
                       rationale="Customer wants a pause but the subscription is not in a state "
                                 "Razorpay permits pausing from.")

        # ------------------------------------------------ live payment failure
        if m.status in (SubStatus.PENDING, SubStatus.HALTED) or m.last_error_reason:
            base_ev = [f"status={m.status.value}", f"auth_attempts={m.auth_attempts}",
                       f"reason={m.last_error_reason}"]

            if ev.emandate_attempt_in_flight:
                return out(cause=Cause.UNKNOWN, cause_confidence=0.5,
                           action=Action.NOOP, action_confidence=0.72,
                           evidence_used=base_ev + ["emandate_attempt_in_flight=true"],
                           rationale="An eMandate debit has been sent and its confirmation has not "
                                     "arrived. Charging now risks a double debit. Wait for the bank.")

            if fam == "liquidity":
                if d2s is not None and d2s <= 0 and Action.MANUAL_CHARGE in allowed:
                    return out(cause=Cause.LIQUIDITY_TIMING, cause_confidence=0.80,
                               action=Action.MANUAL_CHARGE, action_confidence=0.72,
                               evidence_used=base_ev + [f"days_to_salary={d2s}"],
                               rationale="Insufficient funds, but the salary credit has already "
                                         "landed. Charging the open invoice now is the cheapest "
                                         "recovery available on this rail.")
                if d2s is not None and 1 <= d2s <= 6:
                    if m.auth_attempts < 3:
                        return out(cause=Cause.LIQUIDITY_TIMING, cause_confidence=0.76,
                                   action=Action.NOOP, action_confidence=0.68,
                                   evidence_used=base_ev + [f"days_to_salary={d2s}"],
                                   rationale=f"Timing, not inability: salary lands in {d2s} day(s) and "
                                             f"a scheduled retry falls after it. Let the ladder run "
                                             f"rather than spending an intervention.")
                    return out(cause=Cause.LIQUIDITY_TIMING, cause_confidence=0.74,
                               action=Action.ESCALATE, action_confidence=0.55, escalate=True,
                               escalate_reason="retries nearly exhausted before the salary credit lands",
                               evidence_used=base_ev + [f"days_to_salary={d2s}"],
                               rationale="Salary arrives after the last scheduled retry; the mandate "
                                         "will halt first. Needs a human to time a charge.")
                return out(cause=Cause.LIQUIDITY_STRUCTURAL, cause_confidence=0.66,
                           action=(Action.STEP_DOWN_PLAN if Action.STEP_DOWN_PLAN in allowed
                                   else Action.ESCALATE),
                           action_confidence=0.66 if Action.STEP_DOWN_PLAN in allowed else 0.5,
                           step_down_target_plan_id="plan_basic" if Action.STEP_DOWN_PLAN in allowed else None,
                           escalate=Action.STEP_DOWN_PLAN not in allowed,
                           escalate_reason=None if Action.STEP_DOWN_PLAN in allowed
                                           else "structural shortfall with no amount lever on this rail",
                           evidence_used=base_ev + [f"days_to_salary={d2s}",
                                                    f"prior_fail_count_6m={ev.prior_fail_count_6m}"],
                           rationale="Repeated shortfall with no salary credit nearby. Charging the "
                                     "same account again is unlikely to work; reduce the amount if "
                                     "the rail allows it, otherwise a human should talk to them.")

            if fam == "technical":
                if m.auth_attempts < 3:
                    return out(cause=Cause.BANK_TECHNICAL, cause_confidence=0.74,
                               action=Action.NOOP, action_confidence=0.70,
                               evidence_used=base_ev + [f"bank_in_downtime={ev.in_downtime}",
                                                        f"bank_td={ev.bank_health.get('td_pct')}"],
                               rationale="Technical decline at the bank or gateway. These usually "
                                         "clear on the scheduled retry; intervening now would spend "
                                         "an intervention on a problem that resolves itself.")
                if Action.MANUAL_CHARGE in allowed and (d2s is None or d2s <= 0):
                    return out(cause=Cause.BANK_TECHNICAL, cause_confidence=0.70,
                               action=Action.MANUAL_CHARGE, action_confidence=0.67,
                               evidence_used=base_ev, rationale="Retries exhausted on a technical "
                               "decline; a fresh attempt on the open invoice is worth one try.")
                return out(cause=Cause.BANK_TECHNICAL, cause_confidence=0.68,
                           action=Action.ESCALATE, action_confidence=0.5, escalate=True,
                           escalate_reason="technical failures persisted past the retry ladder",
                           evidence_used=base_ev, rationale="Persistent technical failure.")

            if fam == "instrument":
                looks_remap = (
                    m.rail == Rail.CARD
                    and (m.last_error_reason or "") == "card_expired"
                    and c.tenure_months >= 12
                    and ev.prior_fail_count_6m <= 1
                )
                if looks_remap:
                    return out(cause=Cause.REMAP_IN_FLIGHT, cause_confidence=0.62,
                               action=Action.NOOP, action_confidence=0.60,
                               evidence_used=base_ev + [f"tenure_months={c.tenure_months}",
                                                        f"prior_fail_count_6m={ev.prior_fail_count_6m}"],
                               rationale="'card_expired' on a long-tenured customer with a clean "
                                         "history. Under RBI's 2026 framework this is more likely a "
                                         "reissued-card remap in flight than a real expiry. Asking "
                                         "them to re-authorise would churn a good customer.")
                return out(cause=Cause.INSTRUMENT_INVALID, cause_confidence=0.78,
                           action=(Action.REQUEST_REAUTH if Action.REQUEST_REAUTH in allowed
                                   else Action.ESCALATE),
                           action_confidence=0.72, escalate=True,
                           escalate_reason="re-authorisation needs the customer and a human sign-off",
                           evidence_used=base_ev,
                           rationale="The instrument itself is invalid. No amount of retrying fixes "
                                     "this; the mandate has to be re-papered.",
                           customer_message="Your saved payment method is no longer valid - could you "
                                            "re-authorise so your plan continues?")

            if fam == "limit":
                if Action.STEP_DOWN_PLAN in allowed:
                    return out(cause=Cause.LIMIT_EXCEEDED, cause_confidence=0.76,
                               action=Action.STEP_DOWN_PLAN, action_confidence=0.70,
                               step_down_target_plan_id="plan_basic", evidence_used=base_ev,
                               rationale="Debit exceeds the mandate or card limit. Reducing the "
                                         "amount is the only fix that keeps the mandate alive.")
                return out(cause=Cause.LIMIT_EXCEEDED, cause_confidence=0.74,
                           action=Action.ESCALATE, action_confidence=0.5, escalate=True,
                           escalate_reason="amount exceeds the mandate cap and this rail cannot be amended",
                           evidence_used=base_ev + [f"amount_inr={m.plan_amount_paise/100}"],
                           rationale="The debit is over the mandate cap and Razorpay does not permit "
                                     "amending a UPI or eMandate mandate. Needs a new mandate.")

            return out(cause=Cause.UNKNOWN, cause_confidence=0.40,
                       action=Action.ESCALATE, action_confidence=0.45, escalate=True,
                       escalate_reason="catch-all decline reason carries no diagnosis",
                       evidence_used=base_ev,
                       rationale="Reason code is Razorpay's catch-all, which tells us nothing about "
                                 "cause. Guessing here risks a wrong money action.")

        # -------------------------------------------------- predicted failure
        if m.status == SubStatus.ACTIVE and ev.p_fail > 0:
            # Pre-emptive pause is the most valuable action Grace has and the
            # easiest to overuse: every pause spent on a mandate that would
            # have paid is a healthy customer nagged. Require BOTH a high risk
            # score and a prior failure. Threshold is CONFIG.theta_high, chosen
            # from principle -- it was never tuned against the holdout.
            if (d2s is not None and 1 <= d2s <= 6 and Action.PAUSE in allowed
                    and ev.p_fail >= (self.preemptive_threshold
                                      if self.preemptive_threshold is not None
                                      else ev.preemptive_threshold)
                    and ev.prior_fail_count_6m >= 1):
                return out(cause=Cause.LIQUIDITY_TIMING, cause_confidence=0.60,
                           action=Action.PAUSE, action_confidence=0.58, pause_cycles=1,
                           resume_on=self._resume(ev),
                           evidence_used=[f"p_fail={ev.p_fail:.2f}", f"days_to_salary={d2s}",
                                          f"prior_fail_count_6m={ev.prior_fail_count_6m}"],
                           rationale=f"No failure yet, but the debit is scheduled {d2s} day(s) before "
                                     f"the salary credit and this mandate has failed before. Pausing "
                                     f"one cycle now is cheaper than four failed retries and a halt.")
            if ev.in_downtime:
                return out(cause=Cause.BANK_TECHNICAL, cause_confidence=0.58,
                           action=Action.NOOP, action_confidence=0.62,
                           evidence_used=[f"p_fail={ev.p_fail:.2f}", "bank_in_downtime=true"],
                           rationale="Elevated risk is explained by a known bank downtime window, "
                                     "which the retry ladder already handles.")

        return out(cause=Cause.UNKNOWN, cause_confidence=0.5, action=Action.NOOP,
                   action_confidence=0.70, evidence_used=[f"p_fail={ev.p_fail:.2f}"],
                   rationale="No failure, no customer signal and risk below the intervention "
                             "threshold. Leaving a healthy mandate alone is the correct action.")
