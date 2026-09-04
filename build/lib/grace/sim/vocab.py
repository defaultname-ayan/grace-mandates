"""Failure-reason vocabularies (spec 2.8), and the cause -> reason mapping.

The point of keeping three separate vocabularies is that they are genuinely
different and they move. NPCI rewrote the NACH set in Jan 2025 (20 added, 33
revised, 22 removed). A hardcoded reason->action table goes stale by design.
"""
from __future__ import annotations

import json
from pathlib import Path

from grace.models import Cause, Rail

# Razorpay /docs/errors/payments/cards/
CARD_REASONS = [
    "payment_timed_out", "gateway_technical_error", "payment_cancelled", "card_declined",
    "insufficient_funds", "card_not_enrolled", "bank_technical_error",
    "card_disabled_for_online_payments", "authentication_failed", "payment_risk_check_failed",
    "payment_failed", "incorrect_cvv", "debit_instrument_inactive", "debit_instrument_blocked",
    "card_expired", "transaction_limit_exceeded",
]

# Razorpay /docs/errors/payments/upi/
UPI_REASONS = [
    "bank_technical_error", "credit_failed", "customer bank account mismatch",
    "gateway_technical_error", "insufficient_funds", "invalid_vpa", "payment_cancelled",
    "payment_collect_request_expired", "payment_declined", "payment_timed_out",
    "vpa_resolution_failed",
]

_NACH_PATH = Path(__file__).resolve().parents[1] / "data" / "nach_reason_codes.json"


def nach_codes() -> list[dict]:
    if not _NACH_PATH.exists():
        return []
    return json.loads(_NACH_PATH.read_text()).get("codes", [])


def _nach_by_family(family: str) -> list[str]:
    return [f"{c['code']} {c['description']}" for c in nach_codes() if c["family"] == family]


#: cause -> candidate raw reasons, per rail. remap_in_flight deliberately
#: surfaces as `card_expired`: it is indistinguishable from a genuine expiry at
#: the reason-code level, which is precisely the trap.
CAUSE_REASONS: dict[Rail, dict[Cause, list[str]]] = {
    Rail.UPI_AUTOPAY: {
        Cause.LIQUIDITY_TIMING: ["insufficient_funds"],
        Cause.LIQUIDITY_STRUCTURAL: ["insufficient_funds"],
        Cause.BANK_TECHNICAL: ["bank_technical_error", "payment_timed_out", "gateway_technical_error"],
        Cause.INSTRUMENT_INVALID: ["invalid_vpa", "vpa_resolution_failed"],
        Cause.LIMIT_EXCEEDED: ["payment_declined"],
        Cause.UNKNOWN: ["payment_declined", "credit_failed"],
    },
    Rail.CARD: {
        Cause.LIQUIDITY_TIMING: ["insufficient_funds"],
        Cause.LIQUIDITY_STRUCTURAL: ["insufficient_funds"],
        Cause.BANK_TECHNICAL: ["bank_technical_error", "payment_timed_out", "gateway_technical_error"],
        Cause.INSTRUMENT_INVALID: ["card_expired", "debit_instrument_blocked", "debit_instrument_inactive"],
        Cause.LIMIT_EXCEEDED: ["transaction_limit_exceeded"],
        Cause.REMAP_IN_FLIGHT: ["card_expired"],
        Cause.UNKNOWN: ["payment_failed", "card_declined"],
    },
    Rail.EMANDATE: {
        Cause.LIQUIDITY_TIMING: _nach_by_family("liquidity") or ["M006 Insufficient funds in the account"],
        Cause.LIQUIDITY_STRUCTURAL: _nach_by_family("liquidity") or ["M006 Insufficient funds in the account"],
        Cause.BANK_TECHNICAL: _nach_by_family("technical") or ["M045 Technical failure at destination bank"],
        Cause.INSTRUMENT_INVALID: _nach_by_family("instrument") or ["M021 Mandate not registered for this account"],
        Cause.LIMIT_EXCEEDED: _nach_by_family("limit") or ["M028 Debit amount exceeds the mandate limit"],
        Cause.UNKNOWN: ["M045 Technical failure at destination bank"],
    },
}

#: Coarse family a raw reason belongs to. Used by the rules baseline and the
#: predictor. Intentionally lossy.
LIQUIDITY_TOKENS = ("insufficient_funds", "insufficient funds")
TECHNICAL_TOKENS = ("technical_error", "timed_out", "technical failure", "gateway", "uidai otp", "collect_request_expired")
INSTRUMENT_TOKENS = ("card_expired", "invalid_vpa", "vpa_resolution", "instrument_blocked",
                     "instrument_inactive", "not_enrolled", "account closed", "account frozen",
                     "mandate not registered", "not linked with given debit card",
                     "mobile number not available", "card_disabled")
LIMIT_TOKENS = ("limit_exceeded", "exceeds the mandate limit", "payment_declined")
CUSTOMER_TOKENS = ("payment_cancelled", "stopped by drawer", "cancelled by customer")


def reason_family(reason: str | None) -> str:
    """Map a raw reason string to liquidity | technical | instrument | limit | customer | other."""
    if not reason:
        return "none"
    r = reason.lower()
    for tokens, name in (
        (LIQUIDITY_TOKENS, "liquidity"),
        (INSTRUMENT_TOKENS, "instrument"),
        (TECHNICAL_TOKENS, "technical"),
        (CUSTOMER_TOKENS, "customer"),
        (LIMIT_TOKENS, "limit"),
    ):
        if any(t in r for t in tokens):
            return name
    return "other"


def describe(reason: str) -> str:
    """Human-readable error_description, as Razorpay returns alongside error_code."""
    return reason.replace("_", " ").capitalize()
