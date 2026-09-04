"""The guard must make a double debit unreachable (spec 11)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from grace.integrity import IntegrityGuard
from grace.models import Invoice, Mandate, Rail, SubStatus
from grace.sim.engine import SimEngine
from grace.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "g.db")
    yield s
    s.close()


@pytest.fixture()
def engine(store):
    return SimEngine(store, seed=42, decision_date=date(2026, 9, 4))


def mandate(rail=Rail.EMANDATE, status=SubStatus.PENDING, **kw) -> Mandate:
    kw.setdefault("cycle_day", 5)
    return Mandate(id=kw.pop("id", "simsub_g1"), customer_id="c1", rail=rail,
                   plan_amount_paise=49900, status=status, **kw)


def inflight_invoice(store, m, engine, in_flight=True) -> Invoice:
    inv = engine._new_invoice(m, 0, engine.now)
    inv.attempt_in_flight = in_flight
    store.upsert_invoice(inv)
    return inv


def test_blocks_charge_while_emandate_confirmation_pending(store, engine):
    m = mandate()
    store.upsert_mandate(m)
    inv = inflight_invoice(store, m, engine)
    g = IntegrityGuard(store, now=engine.now)
    ok, why = g.check_manual_charge(m, inv)
    assert ok is False and "in flight" in why
    assert len(g.blocked) == 1


def test_allows_charge_when_no_attempt_in_flight(store, engine):
    m = mandate()
    store.upsert_mandate(m)
    inv = inflight_invoice(store, m, engine, in_flight=False)
    ok, why = IntegrityGuard(store, now=engine.now).check_manual_charge(m, inv)
    assert ok is True and why == "ok"


def test_blocks_second_charge_on_an_already_charged_invoice(store, engine):
    m = mandate(rail=Rail.CARD, status=SubStatus.HALTED)
    store.upsert_mandate(m)
    inv = inflight_invoice(store, m, engine, in_flight=False)
    engine.emit(m, "subscription.charged", engine.now,
                engine.payment_entity(m, inv.id, True, None, engine.now))
    ok, why = IntegrityGuard(store, now=engine.now).check_manual_charge(m, inv)
    assert ok is False and "already charged" in why


def test_blocks_when_an_automatic_retry_is_imminent(store, engine):
    m = mandate(rail=Rail.CARD, status=SubStatus.PENDING)
    m.charge_at = engine.now + timedelta(hours=6)
    store.upsert_mandate(m)
    inv = inflight_invoice(store, m, engine, in_flight=False)
    ok, why = IntegrityGuard(store, now=engine.now).check_manual_charge(m, inv)
    assert ok is False and "retry is scheduled" in why


def test_check_is_pure_and_lock_is_explicit(store, engine):
    """A check that locked as a side effect left invoices locked for 72h
    whenever a later gate step denied the action."""
    m = mandate(rail=Rail.CARD, status=SubStatus.HALTED)
    store.upsert_mandate(m)
    inv = inflight_invoice(store, m, engine, in_flight=False)
    g = IntegrityGuard(store, now=engine.now)
    assert g.check_manual_charge(m, inv)[0] is True
    assert g.check_manual_charge(m, inv)[0] is True, "checking twice must not lock"
    assert g.acquire(inv.id) is True
    ok, why = g.check_manual_charge(m, inv)
    assert ok is False and "already in progress" in why
    g.release(inv.id)
    assert g.check_manual_charge(m, inv)[0] is True


def test_customer_paused_upi_cannot_be_resumed(store, engine):
    m = mandate(rail=Rail.UPI_AUTOPAY, status=SubStatus.PAUSED)
    m.pause_initiated_by = "customer"
    ok, why = IntegrityGuard(store, now=engine.now).check_resume(m)
    assert ok is False and "only the customer" in why


def test_long_paused_card_resume_is_blocked(store, engine):
    m = mandate(rail=Rail.CARD, status=SubStatus.PAUSED)
    m.pause_initiated_by = "self"
    m.paused_at = engine.now - timedelta(days=90)
    ok, why = IntegrityGuard(store, now=engine.now).check_resume(m)
    assert ok is False and "token validity" in why


def test_disabled_guard_allows_the_hazard(store, engine):
    """--no-guard must genuinely remove protection, or the counterfactual lies."""
    m = mandate()
    store.upsert_mandate(m)
    inv = inflight_invoice(store, m, engine)
    ok, why = IntegrityGuard(store, enabled=False, now=engine.now).check_manual_charge(m, inv)
    assert ok is True and "disabled" in why


def test_guard_makes_double_debit_unreachable_end_to_end(store, engine):
    """With the guard on, no manual charge lands while in flight, so no double
    debit is possible. With it off, some do."""
    prevented = doubles_with_guard = doubles_without = 0

    for i in range(50):
        m = mandate(id=f"simsub_on{i}")
        store.upsert_mandate(m)
        inv = inflight_invoice(store, m, engine)
        store.set_sim_state(m.id, {"inflight": [{
            "invoice_id": inv.id, "resolve_at": (engine.now + timedelta(hours=24)).isoformat(),
            "will_succeed": True, "origin": "auto"}]})
        g = IntegrityGuard(store, enabled=True, now=engine.now)
        ok, _ = g.check_manual_charge(m, inv)
        if ok:
            engine.manual_charge(m, inv)
        else:
            prevented += 1
        doubles_with_guard += g.scan_for_double_debits(m.id)

    for i in range(50):
        m = mandate(id=f"simsub_off{i}")
        store.upsert_mandate(m)
        inv = inflight_invoice(store, m, engine)
        store.set_sim_state(m.id, {"inflight": [{
            "invoice_id": inv.id, "resolve_at": (engine.now + timedelta(hours=24)).isoformat(),
            "will_succeed": True, "origin": "auto"}]})
        g = IntegrityGuard(store, enabled=False, now=engine.now)
        ok, _ = g.check_manual_charge(m, inv)
        assert ok
        engine.manual_charge(m, inv)
        doubles_without += g.scan_for_double_debits(m.id)

    assert prevented == 50, "guard must block every in-flight charge"
    assert doubles_with_guard == 0, "no double debit may occur with the guard on"
    assert doubles_without > 0, "the counterfactual must actually be dangerous"


def test_on_event_opens_a_ticket_and_never_auto_refunds(store, engine):
    m = mandate(rail=Rail.CARD, status=SubStatus.ACTIVE)
    store.upsert_mandate(m)
    inv = inflight_invoice(store, m, engine, in_flight=False)
    g = IntegrityGuard(store, now=engine.now)
    # Events arrive one at a time, as webhooks do.
    e1 = engine.emit(m, "subscription.charged", engine.now,
                     engine.payment_entity(m, inv.id, True, None, engine.now), suffix="a")
    assert g.on_event(e1) is False, "the first charge on an invoice is not a double debit"
    e2 = engine.emit(m, "subscription.charged", engine.now + timedelta(minutes=5),
                     engine.payment_entity(m, inv.id, True, None, engine.now, suffix="b"), suffix="b")
    assert g.on_event(e2) is True, "the second charge on the same invoice is"
    incidents = store.audit_by_phase("incident")
    assert incidents and incidents[-1]["kind"] == "DOUBLE_DEBIT_DETECTED"
    assert incidents[-1]["auto_refund"] is False
