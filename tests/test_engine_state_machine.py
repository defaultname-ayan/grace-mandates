"""The engine must reproduce Razorpay's documented state machine (spec 2.2-2.4)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from grace.models import Customer, Mandate, Rail, SubStatus
from grace.sim.engine import (
    MAX_ATTEMPTS_BEFORE_HALT,
    InvalidTransition,
    RailNotSupported,
    SimEngine,
)
from grace.store import Store
from grace.util import ensure_aware


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture()
def engine(store):
    return SimEngine(store, seed=1234, decision_date=date(2026, 9, 4))


def mk(rail=Rail.CARD, status=SubStatus.ACTIVE, **kw) -> Mandate:
    kw.setdefault("cycle_day", 5)
    kw.setdefault("plan_amount_paise", 49900)
    return Mandate(
        id=kw.pop("id", "simsub_test1"), customer_id="cust_1", rail=rail, status=status, **kw,
    )


# --------------------------------------------------------------- retry ladder
def test_retry_ladder_pends_three_times_then_halts(engine, store):
    m = mk(status=SubStatus.ACTIVE)
    m.charge_at = ensure_aware(datetime(2026, 9, 5, 10))
    store.upsert_mandate(m)
    engine._new_invoice(m, 0, m.charge_at)

    seen = []
    for _ in range(MAX_ATTEMPTS_BEFORE_HALT):
        m = engine.retry_ladder_step(m, ok=False)
        seen.append((m.auth_attempts, m.status))

    assert seen[0] == (1, SubStatus.PENDING)
    assert seen[1] == (2, SubStatus.PENDING)
    assert seen[2] == (3, SubStatus.PENDING)
    assert seen[3] == (4, SubStatus.HALTED), "4th consecutive failure must halt"
    assert m.charge_at is None, "halted subscriptions schedule no further auto-charge"

    names = [e.name for e in store.events_for(m.id)]
    assert names.count("subscription.pending") == 3
    assert names.count("subscription.halted") == 1


def test_retry_shifts_charge_at_by_one_day(engine, store):
    m = mk()
    m.charge_at = ensure_aware(datetime(2026, 9, 5, 10))
    store.upsert_mandate(m)
    engine._new_invoice(m, 0, m.charge_at)
    before = m.charge_at
    m = engine.retry_ladder_step(m, ok=False)
    assert m.charge_at == before + timedelta(days=1)


def test_success_clears_attempts_and_advances_a_month(engine, store):
    m = mk()
    m.charge_at = ensure_aware(datetime(2026, 9, 5, 10))
    store.upsert_mandate(m)
    engine._new_invoice(m, 0, m.charge_at)
    m = engine.retry_ladder_step(m, ok=False)
    assert m.auth_attempts == 1
    m = engine.retry_ladder_step(m, ok=True)
    assert m.auth_attempts == 0 and m.status == SubStatus.ACTIVE and m.paid_count == 1
    assert m.charge_at.date() == date(2026, 10, 5)


def test_charge_from_halted_is_refused(engine, store):
    m = mk(status=SubStatus.HALTED)
    store.upsert_mandate(m)
    with pytest.raises(InvalidTransition):
        engine.retry_ladder_step(m, ok=True)


# ---------------------------------------------------------------------- pause
def test_pause_requires_active(engine, store):
    m = mk(status=SubStatus.PENDING)
    store.upsert_mandate(m)
    with pytest.raises(InvalidTransition):
        engine.pause(m)


def test_pause_on_authenticated_cancels_permanently(engine, store):
    """The single most dangerous documented behaviour in the whole API."""
    m = mk(status=SubStatus.AUTHENTICATED)
    store.upsert_mandate(m)
    with pytest.raises(InvalidTransition):
        engine.pause(m)
    assert m.status == SubStatus.CANCELLED


def test_pause_then_resume_roundtrip(engine, store):
    m = mk()
    store.upsert_mandate(m)
    m = engine.pause(m)
    assert m.status == SubStatus.PAUSED
    assert m.pause_initiated_by == "self"
    assert m.charge_at is None
    m = engine.resume(m, resume_on=date(2026, 10, 7))
    assert m.status == SubStatus.ACTIVE and m.pause_initiated_by is None
    assert m.charge_at is not None and m.charge_at.date() >= date(2026, 10, 5)


def test_customer_paused_upi_cannot_be_resumed_by_merchant(engine, store):
    """Razorpay FAQ: only the customer can resume a customer-paused UPI mandate."""
    m = mk(rail=Rail.UPI_AUTOPAY)
    store.upsert_mandate(m)
    m = engine.pause(m, initiated_by="customer")
    with pytest.raises(RailNotSupported):
        engine.resume(m)


def test_merchant_paused_upi_can_be_resumed(engine, store):
    m = mk(rail=Rail.UPI_AUTOPAY)
    store.upsert_mandate(m)
    m = engine.pause(m, initiated_by="self")
    m = engine.resume(m)
    assert m.status == SubStatus.ACTIVE


# --------------------------------------------------------------------- update
@pytest.mark.parametrize("rail", [Rail.UPI_AUTOPAY, Rail.EMANDATE])
def test_update_blocked_on_non_card_rails(engine, store, rail):
    m = mk(rail=rail)
    store.upsert_mandate(m)
    with pytest.raises(RailNotSupported):
        engine.update(m, plan_amount_paise=29900)


def test_update_allowed_on_cards(engine, store):
    m = mk(rail=Rail.CARD)
    store.upsert_mandate(m)
    m = engine.update(m, plan_amount_paise=29900)
    assert m.plan_amount_paise == 29900


# --------------------------------------------------------------------- cancel
def test_cancel_at_cycle_end_defers(engine, store):
    m = mk()
    store.upsert_mandate(m)
    m = engine.cancel(m, at_cycle_end=True)
    assert m.status == SubStatus.ACTIVE, "deferred cancel must not terminate immediately"
    assert store.get_sim_state(m.id)["cancel_at_cycle_end"] is True


def test_cancel_is_terminal(engine, store):
    m = mk()
    store.upsert_mandate(m)
    m = engine.cancel(m, at_cycle_end=False)
    assert m.status == SubStatus.CANCELLED
    with pytest.raises(InvalidTransition):
        engine.cancel(m)


# ------------------------------------------------------------- double debit
def test_manual_charge_while_inflight_can_double_debit(engine, store):
    """The hazard the integrity guard exists to prevent."""
    doubles = 0
    for i in range(60):
        m = mk(rail=Rail.EMANDATE, status=SubStatus.PENDING, id=f"simsub_dd{i}")
        store.upsert_mandate(m)
        inv = engine._new_invoice(m, 0, engine.now)
        inv.attempt_in_flight = True
        store.upsert_invoice(inv)
        store.set_sim_state(m.id, {"inflight": [{
            "invoice_id": inv.id,
            "resolve_at": (engine.now + timedelta(hours=24)).isoformat(),
            "will_succeed": True, "origin": "auto",
        }]})
        _, _, double = engine.manual_charge(m, inv)
        if double:
            doubles += 1
            charged = [e for e in store.events_for(m.id) if e.name == "subscription.charged"]
            assert len(charged) == 2, "a double debit must emit two charged events"
            assert charged[0].invoice_id == charged[1].invoice_id
    assert 5 <= doubles <= 45, f"double-debit rate implausible: {doubles}/60"


def test_manual_charge_without_inflight_never_doubles(engine, store):
    for i in range(40):
        m = mk(rail=Rail.EMANDATE, status=SubStatus.PENDING, id=f"simsub_nd{i}")
        store.upsert_mandate(m)
        inv = engine._new_invoice(m, 0, engine.now)
        store.set_sim_state(m.id, {"inflight": []})
        _, _, double = engine.manual_charge(m, inv)
        assert double is False


# --------------------------------------------------------------- determinism
def test_same_seed_produces_identical_history(tmp_path):
    def run(path):
        s = Store(path)
        e = SimEngine(s, seed=99, decision_date=date(2026, 9, 4))
        m = mk(id="simsub_det")
        c = Customer(id="cust_1", bank="HDFC Bank", salary_day=1, tenure_months=10, ltv_band="mid")
        m, st = e.build_history(m, c)
        evs = [(ev.name, ev.at.isoformat(), ev.error_code) for ev in s.events_for(m.id)]
        s.close()
        return evs, st

    a, sa = run(tmp_path / "a.db")
    b, sb = run(tmp_path / "b.db")
    assert a == b and sa == sb
    assert len(a) >= 2


def test_history_leaves_mandate_alive_and_paying(tmp_path):
    s = Store(tmp_path / "h.db")
    e = SimEngine(s, seed=7, decision_date=date(2026, 9, 4))
    m = mk(id="simsub_hist")
    c = Customer(id="cust_1", bank="State Bank of India", salary_day=1, tenure_months=3, ltv_band="low")
    m, st = e.build_history(m, c)
    assert m.status == SubStatus.ACTIVE
    assert m.paid_count >= 1
    assert st["prior_fail_count_6m"] >= 0
    assert 0.0 < st["propensity"] < 1.0
    s.close()


def test_emandate_history_avoids_bank_holidays(tmp_path):
    """Razorpay charges eMandate on T-1 when T is a bank holiday."""
    from grace.signals.holidays import HolidayCalendar

    cal = HolidayCalendar()
    s = Store(tmp_path / "e.db")
    e = SimEngine(s, seed=5, decision_date=date(2026, 9, 4))
    # cycle_day 13 lands on Sundays/Saturdays across the window
    m = mk(rail=Rail.EMANDATE, id="simsub_em", cycle_day=13)
    c = Customer(id="cust_1", bank="HDFC Bank", salary_day=1, tenure_months=8, ltv_band="mid")
    m, _ = e.build_history(m, c)
    charged = [ev for ev in s.events_for(m.id) if ev.name in ("subscription.charged", "subscription.pending")]
    first_attempts = [ev for ev in charged if ev.name == "subscription.pending"]
    for ev in first_attempts:
        assert not cal.is_bank_holiday(ev.at.date()), f"eMandate attempted on a holiday: {ev.at.date()}"
    s.close()
