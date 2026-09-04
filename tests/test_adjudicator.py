"""Adjudicator contract: the 12 cases from spec Appendix A, plus schema safety.

These run against the deterministic offline stub so they need no network. They
encode what a CORRECT decision looks like for each situation; the Claude
adjudicator is held to the same contract by the same assertions when run online.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from grace.adjudicate.offline import OfflineAdjudicator
from grace.adjudicate.prompt import SYSTEM, evidence_hash, format_evidence
from grace.adjudicate.schema import Decision
from grace.models import Action, Cause, Customer, Evidence, Mandate, Rail, SubStatus
from grace.policy import allowed_actions

TODAY = date(2026, 9, 4)
FIX = Path(__file__).parent / "fixtures" / "decisions"


def ev(rail=Rail.UPI_AUTOPAY, status=SubStatus.ACTIVE, *, reason=None, salary_gap=None,
       tenure=12, paid=6, attempts=0, intent=None, p_fail=0.2, in_flight=False,
       prior_fails=0, amount=49900, pause_by=None, has_invoice=None) -> Evidence:
    if has_invoice is None:
        has_invoice = status in (SubStatus.PENDING, SubStatus.HALTED)
    m = Mandate(id="simsub_a1", customer_id="c1", rail=rail, plan_amount_paise=amount,
                cycle_day=5, status=status, paid_count=paid, total_count=12,
                auth_attempts=attempts, last_error_reason=reason, pause_initiated_by=pause_by)
    c = Customer(id="c1", bank="HDFC Bank", salary_day=10, tenure_months=tenure, ltv_band="mid")
    return Evidence(
        mandate=m, customer=c, recent_events=[],
        bank_health={"td_pct": 0.42, "bd_pct": 9.1, "uptime_pct": 99.6},
        days_to_salary=salary_gap, cancel_intent_text=intent, p_fail=p_fail,
        allowed_actions=allowed_actions(rail, status, has_pending_invoice=has_invoice,
                                        pause_initiated_by=pause_by),
        has_pending_invoice=has_invoice, emandate_attempt_in_flight=in_flight,
        prior_fail_count_6m=prior_fails,
    )


@pytest.fixture()
def adj():
    return OfflineAdjudicator(today=TODAY)


CASES = {
    "liquidity_timing_upi": (
        lambda: ev(rail=Rail.UPI_AUTOPAY, status=SubStatus.PENDING, reason="insufficient_funds",
                   salary_gap=3, attempts=1),
        Cause.LIQUIDITY_TIMING, {Action.NOOP},
        "salary is 3 days away and a retry lands after it: let the ladder run",
    ),
    "liquidity_timing_salary_landed": (
        lambda: ev(rail=Rail.CARD, status=SubStatus.HALTED, reason="insufficient_funds",
                   salary_gap=0, attempts=4),
        Cause.LIQUIDITY_TIMING, {Action.MANUAL_CHARGE},
        "money has landed: charge the open invoice",
    ),
    "liquidity_structural_card": (
        lambda: ev(rail=Rail.CARD, status=SubStatus.PENDING, reason="insufficient_funds",
                   salary_gap=None, attempts=2, prior_fails=4),
        Cause.LIQUIDITY_STRUCTURAL, {Action.ESCALATE, Action.STEP_DOWN_PLAN},
        "no salary signal and repeated shortfall: do not just charge again",
    ),
    "bank_technical_early": (
        lambda: ev(status=SubStatus.PENDING, reason="bank_technical_error", attempts=1, salary_gap=8),
        Cause.BANK_TECHNICAL, {Action.NOOP},
        "technical declines usually clear on the scheduled retry",
    ),
    "instrument_invalid_emandate": (
        lambda: ev(rail=Rail.EMANDATE, status=SubStatus.HALTED,
                   reason="M021 Mandate not registered for this account", attempts=4),
        Cause.INSTRUMENT_INVALID, {Action.REQUEST_REAUTH},
        "the instrument is dead; retrying cannot fix it",
    ),
    "remap_in_flight_card": (
        lambda: ev(rail=Rail.CARD, status=SubStatus.PENDING, reason="card_expired",
                   tenure=30, prior_fails=0, attempts=1),
        Cause.REMAP_IN_FLIGHT, {Action.NOOP},
        "card_expired on a long-tenured clean payer is probably a reissue remap; do NOT churn them",
    ),
    "genuine_expiry_new_customer": (
        lambda: ev(rail=Rail.CARD, status=SubStatus.PENDING, reason="card_expired",
                   tenure=2, prior_fails=3, attempts=1),
        Cause.INSTRUMENT_INVALID, {Action.REQUEST_REAUTH},
        "same reason code, opposite decision, because the context differs",
    ),
    "limit_exceeded_upi": (
        lambda: ev(rail=Rail.UPI_AUTOPAY, status=SubStatus.PENDING, reason="payment_declined",
                   amount=1_399_900, attempts=1),
        Cause.LIMIT_EXCEEDED, {Action.ESCALATE},
        "over the cap and UPI mandates cannot be amended",
    ),
    "intent_temporary": (
        lambda: ev(intent="travelling for 2 months, dont charge me pls"),
        Cause.CUSTOMER_INTENT_TEMPORARY, {Action.PAUSE},
        "a break, not an exit",
    ),
    "intent_price_card": (
        lambda: ev(rail=Rail.CARD, intent="too expensive, do you have a cheaper plan"),
        Cause.CUSTOMER_INTENT_PRICE, {Action.STEP_DOWN_PLAN},
        "cards are the only rail where the amount can change",
    ),
    "intent_done": (
        lambda: ev(intent="course finished, please cancel permanently"),
        Cause.CUSTOMER_INTENT_DONE, {Action.CANCEL_AT_CYCLE_END},
        "honour a clear exit, but at cycle end",
    ),
    "emandate_in_flight": (
        lambda: ev(rail=Rail.EMANDATE, status=SubStatus.PENDING, in_flight=True, attempts=1),
        Cause.UNKNOWN, {Action.NOOP},
        "an unconfirmed debit is untouchable",
    ),
    "thin_evidence_catchall": (
        lambda: ev(rail=Rail.CARD, status=SubStatus.PENDING, reason="payment_failed", attempts=1),
        Cause.UNKNOWN, {Action.ESCALATE},
        "the catch-all reason code is not a diagnosis",
    ),
    "customer_paused_upi": (
        lambda: ev(rail=Rail.UPI_AUTOPAY, status=SubStatus.PAUSED, pause_by="customer"),
        Cause.UNKNOWN, {Action.NOOP},
        "we cannot resume what the customer paused",
    ),
    "healthy_mandate": (
        lambda: ev(p_fail=0.05),
        Cause.UNKNOWN, {Action.NOOP},
        "leaving a healthy mandate alone is a correct action",
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_adjudicator_case(adj, name):
    make, want_cause, want_actions, why = CASES[name]
    e = make()
    d = adj.decide(e)
    assert d.cause == want_cause, f"{name}: {why} (got cause={d.cause.value})"
    assert d.action in want_actions, f"{name}: {why} (got action={d.action.value})"
    assert d.action in e.allowed_actions or d.action == Action.ESCALATE, \
        f"{name}: proposed an action outside the rail/status matrix"
    assert d.rationale, f"{name}: every decision must carry a rationale"


def test_never_resumes_a_customer_paused_upi_mandate(adj):
    e = ev(rail=Rail.UPI_AUTOPAY, status=SubStatus.PAUSED, pause_by="customer")
    assert adj.decide(e).action != Action.RESUME


def test_never_pauses_an_authenticated_subscription(adj):
    e = ev(status=SubStatus.AUTHENTICATED, intent="please pause for a month")
    assert adj.decide(e).action != Action.PAUSE


def test_output_always_validates_and_clamps(adj):
    for make, *_ in CASES.values():
        d = adj.decide(make())
        assert isinstance(d, Decision)
        assert 0.0 <= d.cause_confidence <= 1.0 and 0.0 <= d.action_confidence <= 1.0
        assert len(d.rationale) <= 600
        if d.pause_cycles is not None:
            assert 1 <= d.pause_cycles <= 2


def test_decisions_are_deterministic(adj):
    for make, *_ in CASES.values():
        a, b = adj.decide(make()), adj.decide(make())
        assert a.model_dump() == b.model_dump()


# ------------------------------------------------------------------- prompting
def test_evidence_never_leaks_ground_truth():
    e = ev(status=SubStatus.PENDING, reason="insufficient_funds", salary_gap=3)
    blob = format_evidence(e)
    for forbidden in ("will_fail", "survival_under", "truth", "payment_will_fail",
                      "at_risk_reason", "cancel_intent\":", "propensity"):
        assert forbidden not in blob, f"evidence leaked {forbidden!r}"


def test_evidence_is_stably_ordered_so_the_cache_holds():
    e = ev(status=SubStatus.PENDING, reason="insufficient_funds", salary_gap=3)
    assert format_evidence(e) == format_evidence(e)
    assert evidence_hash(e) == evidence_hash(e)
    assert json.loads(format_evidence(e))  # valid JSON


def test_system_prompt_carries_nothing_volatile():
    """Volatile content in the system prompt silently destroys the cache."""
    for bad in ("2026-09", "simsub_", "run_id", "sub_", str(date.today().year) + "-"):
        assert bad not in SYSTEM, f"system prompt contains volatile text {bad!r}"


def test_system_prompt_states_the_rail_constraints():
    for must in ("AUTHENTICATED", "eMandate", "UPI Autopay", "15,000", "double-debit",
                 "remap", "catch-all"):
        assert must.lower() in SYSTEM.lower(), f"prompt omits {must!r}"


# ------------------------------------------------------------------- fixtures
def test_written_fixtures_match_current_behaviour(adj):
    """Recorded evidence -> decision pairs, so a behaviour change is visible in the diff."""
    FIX.mkdir(parents=True, exist_ok=True)
    for name, (make, *_rest) in CASES.items():
        e = make()
        d = adj.decide(e)
        path = FIX / f"{name}.json"
        payload = {
            "case": name, "evidence_hash": evidence_hash(e),
            "evidence": json.loads(format_evidence(e)),
            "expected": {"cause": d.cause.value, "action": d.action.value,
                         "rationale": d.rationale},
        }
        if not path.exists():
            path.write_text(json.dumps(payload, indent=2))
        recorded = json.loads(path.read_text())
        assert recorded["expected"]["cause"] == d.cause.value, f"{name}: cause drifted"
        assert recorded["expected"]["action"] == d.action.value, f"{name}: action drifted"


def test_safe_default_never_acts():
    from grace.adjudicate.claude import safe_default

    d = safe_default("network down")
    assert d.action == Action.ESCALATE and d.escalate is True
    assert d.action_confidence == 0.0
