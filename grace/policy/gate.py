"""The trust boundary: the model proposes, code disposes (spec 9.3).

Every override is recorded as a flag and counted in the report. Nothing here
is silent. If this file and the prompt ever disagree, this file wins.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from grace.config import Bounds
from grace.models import INTERVENTIONS, Action, Cause, Evidence
from grace.policy.bounds import DEFAULT_BOUNDS
from grace.signals.holidays import HolidayCalendar, default_calendar
from grace.signals.salary_cycle import suggested_resume_date
from grace.sim.vocab import reason_family


@dataclass
class GateResult:
    final_action: Action
    params: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)

    @property
    def overridden(self) -> bool:
        return bool(self.flags)


def gate(
    decision,
    ev: Evidence,
    *,
    guard=None,
    store=None,
    bounds: Bounds = DEFAULT_BOUNDS,
    today: date | None = None,
    calendar: HolidayCalendar | None = None,
) -> GateResult:
    """Validate a proposed decision against policy. Returns the action to execute."""
    cal = calendar or default_calendar()
    today = today or date.today()
    m = ev.mandate
    proposed = decision.action
    flags: dict[str, Any] = {}
    params: dict[str, Any] = {}

    def deny(flag: str, detail: Any = True) -> GateResult:
        flags[flag] = detail
        return GateResult(Action.ESCALATE, params, flags)

    # 1. The action must exist in the rail x status matrix. This is the check
    #    that catches a model hallucinating an unsupported capability.
    if proposed not in ev.allowed_actions:
        return deny("model_out_of_policy", f"{proposed.value} not allowed for "
                                           f"{m.rail.value}/{m.status.value}")

    # 2. Re-authorisation creates a new mandate and a new customer ask. Always human.
    if proposed == Action.REQUEST_REAUTH:
        return deny("human_required", "request_reauth requires human approval")

    if proposed in INTERVENTIONS:
        # 3. No relationship yet: nothing has ever been paid.
        if m.paid_count == 0:
            return deny("no_relationship_yet", "paid_count == 0")
        # 4. Stopping rules.
        if m.interventions_total >= bounds.MAX_INTERVENTIONS_TOTAL:
            return deny("stopping_rule_hit",
                        f"interventions_total {m.interventions_total} >= {bounds.MAX_INTERVENTIONS_TOTAL}")
        if m.interventions_this_cycle >= bounds.MAX_INTERVENTIONS_PER_CYCLE:
            return deny("stopping_rule_hit",
                        f"interventions_this_cycle {m.interventions_this_cycle} >= "
                        f"{bounds.MAX_INTERVENTIONS_PER_CYCLE}")

    # 5. Cancelling is irreversible. Only on a clear, confident "I am done".
    if proposed == Action.CANCEL_AT_CYCLE_END:
        if decision.cause != Cause.CUSTOMER_INTENT_DONE:
            return deny("cancel_cause_gate", f"cause is {decision.cause.value}, not customer_intent_done")
        if decision.cause_confidence < bounds.CONF_CANCEL_CAUSE:
            return deny("cancel_cause_gate",
                        f"cause_confidence {decision.cause_confidence:.2f} < {bounds.CONF_CANCEL_CAUSE}")

    # 6. Confidence gates, stricter for anything that moves money.
    if proposed in (Action.MANUAL_CHARGE, Action.STEP_DOWN_PLAN):
        if decision.action_confidence < bounds.CONF_MONEY:
            return deny("confidence_below_gate",
                        f"{decision.action_confidence:.2f} < {bounds.CONF_MONEY} for a money action")
    elif proposed == Action.PAUSE:
        if decision.action_confidence < bounds.CONF_PAUSE:
            return deny("confidence_below_gate",
                        f"{decision.action_confidence:.2f} < {bounds.CONF_PAUSE} for pause")

    # 7. Pause parameters are re-derived, never trusted.
    if proposed == Action.PAUSE:
        cycles = decision.pause_cycles or 1
        if not (1 <= cycles <= bounds.MAX_PAUSE_CYCLES):
            flags["param_rewritten"] = f"cycles {cycles} -> {min(max(cycles,1), bounds.MAX_PAUSE_CYCLES)}"
            cycles = min(max(cycles, 1), bounds.MAX_PAUSE_CYCLES)
        want = None
        if decision.resume_on:
            try:
                want = date.fromisoformat(decision.resume_on)
            except (TypeError, ValueError):
                flags["param_rewritten"] = f"unparseable resume_on {decision.resume_on!r}"
        safe = suggested_resume_date(today, ev.customer.salary_day, m.cycle_day, cal)
        if want is None:
            resume_on = safe
        elif want <= today or want > today + timedelta(days=bounds.MAX_RESUME_HORIZON_DAYS):
            flags["param_rewritten"] = f"resume_on {want} out of window -> {safe}"
            resume_on = safe
        elif cal.is_bank_holiday(want):
            nxt = cal.next_business_day(want)
            flags["param_rewritten"] = f"resume_on {want} is a bank holiday -> {nxt}"
            resume_on = nxt
        else:
            resume_on = want
        params = {"cycles": cycles, "resume_on": resume_on.isoformat()}

    # 8. Charging an existing invoice. Integrity is checked BEFORE efficacy:
    #    a double debit is a compliance incident, a mistimed charge is only a
    #    wasted attempt.
    if proposed == Action.MANUAL_CHARGE:
        if guard is not None and store is not None:
            inv = store.open_invoice(m.id)
            if inv is None:
                return deny("no_open_invoice")
            ok, why = guard.check_manual_charge(m, inv)
            if not ok:
                return deny("integrity_blocked", why)
            params["invoice_id"] = inv.id
        # Efficacy: never charge a liquidity failure before the money lands.
        # Scoped to liquidity causes -- salary timing says nothing about a
        # technical decline, and blocking those would be superstition.
        liquidity = decision.cause in (Cause.LIQUIDITY_TIMING, Cause.LIQUIDITY_STRUCTURAL) or \
            reason_family(m.last_error_reason) == "liquidity"
        if liquidity and ev.days_to_salary is not None and ev.days_to_salary > 0:
            return deny("charge_before_salary_blocked",
                        f"salary is {ev.days_to_salary}d away; charging now would fail again")

    # 9. Resuming a mandate we may not control, or whose token may be dead.
    if proposed == Action.RESUME and guard is not None:
        ok, why = guard.check_resume(m)
        if not ok:
            return deny("integrity_blocked", why)

    return GateResult(proposed, params, flags)
