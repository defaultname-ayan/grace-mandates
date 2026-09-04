"""Async double-debit guard (spec 11).

Razorpay's own retry documentation is what makes this necessary: for eMandate,
"we attempt to retry only when we get the confirmation or rejection of the last
payment, as it may take more than 24 hours". Anything that charges an invoice
inside that window can debit the customer twice. A double debit is not a missed
optimisation; it is a compliance incident.

The guard is defence in depth. The adjudicator can already see
`emandate_attempt_in_flight` and should avoid the hazard by itself; this class
assumes it will not always.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from grace.models import Invoice, Mandate, Rail
from grace.util import ensure_aware, utcnow

LOCK_TTL = timedelta(hours=72)
PAUSED_CARD_TOKEN_RISK_DAYS = 60


class IntegrityGuard:
    def __init__(self, store, enabled: bool = True, now: datetime | None = None):
        self.store = store
        self.enabled = enabled
        self._now = ensure_aware(now)
        self.blocked: list[dict] = []

    @property
    def now(self) -> datetime:
        return self._now or utcnow()

    # ------------------------------------------------------------- checks
    def check_manual_charge(self, m: Mandate, invoice: Invoice) -> tuple[bool, str]:
        if not self.enabled:
            return True, "guard disabled (--no-guard)"

        if m.rail == Rail.EMANDATE and invoice.attempt_in_flight:
            return self._block(m, invoice, "emandate attempt in flight; confirmation not yet received")

        if any(e.name == "subscription.charged" for e in self.store.events_for_invoice(invoice.id)):
            return self._block(m, invoice, "invoice already charged")

        if m.rail != Rail.EMANDATE and self.store.retry_due_within(m, hours=24, now=self.now):
            return self._block(m, invoice, "an automatic retry is scheduled within 24h; let it run")

        if self.store.is_locked(invoice.id):
            return self._block(m, invoice, "another action is already in progress on this invoice")

        self.store.lock(invoice.id, LOCK_TTL)
        return True, "ok"

    def check_resume(self, m: Mandate) -> tuple[bool, str]:
        if not self.enabled:
            return True, "guard disabled (--no-guard)"
        if m.rail == Rail.UPI_AUTOPAY and m.pause_initiated_by == "customer":
            return False, "customer-paused UPI mandate; only the customer can resume it"
        if (m.rail == Rail.CARD and m.paused_at
                and self.now - ensure_aware(m.paused_at) > timedelta(days=PAUSED_CARD_TOKEN_RISK_DAYS)):
            return False, (f"card token validity uncertain after {PAUSED_CARD_TOKEN_RISK_DAYS} days "
                           f"paused; resume may fail")
        return True, "ok"

    def _block(self, m: Mandate, invoice: Invoice, why: str) -> tuple[bool, str]:
        self.blocked.append({
            "mandate_id": m.id, "invoice_id": invoice.id, "rail": m.rail.value,
            "reason": why, "amount_paise": invoice.amount_paise,
            "attempt_in_flight": invoice.attempt_in_flight,
        })
        return False, why

    # -------------------------------------------------------------- events
    def on_event(self, event) -> bool:
        """Watch the event stream for an actual double debit. Returns True if
        one was detected. Never auto-refunds: it opens a ticket and escalates."""
        if event.name != "subscription.charged":
            return False
        inv = event.invoice_id
        if not inv:
            return False
        prior = [e for e in self.store.events_for_invoice(inv)
                 if e.name == "subscription.charged" and e.id != event.id]
        detected = bool(prior)
        if detected:
            self.store.append_audit(
                phase="incident", mandate_id=event.mandate_id,
                kind="DOUBLE_DEBIT_DETECTED", invoice_id=inv,
                event_ids=[event.id, *[p.id for p in prior]],
                action="open_refund_ticket", auto_refund=False, escalate=True,
                note="Refund is a human decision. Grace opens a ticket and stops.",
            )
        self.store.unlock(inv)
        return detected

    def scan_for_double_debits(self, mandate_id: str) -> int:
        """Count invoices of this mandate that carry more than one charged event."""
        seen: dict[str, int] = {}
        for e in self.store.events_for(mandate_id):
            if e.name == "subscription.charged" and e.invoice_id:
                seen[e.invoice_id] = seen.get(e.invoice_id, 0) + 1
        doubles = sum(1 for n in seen.values() if n > 1)
        for inv, n in seen.items():
            if n > 1:
                self.store.append_audit(
                    phase="incident", mandate_id=mandate_id, kind="DOUBLE_DEBIT_DETECTED",
                    invoice_id=inv, charged_count=n, action="open_refund_ticket",
                    auto_refund=False, escalate=True,
                )
        return doubles
