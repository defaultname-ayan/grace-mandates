"""Orchestrator invariants that the metrics silently depend on."""
from __future__ import annotations

from datetime import date

from grace.adjudicate.schema import Decision
from grace.models import Action, Cause, Evidence, Rail, SubStatus
from grace.orchestrator import arm_db_path, run_batch
from grace.sim.cohort import generate
from grace.store import Store

TODAY = date(2026, 9, 4)


def _seed(tmp_path, n=80):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    s = Store(run_dir / "grace.db")
    try:
        generate(s, n=n, seed=7, decision_date=TODAY)
        original = {m.id: m.rupees_at_stake for m in s.all_mandates()}
    finally:
        s.close()
    return run_dir, original


class AlwaysStepDown:
    name = "test_stepdown"
    metas: list = []

    def decide(self, ev: Evidence) -> Decision:
        if ev.mandate.rail == Rail.CARD and ev.mandate.status == SubStatus.ACTIVE:
            return Decision(cause=Cause.LIQUIDITY_STRUCTURAL, cause_confidence=0.9,
                            action=Action.STEP_DOWN_PLAN, action_confidence=0.9,
                            step_down_target_plan_id="plan_basic", rationale="test")
        return Decision(cause=Cause.UNKNOWN, cause_confidence=0.5, action=Action.NOOP,
                        action_confidence=0.9, rationale="test")


def test_rupees_at_stake_is_frozen_before_the_action_mutates_the_amount(tmp_path):
    """A step-down cuts the plan to 60%; the stake recorded for scoring must be
    the pre-action amount, or every step-down mandate is under-counted."""
    run_dir, original = _seed(tmp_path)
    run_batch(run_dir, "agent", AlwaysStepDown(), decision_date=TODAY, adjudicator_name="test")
    s = Store(arm_db_path(run_dir, "agent"))
    try:
        executed = [(mid, d) for mid, d in s.decisions_for_arm("agent").items()
                    if d["final_action"] == "step_down_plan" and d["executed"]]
        assert executed, "the stub must have executed at least one step-down"
        for mid, d in executed:
            assert s.get_mandate(mid).rupees_at_stake < original[mid], "sanity: it did mutate"
            assert d["rupees_at_stake"] == original[mid], mid
    finally:
        s.close()


class ChargeEverything:
    name = "test_charge"
    metas: list = []

    def decide(self, ev: Evidence) -> Decision:
        if Action.MANUAL_CHARGE in ev.allowed_actions:
            return Decision(cause=Cause.BANK_TECHNICAL, cause_confidence=0.9,
                            action=Action.MANUAL_CHARGE, action_confidence=0.9, rationale="t")
        return Decision(cause=Cause.UNKNOWN, cause_confidence=0.5, action=Action.NOOP,
                        action_confidence=0.9, rationale="t")


def test_manual_charge_lock_is_released_after_the_action(tmp_path):
    run_dir, _ = _seed(tmp_path, n=1200)
    run_batch(run_dir, "agent", ChargeEverything(), decision_date=TODAY, adjudicator_name="t")
    s = Store(arm_db_path(run_dir, "agent"))
    try:
        charged = [d for d in s.decisions_for_arm("agent").values()
                   if d["final_action"] == "manual_charge"]
        assert charged, "need at least one executed manual charge to test the lock"
        for d in charged:
            inv = d["params"].get("invoice_id")
            assert inv and not s.is_locked(inv), f"lock on {inv} must be released after the action"
    finally:
        s.close()


def test_denied_charge_does_not_leak_a_lock(tmp_path):
    """The gate used to acquire the lock inside the check and then deny the
    action on salary timing, leaving the invoice locked for 72h."""
    class ChargeTooEarly(ChargeEverything):
        def decide(self, ev: Evidence) -> Decision:
            d = super().decide(ev)
            if d.action == Action.MANUAL_CHARGE:
                d.cause = Cause.LIQUIDITY_TIMING  # the salary gate applies
            return d

    run_dir, _ = _seed(tmp_path, n=1200)
    run_batch(run_dir, "agent", ChargeTooEarly(), decision_date=TODAY, adjudicator_name="t")
    s = Store(arm_db_path(run_dir, "agent"))
    try:
        denied = [d for d in s.decisions_for_arm("agent").values()
                  if "charge_before_salary_blocked" in d.get("gate_flags", {})]
        assert denied, "expected some charges denied on salary timing"
        for m in s.all_mandates():
            oi = s.open_invoice(m.id)
            if oi is not None:
                assert not s.is_locked(oi.id), f"a denied action leaked a lock on {oi.id}"
    finally:
        s.close()


def test_fallback_is_an_explicit_flag_not_a_string_match(tmp_path):
    class Broken:
        name = "broken"
        metas: list = []

        def decide(self, ev: Evidence) -> Decision:
            raise RuntimeError("simulated provider outage")

    run_dir, _ = _seed(tmp_path, n=40)
    summary = run_batch(run_dir, "agent", Broken(), decision_date=TODAY, adjudicator_name="broken")
    assert summary["adjudicator_fallbacks"] == summary["n_triggered"] > 0
    s = Store(arm_db_path(run_dir, "agent"))
    try:
        ds = s.decisions_for_arm("agent")
        assert sum(1 for d in ds.values() if d.get("fallback")) == summary["adjudicator_fallbacks"]
        assert all(d["final_action"] == "escalate" for d in ds.values() if d.get("fallback")), \
            "a fallback must never act"
    finally:
        s.close()


def test_current_cycle_is_never_dated_into_last_month(tmp_path):
    """7 of 9 cycle days used to get a 'current' failure anchored a month back,
    with the scheduled retry 27 days in the past."""
    run_dir, _ = _seed(tmp_path, n=400)
    s = Store(run_dir / "grace.db")
    try:
        pending = active = 0
        for m in s.all_mandates():
            if m.status == SubStatus.PENDING:
                pending += 1
                assert m.charge_at is not None and m.charge_at.date() >= TODAY, (
                    f"{m.id}: pending retry scheduled in the past: {m.charge_at}")
            elif m.status == SubStatus.ACTIVE and m.charge_at is not None:
                active += 1
                assert m.charge_at.date() >= TODAY, f"{m.id}: next charge in the past: {m.charge_at}"
        assert pending and active
    finally:
        s.close()
