"""Evaluation must be fair, paired, and holdout-only (spec 14)."""
from __future__ import annotations



from grace.evaluation.metrics import MIDDLE_STATES, compare, evaluate_arm, outcome_draw, survived
from grace.models import ACTION_TO_CF_KEY, CF_KEYS, Action, Cause, Mandate, Rail, SubStatus, Truth
from grace.sim.cohort import COUNTERFACTUALS, HEALTHY_CF, is_holdout
from grace.store import Store


def test_common_random_numbers_are_stable_and_shared():
    """Arms must differ by their action, never by luck."""
    a = outcome_draw("simsub_00042")
    b = outcome_draw("simsub_00042")
    assert a == b and 0.0 <= a < 1.0
    assert outcome_draw("simsub_00042") != outcome_draw("simsub_00043")


def test_survival_uses_the_action_specific_counterfactual():
    t = Truth(will_fail=True, payment_will_fail=True, cause=Cause.LIQUIDITY_TIMING,
              survival_under={"noop": 0.0, "pause": 1.0, "manual_charge": 1.0,
                              "cancel_at_cycle_end": 0.0, "step_down_plan": 0.0,
                              "request_reauth": 0.0})
    assert survived(t, Action.PAUSE, "m1") is True
    assert survived(t, Action.NOOP, "m1") is False


def test_escalate_scores_as_noop_because_it_moves_no_money():
    assert ACTION_TO_CF_KEY[Action.ESCALATE] == "noop"
    assert ACTION_TO_CF_KEY[Action.RESUME] == "noop"


def test_every_action_has_a_counterfactual_key():
    for a in Action:
        assert a in ACTION_TO_CF_KEY
        assert ACTION_TO_CF_KEY[a] in CF_KEYS


def test_counterfactual_table_covers_every_cause_and_key():
    for cause, row in COUNTERFACTUALS.items():
        assert set(row) == set(CF_KEYS), f"{cause} missing counterfactual keys"
        for v in row.values():
            assert 0.0 <= v <= 1.0
    assert set(HEALTHY_CF) == set(CF_KEYS)


def test_doing_nothing_is_best_for_a_healthy_mandate():
    """Otherwise the false-intervention metric would be meaningless."""
    assert HEALTHY_CF["noop"] == max(HEALTHY_CF.values())
    for k, v in HEALTHY_CF.items():
        if k != "noop":
            assert v <= 0.90


def test_holdout_split_is_stable_across_processes():
    """The spec used Python hash(), which is per-process randomised. This is why."""
    ids = [f"simsub_{i:05d}" for i in range(2000)]
    first = [is_holdout(i) for i in ids]
    second = [is_holdout(i) for i in ids]
    assert first == second
    frac = sum(first) / len(first)
    assert 0.25 < frac < 0.35, f"holdout fraction drifted: {frac:.3f}"


def test_middle_states_are_the_ones_that_keep_a_relationship():
    assert Action.PAUSE in MIDDLE_STATES
    assert Action.CANCEL_AT_CYCLE_END not in MIDDLE_STATES
    assert Action.NOOP not in MIDDLE_STATES


def test_compare_reports_net_of_false_intervention_cost():
    arms = {
        "noop": {"rupees_preserved_paise": 1000, "mandates_preserved": 1,
                 "false_intervention_rate": None, "false_intervention_cost_paise": 0},
        "agent": {"rupees_preserved_paise": 3000, "mandates_preserved": 3,
                  "false_intervention_rate": 0.2, "false_intervention_cost_paise": 500},
    }
    c = compare(arms)["agent"]
    assert c["rupees_preserved_lift_paise"] == 2000
    assert c["net_rupees_paise"] == 1500, "net must subtract the cost of nagging healthy customers"


def test_evaluate_arm_scores_only_the_holdout(tmp_path):
    s = Store(tmp_path / "e.db")
    try:
        for i in range(40):
            mid = f"simsub_{i:05d}"
            m = Mandate(id=mid, customer_id=f"c{i}", rail=Rail.UPI_AUTOPAY,
                        plan_amount_paise=10000, cycle_day=5, status=SubStatus.ACTIVE,
                        paid_count=3, total_count=12)
            s.upsert_mandate(m, holdout=is_holdout(mid))
            s.set_truth(mid, Truth(will_fail=True, payment_will_fail=True,
                                   cause=Cause.LIQUIDITY_TIMING,
                                   survival_under={k: 1.0 if k == "pause" else 0.0 for k in CF_KEYS}))
            s.save_decision(f"agent:{mid}", mid, "agent", {
                "trigger": "failure", "p_fail": 0.9, "cause": "liquidity_timing",
                "cause_conf": 0.8, "proposed_action": "pause", "final_action": "pause",
                "action_conf": 0.8, "params": {}, "gate_flags": {}, "rationale": "t",
                "evidence_used": [], "escalate": False, "executed": True, "error": None,
                "rupees_at_stake": 90000,
            })
        res = evaluate_arm(s, "agent", holdout_only=True)
        assert res["n_scored"] == len(s.holdout_ids())
        assert res["n_scored"] < 40, "holdout must be a strict subset"
        assert res["mandates_preserved"] == res["at_risk"], "pause survives every case here"
        assert res["false_interventions"] == 0
    finally:
        s.close()


def test_intervening_on_a_healthy_mandate_counts_as_a_false_intervention(tmp_path):
    s = Store(tmp_path / "f.db")
    try:
        holdout_id = next(f"simsub_{i:05d}" for i in range(500) if is_holdout(f"simsub_{i:05d}"))
        m = Mandate(id=holdout_id, customer_id="c1", rail=Rail.UPI_AUTOPAY,
                    plan_amount_paise=100000, cycle_day=5, status=SubStatus.ACTIVE,
                    paid_count=5, total_count=12)
        s.upsert_mandate(m, holdout=True)
        s.set_truth(holdout_id, Truth(will_fail=False, payment_will_fail=False,
                                      survival_under=dict(HEALTHY_CF)))
        s.save_decision(f"agent:{holdout_id}", holdout_id, "agent", {
            "trigger": "predicted", "p_fail": 0.7, "cause": "liquidity_timing",
            "cause_conf": 0.6, "proposed_action": "pause", "final_action": "pause",
            "action_conf": 0.6, "params": {}, "gate_flags": {}, "rationale": "t",
            "evidence_used": [], "escalate": False, "executed": True, "error": None,
            "rupees_at_stake": 300000,
        })
        res = evaluate_arm(s, "agent", holdout_only=True)
        assert res["false_interventions"] == 1
        assert res["false_intervention_rate"] == 1.0
        assert res["false_intervention_cost_paise"] > 0, "nagging a healthy customer must cost something"
    finally:
        s.close()


def test_partial_arm_is_flagged_not_silently_compared(tmp_path):
    """A --limit run must never be compared against a full run without warning."""
    from grace.evaluation.run import score
    from grace.sim.cohort import is_holdout

    run_dir = tmp_path / "r"
    run_dir.mkdir()
    ids = [f"simsub_{i:05d}" for i in range(60)]
    for arm, take in (("noop", ids), ("agent", ids[:5])):
        s = Store(run_dir / f"arm_{arm}.db")
        try:
            for mid in ids:
                s.upsert_mandate(
                    Mandate(id=mid, customer_id="c", rail=Rail.UPI_AUTOPAY,
                            plan_amount_paise=10000, cycle_day=5, status=SubStatus.ACTIVE,
                            paid_count=2, total_count=12),
                    holdout=is_holdout(mid))
                s.set_truth(mid, Truth(will_fail=False, survival_under=dict(HEALTHY_CF)))
            for mid in take:
                s.save_decision(f"{arm}:{mid}", mid, arm, {
                    "trigger": "tick", "p_fail": 0.0, "cause": "unknown", "cause_conf": 0.5,
                    "proposed_action": "noop", "final_action": "noop", "action_conf": 0.9,
                    "params": {}, "gate_flags": {}, "rationale": "t", "evidence_used": [],
                    "escalate": False, "executed": False, "error": None, "rupees_at_stake": 0,
                })
        finally:
            s.close()

    payload = score(run_dir, ("noop", "agent"))
    assert payload["arms_comparable"] is False
    assert "PARTIAL_RUN_WARNING" in payload["arms"]["agent"]


def test_gate_flag_reasons_are_counted_as_keys_not_as_counts(tmp_path):
    """gate_flags maps flag -> reason STRING. Counter.update(dict) would try to
    add the string as a count and raise TypeError; keys must be counted."""
    s = Store(tmp_path / "g.db")
    try:
        mid = next(f"simsub_{i:05d}" for i in range(500) if is_holdout(f"simsub_{i:05d}"))
        s.upsert_mandate(Mandate(id=mid, customer_id="c", rail=Rail.CARD, plan_amount_paise=10000,
                                 cycle_day=5, status=SubStatus.ACTIVE, paid_count=2, total_count=12),
                         holdout=True)
        s.set_truth(mid, Truth(will_fail=False, survival_under=dict(HEALTHY_CF)))
        s.save_decision(f"agent:{mid}", mid, "agent", {
            "trigger": "predicted", "p_fail": 0.3, "cause": "unknown", "cause_conf": 0.5,
            "proposed_action": "pause", "final_action": "escalate", "action_conf": 0.4,
            "params": {}, "gate_flags": {"confidence_below_gate": "0.40 < 0.55 for pause"},
            "rationale": "t", "evidence_used": [], "escalate": True, "executed": False,
            "error": None, "rupees_at_stake": 30000,
        })
        res = evaluate_arm(s, "agent", holdout_only=True)
        assert res["gate_flags"] == {"confidence_below_gate": 1}
    finally:
        s.close()


def test_prior_failure_signals_are_alive_across_the_cohort(tmp_path):
    """Guards a bug that shipped twice, in opposite directions.

    Counting pending/halted EVENTS inflated one failure with two retries into
    "failed twice before". Counting each invoice by its FINAL outcome zeroed
    the feature, because generated history always recovers -- so no mandate had
    any prior failure, the predictor lost its strongest feature, and the
    pre-emptive pause could never fire. Both versions passed every other test.
    """
    from collections import Counter
    from datetime import date

    from grace.evidence import build_evidence
    from grace.signals.bank_health import BankHealth
    from grace.signals.holidays import HolidayCalendar
    from grace.sim.cohort import generate

    today = date(2026, 9, 4)
    s = Store(tmp_path / "c.db")
    try:
        generate(s, n=250, seed=11, decision_date=today)
        bh, cal = BankHealth(), HolidayCalendar()
        counts = Counter()
        for m in s.all_mandates():
            ev = build_evidence(s, m, bank_health=bh, calendar=cal, today=today)
            counts[ev.prior_fail_count_6m] += 1
            # A single current failure with N retries must not read as N bounces.
            assert ev.prior_fail_count_6m <= 6, f"{m.id}: implausible {ev.prior_fail_count_6m}"
        assert counts[0] < sum(counts.values()), "no mandate has a prior bounce: signal is dead"
        assert sum(v for k, v in counts.items() if k >= 1) > 0.2 * sum(counts.values()), \
            f"prior-bounce signal too sparse to be usable: {dict(counts)}"
    finally:
        s.close()
