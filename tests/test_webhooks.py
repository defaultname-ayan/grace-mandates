"""Webhook handling: signature on the raw body, idempotent, order-tolerant."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from grace.rzp.webhooks import event_from_payload, verify
from grace.store import Store

FIX = Path(__file__).parent / "fixtures" / "webhooks"
SECRET = "whsec_test"


def sign(raw: bytes) -> str:
    return hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def test_signature_is_verified_against_the_raw_body():
    raw = b'{"event":"subscription.paused","payload":{}}'
    assert verify(raw, sign(raw), SECRET) is True
    assert verify(raw + b" ", sign(raw), SECRET) is False, "re-serialised body must not verify"
    assert verify(raw, "", SECRET) is False
    assert verify(raw, "deadbeef", SECRET) is False


def test_signature_comparison_is_constant_time():
    import inspect

    assert "compare_digest" in inspect.getsource(verify)


@pytest.mark.parametrize("path", sorted(FIX.glob("*.json")))
def test_every_fixture_parses_into_an_event(path):
    body = json.loads(path.read_text())
    ev = event_from_payload(body, path.read_bytes())
    assert ev.name == body["event"]
    assert ev.mandate_id.startswith("sub_")
    assert ev.at is not None and ev.at.tzinfo is not None


def test_fixtures_cover_the_lifecycle():
    names = {p.stem for p in FIX.glob("*.json")}
    for required in ("subscription.charged", "subscription.pending", "subscription.halted",
                     "subscription.paused", "subscription.resumed", "subscription.cancelled"):
        assert required in names, f"missing fixture {required}"


def test_duplicate_delivery_is_a_no_op(tmp_path):
    s = Store(tmp_path / "w.db")
    try:
        raw = (FIX / "subscription.charged.json").read_bytes()
        ev = event_from_payload(json.loads(raw), raw, event_id="evt_dup_1")
        assert s.append_event(ev) is True
        assert s.append_event(ev) is False, "at-least-once delivery must be idempotent"
        assert len(s.events_for(ev.mandate_id)) == 1
    finally:
        s.close()


def test_out_of_order_delivery_is_tolerated(tmp_path):
    """Razorpay documents payment.failed arriving after payment.captured."""
    s = Store(tmp_path / "o.db")
    try:
        pend = (FIX / "subscription.pending.json").read_bytes()
        chg = (FIX / "subscription.charged.json").read_bytes()
        e_late = event_from_payload(json.loads(pend), pend, event_id="evt_late")
        e_early = event_from_payload(json.loads(chg), chg, event_id="evt_early")
        s.append_event(e_late)
        s.append_event(e_early)
        got = s.events_for(e_late.mandate_id)
        assert len(got) == 2
        assert [e.at for e in got] == sorted(e.at for e in got), "events must read back in time order"
    finally:
        s.close()


def test_payment_error_fields_survive_the_round_trip(tmp_path):
    s = Store(tmp_path / "p.db")
    try:
        raw = (FIX / "subscription.pending.json").read_bytes()
        ev = event_from_payload(json.loads(raw), raw, event_id="evt_err")
        s.append_event(ev)
        back = s.events_for(ev.mandate_id)[0]
        assert back.error_code == "insufficient_funds"
        assert back.invoice_id is not None
    finally:
        s.close()
