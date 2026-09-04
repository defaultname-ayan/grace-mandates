"""LiveClient construction and its refusals. No network: the razorpay SDK
builds its client lazily, so constructing it does not call Razorpay.

This test exists because a rewrite once deleted `_client()` and a later
lint fix removed its `import os`; nothing constructed LiveClient in tests, so
both shipped. Construction is now exercised on every run.
"""
from __future__ import annotations

import pytest

razorpay = pytest.importorskip("razorpay")


@pytest.fixture()
def test_keys(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fakefakefake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fakesecret")


def test_live_client_constructs_with_a_test_key(test_keys):
    from grace.rzp.live import LiveClient

    c = LiveClient()
    assert type(c.c).__name__ == "Client"


def test_live_key_is_refused_before_any_call(monkeypatch):
    from grace.rzp.live import _client

    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_realkey")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "x")
    with pytest.raises(RuntimeError, match="non-test key"):
        _client()


def test_missing_keys_give_a_useful_message(monkeypatch):
    from grace.rzp.live import _client

    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    with pytest.raises(RuntimeError, match=r"\.env\.example"):
        _client()


def test_update_refuses_amounts_and_non_card_rails_without_http(test_keys):
    """Razorpay's PATCH takes plan_id, never an amount, and cannot touch
    UPI/eMandate subscriptions. Both must be refused before any HTTP call."""
    from grace.rzp.live import LiveClient

    c = LiveClient()
    r = c.update("sub_x", plan_amount_paise=29900)
    assert r.ok is False and "plan_id" in (r.error or "")
    r = c.update("sub_x", rail="upi", plan_id="plan_y")
    assert r.ok is False and "upi" in (r.error or "")


def test_charge_invoice_is_honestly_unimplemented(test_keys):
    from grace.rzp.live import LiveClient

    with pytest.raises(NotImplementedError, match="dashboard"):
        LiveClient().charge_invoice("sub_x", "inv_y")


def test_subscriptions_probe_goes_through_the_guarded_client(test_keys, monkeypatch):
    """A raw-HTTP probe once bypassed the rzp_test guard. The probe must use
    the client's own SDK object, never re-read the environment."""
    from grace.rzp.live import LiveClient, subscriptions_enabled

    c = LiveClient()
    calls: list[str] = []

    class _Payments:
        def all(self, _q):
            calls.append("payments"); return {"items": []}

    class _Plans:
        def all(self, _q):
            calls.append("plans"); raise RuntimeError("Unauthorized")

    monkeypatch.setattr(c.c, "payment", _Payments())
    monkeypatch.setattr(c.c, "plan", _Plans())
    ok, why = subscriptions_enabled(c)
    assert ok is False and "NOT enabled" in why
    assert calls == ["payments", "plans"], "probe must use the SDK client, in order"
