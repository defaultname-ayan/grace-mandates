"""Webhook receiver (spec 10.4).

Two rules that Razorpay's docs are explicit about and that people get wrong:
verify the signature against the RAW body before parsing anything, and treat
delivery as at-least-once and out-of-order.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

from fastapi import APIRouter, HTTPException, Request

from grace.models import Event
from grace.util import utcnow

router = APIRouter()


def verify(raw: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(expected, signature or "")
    except TypeError:  # non-ASCII header: definitely not our hex digest
        return False


def event_from_payload(body: dict, raw: bytes, event_id: str | None = None) -> Event:
    inner = body.get("payload", body)
    sub = ((inner.get("subscription") or {}).get("entity") or {})
    name = body.get("event") or "unknown"
    eid = event_id or body.get("id") or hashlib.sha256(raw).hexdigest()[:24]
    created = body.get("created_at")
    at = utcnow()
    if isinstance(created, int):
        from datetime import datetime, timezone

        at = datetime.fromtimestamp(created, tz=timezone.utc)
    return Event(id=eid, mandate_id=sub.get("id", "unknown"), name=name, at=at, payload=inner)


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="RAZORPAY_WEBHOOK_SECRET not configured")

    raw = await request.body()  # RAW body: never parse before verifying
    sig = request.headers.get("X-Razorpay-Signature", "")
    if not verify(raw, sig, secret):
        raise HTTPException(status_code=400, detail="bad signature")

    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="body is not JSON") from None
    ev = event_from_payload(body, raw, request.headers.get("x-razorpay-event-id"))

    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="no run loaded; run `grace seed` first")
    inserted = store.append_event(ev)
    if inserted:
        guard = request.app.state.guard
        if guard is not None and guard.on_event(ev):
            return {"ok": True, "duplicate": False, "incident": "DOUBLE_DEBIT_DETECTED"}
    return {"ok": True, "duplicate": not inserted, "event": ev.name, "mandate_id": ev.mandate_id}
