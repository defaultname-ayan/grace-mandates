"""Policy is the trust boundary. These tests are the contract (spec 9)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from grace.adjudicate.schema import Decision
from grace.config import Bounds
from grace.integrity import IntegrityGuard
from grace.models import Action, Cause, Customer, Evidence, Mandate, Rail, SubStatus
from grace.policy import allowed_actions, gate
from grace.store import Store

TODAY = date(2026, 9, 4)


def ev_for(rail=Rail.UPI_AUTOPAY, status=SubStatus.ACTIVE, *, paid=3, salary_day=10,
           interventions_total=0, interventions_this_cycle=0, in_flight=False,
           pause_initiated_by=None, has_invoice=False, **kw) -> Evidence:
    m = Mandate(id="simsub_p1", customer_id="c1", rail=rail, plan_amount_paise=49900,
                cycle_day=5, status=status, paid_count=paid, total_count=12,
                interventions_total=interventions_total,
                interventions_this_cycle=interventions_this_cycle,
                pause_initiated_by=pause_initiated_by, **kw)
    c = Customer(id="c1", bank="HDFC Bank", salary_day=salary_day, tenure_months=14, ltv_band="mid")
    return Evidence(
        mandate=m, customer=c, recent_events=[], bank_health={"td_pct": 0.4, "bd_pct": 9.1},
        days_to_salary=None if salary_day is None else 6,
        allowed_actions=allowed_actions(rail, status, has_pending_invoice=has_invoice,
                                        pause_initiated_by=pause_initiated_by),
        p_fail=0.5, has_pending_invoice=has_invoice, emandate_attempt_in_flight=in_flight,
    )


def dec(action, cause=Cause.LIQUIDITY_TIMING, conf=0.9, cause_conf=0.9, **kw) -> Decision:
    return Decision(cause=cause, cause_confidence=cause_conf, action=action,
                    action_confidence=conf, rationale="test", **kw)


# ------------------------------------------------------- out-of-policy actions
@pytest.mark.parametrize("rail", [Rail.UPI_AUTOPAY, Rail.EMANDATE])
def test_step_down_is_refused_on_non_card_rails(rail):
    r = gate(dec(Action.STEP_DOWN_PLAN), ev_for(rail=rail), today=TODAY)
    assert r.final_action == Action.ESCALATE
    assert "model_out_of_policy" in r.flags


def test_pause_is_refused_on_an_authenticated_subscription():
    """Pausing an authenticated subscription cancels it permanently."""
    r = gate(dec(Action.PAUSE), ev_for(status=SubStatus.AUTHENTICATED), today=TODAY)
    assert r.final_action == Action.ESCALATE
    assert "model_out_of_policy" in r.flags


def test_pause_is_refused_while_pending():
    r = gate(dec(Action.PAUSE), ev_for(status=SubStatus.PENDING), today=TODAY)
    assert r.final_action == Action.ESCALATE


def test_resume_refused_on_customer_paused_upi():
    e = ev_for(rail=Rail.UPI_AUTOPAY, status=SubStatus.PAUSED, pause_initiated_by="customer")
    r = gate(dec(Action.RESUME), e, today=TODAY)
    assert r.final_action == Action.ESCALATE
    assert "model_out_of_policy" in r.flags


# --------------------------------------------------------------- human gating
def test_request_reauth_always_needs_a_human():
    e = ev_for(status=SubStatus.HALTED, has_invoice=True)
    r = gate(dec(Action.REQUEST_REAUTH), e, today=TODAY)
    assert r.final_action == Action.ESCALATE and "human_required" in r.flags


# ------------------------------------------------------------- stopping rules
def test_stopping_rule_on_total_interventions():
    r = gate(dec(Action.PAUSE), ev_for(interventions_total=3), today=TODAY)
    assert r.final_action == Action.ESCALATE and "stopping_rule_hit" in r.flags


def test_stopping_rule_within_a_cycle():
    r = gate(dec(Action.PAUSE), ev_for(interventions_this_cycle=1), today=TODAY)
    assert r.final_action == Action.ESCALATE and "stopping_rule_hit" in r.flags


def test_no_intervention_before_the_first_successful_payment():
    r = gate(dec(Action.PAUSE), ev_for(paid=0), today=TODAY)
    assert r.final_action == Action.ESCALATE and "no_relationship_yet" in r.flags


# ---------------------------------------------------------- confidence gating
def test_low_confidence_pause_is_refused():
    r = gate(dec(Action.PAUSE, conf=0.4), ev_for(), today=TODAY)
    assert r.final_action == Action.ESCALATE and "confidence_below_gate" in r.flags


def test_money_actions_need_a_higher_bar_than_pause():
    b = Bounds()
    conf = (b.CONF_PAUSE + b.CONF_MONEY) / 2  # passes pause, fails money
    assert gate(dec(Action.PAUSE, conf=conf), ev_for(), today=TODAY).final_action == Action.PAUSE
    e = ev_for(rail=Rail.CARD, status=SubStatus.ACTIVE)
    r = gate(dec(Action.STEP_DOWN_PLAN, conf=conf), e, today=TODAY)
    assert r.final_action == Action.ESCALATE and "confidence_below_gate" in r.flags


# ----------------------------------------------------------------- cancelling
def test_cancel_requires_a_confident_done_signal():
    r = gate(dec(Action.CANCEL_AT_CYCLE_END, cause=Cause.LIQUIDITY_TIMING), ev_for(), today=TODAY)
    assert r.final_action == Action.ESCALATE and "cancel_cause_gate" in r.flags

    r = gate(dec(Action.CANCEL_AT_CYCLE_END, cause=Cause.CUSTOMER_INTENT_DONE, cause_conf=0.5),
             ev_for(), today=TODAY)
    assert r.final_action == Action.ESCALATE and "cancel_cause_gate" in r.flags

    r = gate(dec(Action.CANCEL_AT_CYCLE_END, cause=Cause.CUSTOMER_INTENT_DONE, cause_conf=0.9),
             ev_for(), today=TODAY)
    assert r.final_action == Action.CANCEL_AT_CYCLE_END


# ------------------------------------------------------------ pause parameters
def test_pause_params_are_rederived_not_trusted():
    r = gate(dec(Action.PAUSE, pause_cycles=9, resume_on="1999-01-01"), ev_for(), today=TODAY)
    assert r.final_action == Action.PAUSE
    assert r.params["cycles"] == 2, "cycles clamped to the maximum"
    assert date.fromisoformat(r.params["resume_on"]) > TODAY
    assert "param_rewritten" in r.flags


def test_unparseable_resume_date_is_replaced():
    r = gate(dec(Action.PAUSE, pause_cycles=1, resume_on="not-a-date"), ev_for(), today=TODAY)
    assert r.final_action == Action.PAUSE and "param_rewritten" in r.flags
    date.fromisoformat(r.params["resume_on"])


def test_resume_date_never_lands_on_a_bank_holiday():
    from grace.signals.holidays import HolidayCalendar

    cal = HolidayCalendar()
    r = gate(dec(Action.PAUSE, pause_cycles=1, resume_on="2026-10-02"), ev_for(), today=TODAY)
    assert not cal.is_bank_holiday(date.fromisoformat(r.params["resume_on"]))


# -------------------------------------------------------------- manual charge
def test_manual_charge_blocked_while_emandate_confirmation_pending(tmp_path):
    from grace.sim.engine import SimEngine

    s = Store(tmp_path / "p.db")
    try:
        eng = SimEngine(s, seed=3, decision_date=TODAY)
        e = ev_for(rail=Rail.EMANDATE, status=SubStatus.PENDING, has_invoice=True,
                   in_flight=True, salary_day=None)
        s.upsert_mandate(e.mandate)
        inv = eng._new_invoice(e.mandate, 0, eng.now)
        inv.attempt_in_flight = True
        s.upsert_invoice(inv)
        g = IntegrityGuard(s, now=eng.now)
        r = gate(dec(Action.MANUAL_CHARGE, cause=Cause.UNKNOWN), e, guard=g, store=s, today=TODAY)
        assert r.final_action == Action.ESCALATE
        assert "integrity_blocked" in r.flags
    finally:
        s.close()


def test_liquidity_charge_blocked_before_salary_lands(tmp_path):
    from grace.sim.engine import SimEngine

    s = Store(tmp_path / "q.db")
    try:
        eng = SimEngine(s, seed=3, decision_date=TODAY)
        e = ev_for(rail=Rail.CARD, status=SubStatus.HALTED, has_invoice=True, salary_day=10)
        e.mandate.last_error_reason = "insufficient_funds"
        s.upsert_mandate(e.mandate)
        s.upsert_invoice(eng._new_invoice(e.mandate, 0, eng.now))
        r = gate(dec(Action.MANUAL_CHARGE, cause=Cause.LIQUIDITY_TIMING), e,
                 guard=IntegrityGuard(s, now=eng.now), store=s, today=TODAY)
        assert r.final_action == Action.ESCALATE
        assert "charge_before_salary_blocked" in r.flags
    finally:
        s.close()


def test_technical_charge_is_not_blocked_by_salary_timing(tmp_path):
    """Salary timing says nothing about a technical decline."""
    from grace.sim.engine import SimEngine

    s = Store(tmp_path / "r.db")
    try:
        eng = SimEngine(s, seed=3, decision_date=TODAY)
        e = ev_for(rail=Rail.CARD, status=SubStatus.HALTED, has_invoice=True, salary_day=10)
        e.mandate.last_error_reason = "gateway_technical_error"
        s.upsert_mandate(e.mandate)
        s.upsert_invoice(eng._new_invoice(e.mandate, 0, eng.now))
        r = gate(dec(Action.MANUAL_CHARGE, cause=Cause.BANK_TECHNICAL), e,
                 guard=IntegrityGuard(s, now=eng.now), store=s, today=TODAY)
        assert r.final_action == Action.MANUAL_CHARGE
        assert "invoice_id" in r.params
    finally:
        s.close()


# ------------------------------------------------------------------ happy path
def test_a_clean_pause_passes_untouched():
    r = gate(dec(Action.PAUSE, pause_cycles=1, resume_on=(TODAY + timedelta(days=12)).isoformat()),
             ev_for(), today=TODAY)
    assert r.final_action == Action.PAUSE and not r.flags and r.params["cycles"] == 1


def test_noop_and_escalate_are_always_available():
    for rail in Rail:
        for status in SubStatus:
            a = allowed_actions(rail, status)
            assert Action.NOOP in a and Action.ESCALATE in a
