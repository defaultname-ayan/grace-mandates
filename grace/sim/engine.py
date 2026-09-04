"""Razorpay-faithful subscription simulator (spec 10.2).

Why a simulator at all: Razorpay test mode cannot force individual decline
reasons, its subscription tokens expire in 3 days, and it will not manufacture
an eMandate confirmation race. None of the interesting decisions can be
exercised live. The simulator reproduces the documented state machine (spec
2.2), the documented retry ladder (spec 2.3) and the documented rail capability
matrix (spec 2.4) so the batch can be evaluated; `grace live-demo` then proves
the same actions against the real test-mode API.

Everything here is seeded. Two runs with the same seed produce byte-identical
event streams.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from grace.models import (
    Cause,
    Customer,
    Event,
    Invoice,
    Mandate,
    Rail,
    SubStatus,
    Truth,
)
from grace.signals.bank_health import BankHealth
from grace.signals.holidays import HolidayCalendar, default_calendar
from grace.sim.vocab import CAUSE_REASONS, describe
from grace.store import Store
from grace.util import add_months, ensure_aware, on_cycle_day, stable_hash, to_iso

#: Documented retry ladder: attempt at T, retries at T+1, T+2, T+3, then halted.
MAX_ATTEMPTS_BEFORE_HALT = 4

#: eMandate confirmation delay distribution (spec 10.2).
EMANDATE_CONFIRM_DELAYS = [(24, 0.55), (48, 0.30), (72, 0.15)]

#: Probability that a manual charge issued while an eMandate attempt is still
#: in flight results in BOTH debits landing (spec 10.2). This is the hazard.
DOUBLE_DEBIT_PROB = 0.35


class RailNotSupported(RuntimeError):
    """Raised when an action is attempted on a rail that cannot support it."""


class InvalidTransition(RuntimeError):
    """Raised when an action is attempted from a state that forbids it."""


def _pick(rng: random.Random, options: list[str]) -> str:
    return options[rng.randrange(len(options))]


class SimEngine:
    def __init__(
        self,
        store: Store,
        seed: int,
        bank_health: BankHealth | None = None,
        calendar: HolidayCalendar | None = None,
        decision_date: date | None = None,
    ):
        self.store = store
        self.seed = seed
        self.bank_health = bank_health or BankHealth()
        self.calendar = calendar or default_calendar()
        self.decision_date = decision_date or date(2026, 9, 4)
        self.now = ensure_aware(
            datetime(self.decision_date.year, self.decision_date.month, self.decision_date.day, 12, 0, 0)
        )

    # ------------------------------------------------------------------ rng
    def rng(self, *parts: object) -> random.Random:
        return random.Random(stable_hash(self.seed, *parts))

    # ------------------------------------------------- Razorpay-shaped JSON
    def subscription_entity(self, m: Mandate, plan_id: str | None = None) -> dict:
        """Mirrors the subscription entity in Razorpay's webhook payloads (spec 2.5)."""
        return {
            "id": m.id,
            "entity": "subscription",
            "plan_id": plan_id or f"plan_{m.id[-12:]}",
            "customer_id": m.customer_id,
            "status": m.status.value,
            "type": 1,
            "current_start": int(m.charge_at.timestamp()) if m.charge_at else None,
            "current_end": None,
            "ended_at": None,
            "quantity": 1,
            "notes": {"grace_rail": m.rail.value},
            "charge_at": int(m.charge_at.timestamp()) if m.charge_at else None,
            "start_at": None,
            "end_at": None,
            "auth_attempts": m.auth_attempts,
            "total_count": m.total_count,
            "paid_count": m.paid_count,
            "customer_notify": True,
            "created_at": None,
            "expire_by": None,
            "short_url": None,
            "has_scheduled_changes": False,
            "change_scheduled_at": None,
            "source": "api",
            "offer_id": None,
            "payment_method": m.rail.value,
            "remaining_count": m.remaining_count,
            "pause_initiated_by": m.pause_initiated_by,
            "cancel_initiated_by": None,
            "paused_at": int(m.paused_at.timestamp()) if m.paused_at else None,
        }

    def payment_entity(
        self, m: Mandate, invoice_id: str, ok: bool, reason: str | None, at: datetime, suffix: str = ""
    ) -> dict:
        code = None if ok else reason
        return {
            "id": f"pay_{abs(stable_hash(m.id, invoice_id, at.isoformat(), suffix)) % 10**14:014d}",
            "entity": "payment",
            "amount": m.plan_amount_paise,
            "currency": "INR",
            "status": "captured" if ok else "failed",
            "invoice_id": invoice_id,
            "method": m.rail.value,
            "description": "Recurring Payment via Subscription",
            "customer_id": m.customer_id,
            "error_code": code,
            "error_description": describe(code) if code else None,
            "error_source": None if ok else ("customer" if reason and "insufficient" in reason.lower() else "bank"),
            "error_step": None if ok else "payment_authorization",
            "created_at": int(at.timestamp()),
        }

    def emit(
        self, m: Mandate, name: str, at: datetime, payment: dict | None = None, suffix: str = ""
    ) -> Event:
        at = ensure_aware(at)
        eid = f"evt_{abs(stable_hash(m.id, name, at.isoformat(), suffix)) % 10**14:014d}"
        payload: dict = {
            "entity": "event",
            "event": name,
            "contains": ["subscription"] + (["payment"] if payment else []),
            "payload": {"subscription": {"entity": self.subscription_entity(m)}},
            "created_at": int(at.timestamp()),
        }
        if payment:
            payload["payload"]["payment"] = {"entity": payment}
        ev = Event(id=eid, mandate_id=m.id, name=name, at=at, payload=payload["payload"])
        # Keep the full Razorpay envelope for fixtures/replay while exposing the
        # inner payload (which is what the adjudicator reads) on Event.payload.
        ev.payload["_envelope"] = {k: payload[k] for k in ("entity", "event", "contains", "created_at")}
        self.store.append_event(ev)
        return ev

    # -------------------------------------------------------- state helpers
    def _invoice_id(self, m: Mandate, cycle: int) -> str:
        return f"inv_{abs(stable_hash(m.id, cycle)) % 10**14:014d}"

    def _new_invoice(self, m: Mandate, cycle: int, at: datetime) -> Invoice:
        inv = Invoice(
            id=self._invoice_id(m, cycle),
            mandate_id=m.id,
            cycle_index=cycle,
            amount_paise=m.plan_amount_paise,
            status="issued",
            issued_at=at,
        )
        self.store.upsert_invoice(inv)
        return inv

    def _confirm_delay_hours(self, rng: random.Random) -> int:
        u = rng.random()
        acc = 0.0
        for hours, p in EMANDATE_CONFIRM_DELAYS:
            acc += p
            if u <= acc:
                return hours
        return EMANDATE_CONFIRM_DELAYS[-1][0]

    # ------------------------------------------------------------- history
    def build_history(
        self, m: Mandate, cust: Customer, months: int = 6
    ) -> tuple[Mandate, dict]:
        """Generate `months` of state-machine-consistent history ending just
        before the current cycle. Returns the mandate and its sim state.

        History failures always recover by the final retry: a mandate that
        halted months ago would present no decision today, so the cohort is
        alive at the decision point by construction. Documented in the manifest.

        Deliberately takes no `truth`: history is driven by the latent
        propensity alone, and the signature should make that visible.
        """
        rng = self.rng("history", m.id)
        propensity = self._propensity(m, cust)
        start = add_months(self.decision_date, -months)

        m.status = SubStatus.CREATED
        auth_at = ensure_aware(datetime.combine(start, datetime.min.time()))
        self.emit(m, "subscription.authenticated", auth_at)
        m.status = SubStatus.AUTHENTICATED
        self.emit(m, "subscription.activated", auth_at + timedelta(minutes=1))
        m.status = SubStatus.ACTIVE

        success_dates: list[date] = []
        prior_fail_count = 0
        for cycle in range(months):
            cycle_date = on_cycle_day(add_months(start, cycle), m.cycle_day)
            if cycle_date >= self.decision_date:
                break
            # eMandate is charged on T-1 when T is a bank holiday (spec 2.3).
            if m.rail == Rail.EMANDATE and self.calendar.is_bank_holiday(cycle_date):
                cycle_date = self.calendar.previous_business_day(cycle_date)
            at = ensure_aware(datetime.combine(cycle_date, datetime.min.time()) + timedelta(hours=10))
            inv = self._new_invoice(m, cycle, at)

            failed_first = rng.random() < propensity
            if failed_first:
                prior_fail_count += 1
                reason = _pick(rng, CAUSE_REASONS[m.rail].get(Cause.LIQUIDITY_TIMING, ["payment_failed"]))
                if rng.random() < 0.45:
                    reason = _pick(
                        rng, CAUSE_REASONS[m.rail].get(Cause.BANK_TECHNICAL, ["payment_failed"])
                    )
                m.auth_attempts = 1
                m.status = SubStatus.PENDING
                m.last_error_reason = reason
                m.charge_at = at + timedelta(days=1)
                inv.attempts = 1
                inv.status = "failed"
                self.store.upsert_invoice(inv)
                self.emit(m, "subscription.pending", at,
                          self.payment_entity(m, inv.id, False, reason, at))
                # Recovery on retry 1..3 (history never halts).
                recover_on = 1 + int(rng.random() * 3)
                rec_at = at + timedelta(days=recover_on)
                m.auth_attempts = 0
                m.status = SubStatus.ACTIVE
                m.paid_count += 1
                m.last_error_reason = None
                inv.status = "paid"
                inv.attempts = recover_on + 1
                self.store.upsert_invoice(inv)
                self.emit(m, "subscription.charged", rec_at,
                          self.payment_entity(m, inv.id, True, None, rec_at), suffix="recover")
                success_dates.append(rec_at.date())
            else:
                m.paid_count += 1
                inv.status = "paid"
                inv.attempts = 1
                self.store.upsert_invoice(inv)
                self.emit(m, "subscription.charged", at,
                          self.payment_entity(m, inv.id, True, None, at))
                success_dates.append(cycle_date)

        sim_state = {
            "next_cycle_index": months,
            "propensity": round(propensity, 4),
            "prior_fail_count_6m": prior_fail_count,
            "success_dates": [d.isoformat() for d in success_dates],
            "inflight": [],
            "cancel_at_cycle_end": False,
            "scheduled_resume_at": None,
        }
        return m, sim_state

    def _propensity(self, m: Mandate, cust: Customer) -> float:
        """Latent per-mandate probability that a given historical cycle fails.

        Driven by observable-ish drivers (bank health, rail, amount headroom,
        salary alignment) so the predictor has real signal to learn, rather
        than noise.
        """
        bh = self.bank_health.get(cust.bank)
        base = {Rail.UPI_AUTOPAY: 0.10, Rail.EMANDATE: 0.09, Rail.CARD: 0.05}[m.rail]
        base += 0.010 * bh["td_pct"] + 0.004 * bh["bd_pct"]
        if cust.salary_day is None:
            base += 0.05
        else:
            gap = days_between_cycle_and_salary(m.cycle_day, cust.salary_day)
            if gap <= 3:
                base += 0.06
        if m.rail == Rail.UPI_AUTOPAY and m.plan_amount_paise >= 0.9 * 1_500_000:
            base += 0.05
        base -= min(0.04, cust.tenure_months * 0.002)
        return max(0.01, min(0.45, base))

    # ------------------------------------------------------- current cycle
    def open_current_cycle(
        self, m: Mandate, cust: Customer, truth: Truth, sim_state: dict
    ) -> tuple[Mandate, dict]:
        """Advance the mandate into the cycle under decision, applying truth.

        Calendar-honest: the cycle under decision is THIS month's debit date.
          * If it is still ahead of the decision date, the debit has not been
            attempted: a doomed mandate is PRE-DEBIT, still ACTIVE, still
            pausable. That is the product's whole window.
          * If it is on or before the decision date, the attempt happened
            `days_ago` days back (0-3): a doomed mandate is inside the retry
            ladder (PENDING, next retry in the future), HALTED if the ladder is
            spent, or -- for eMandate -- still awaiting confirmation.

        An earlier version anchored post-failure cases to LAST month's date
        whenever this month's was ahead, duplicating the final history cycle
        with a retry dated a month in the past; the guard's 24h-retry rule
        could never fire and the adjudicator reasoned about a stale schedule.
        """
        rng = self.rng("current", m.id)
        cycle = sim_state["next_cycle_index"]
        anchor = on_cycle_day(self.decision_date, m.cycle_day)
        if m.rail == Rail.EMANDATE and self.calendar.is_bank_holiday(anchor):
            anchor = self.calendar.previous_business_day(anchor)
        at = ensure_aware(datetime.combine(anchor, datetime.min.time()) + timedelta(hours=10))
        inv = self._new_invoice(m, cycle, at)
        days_ago = (self.decision_date - anchor).days
        sim_state["next_cycle_index"] = cycle
        sim_state["pre_debit"] = False

        # ---- debit still ahead: nothing has been attempted yet
        if days_ago < 0:
            m.status = SubStatus.ACTIVE
            m.auth_attempts = 0
            m.last_error_reason = None
            m.charge_at = at
            if truth.payment_will_fail:
                truth.raw_reason = _pick(rng, CAUSE_REASONS[m.rail].get(truth.cause, ["payment_failed"]))
                sim_state["pre_debit"] = True
            self.store.upsert_invoice(inv)
            return m, sim_state

        # ---- attempted `days_ago` days back
        if not truth.payment_will_fail:
            m.status = SubStatus.ACTIVE
            m.auth_attempts = 0
            m.last_error_reason = None
            m.paid_count += 1
            inv.status = "paid"
            inv.attempts = 1
            self.store.upsert_invoice(inv)
            self.emit(m, "subscription.charged", at, self.payment_entity(m, inv.id, True, None, at))
            nxt = on_cycle_day(add_months(anchor, 1), m.cycle_day)
            m.charge_at = ensure_aware(datetime.combine(nxt, datetime.min.time()) + timedelta(hours=10))
            sim_state["next_cycle_index"] = cycle + 1
            return m, sim_state

        reason = _pick(rng, CAUSE_REASONS[m.rail].get(truth.cause, ["payment_failed"]))
        truth.raw_reason = reason

        if m.rail == Rail.EMANDATE and days_ago <= 2 and rng.random() < 0.45:
            # Attempt sent, confirmation not yet received: the double-debit window.
            inv.attempt_in_flight = True
            inv.attempts = 1
            self.store.upsert_invoice(inv)
            m.status = SubStatus.PENDING
            m.auth_attempts = 1
            m.last_error_reason = None  # nothing has come back yet
            delay = self._confirm_delay_hours(rng)
            resolve_at = max(at + timedelta(hours=delay), self.now + timedelta(hours=1))
            m.charge_at = max(at + timedelta(days=2), self.now + timedelta(days=1))
            sim_state["inflight"] = [{
                "invoice_id": inv.id,
                "resolve_at": to_iso(resolve_at),
                "will_succeed": rng.random() < 0.35,
                "origin": "auto",
            }]
            self.emit(m, "subscription.pending", at)
            return m, sim_state

        # Retry ladder: one attempt per elapsed day, T, T+1, T+2, T+3, halt on the 4th.
        attempts = min(MAX_ATTEMPTS_BEFORE_HALT, days_ago + 1)
        for a in range(1, attempts + 1):
            fail_at = at + timedelta(days=a - 1)
            m.auth_attempts = a
            m.last_error_reason = reason
            m.last_error_source = "customer" if "insufficient" in reason.lower() else "bank"
            inv.attempts = a
            inv.status = "failed"
            if a >= MAX_ATTEMPTS_BEFORE_HALT:
                m.status = SubStatus.HALTED
                m.charge_at = None
                self.emit(m, "subscription.halted", fail_at,
                          self.payment_entity(m, inv.id, False, reason, fail_at, suffix=f"a{a}"))
            else:
                m.status = SubStatus.PENDING
                m.charge_at = fail_at + timedelta(days=1)
                self.emit(m, "subscription.pending", fail_at,
                          self.payment_entity(m, inv.id, False, reason, fail_at, suffix=f"a{a}"))
        self.store.upsert_invoice(inv)
        return m, sim_state

    # ------------------------------------------------------ retry mechanics
    def retry_ladder_step(self, m: Mandate, ok: bool, reason: str = "insufficient_funds") -> Mandate:
        """Advance one automatic retry. Encodes spec 2.3 exactly.

        T fails -> pending(1); T+1 -> 2; T+2 -> 3; T+3 -> halted at 4.
        """
        if m.status not in (SubStatus.ACTIVE, SubStatus.PENDING):
            raise InvalidTransition(f"cannot attempt a charge from {m.status.value}")
        inv = self.store.open_invoice(m.id)
        at = m.charge_at or self.now
        if ok:
            m.status = SubStatus.ACTIVE
            m.auth_attempts = 0
            m.paid_count += 1
            m.last_error_reason = None
            if inv:
                inv.status = "paid"
                inv.attempt_in_flight = False
                self.store.upsert_invoice(inv)
            m.charge_at = ensure_aware(
                datetime.combine(on_cycle_day(add_months(at.date(), 1), m.cycle_day),
                                 datetime.min.time()) + timedelta(hours=10)
            )
            self.emit(m, "subscription.charged", at,
                      self.payment_entity(m, inv.id if inv else "inv_unknown", True, None, at),
                      suffix=f"ladder{m.paid_count}")
            return m
        m.auth_attempts += 1
        m.last_error_reason = reason
        if inv:
            inv.attempts = m.auth_attempts
            inv.status = "failed"
            self.store.upsert_invoice(inv)
        if m.auth_attempts >= MAX_ATTEMPTS_BEFORE_HALT:
            m.status = SubStatus.HALTED
            m.charge_at = None
            self.emit(m, "subscription.halted", at,
                      self.payment_entity(m, inv.id if inv else "inv_unknown", False, reason, at,
                                          suffix=f"a{m.auth_attempts}"))
        else:
            m.status = SubStatus.PENDING
            m.charge_at = at + timedelta(days=1)
            self.emit(m, "subscription.pending", at,
                      self.payment_entity(m, inv.id if inv else "inv_unknown", False, reason, at,
                                          suffix=f"a{m.auth_attempts}"))
        return m

    # -------------------------------------------------------------- actions
    def pause(self, m: Mandate, initiated_by: str = "self") -> Mandate:
        """Spec 2.1: only ACTIVE may be paused. Pausing AUTHENTICATED cancels it."""
        if m.status == SubStatus.AUTHENTICATED:
            m.status = SubStatus.CANCELLED
            m.charge_at = None
            self.emit(m, "subscription.cancelled", self.now, suffix="pause-on-authenticated")
            raise InvalidTransition(
                "pausing an authenticated subscription cancels it permanently; refused"
            )
        if m.status != SubStatus.ACTIVE:
            raise InvalidTransition(f"only active subscriptions can be paused (was {m.status.value})")
        m.status = SubStatus.PAUSED
        m.paused_at = self.now
        m.pause_initiated_by = initiated_by
        m.charge_at = None
        self.emit(m, "subscription.paused", self.now)
        return m

    def resume(self, m: Mandate, resume_on: date | None = None) -> Mandate:
        if m.status != SubStatus.PAUSED:
            raise InvalidTransition(f"only paused subscriptions can be resumed (was {m.status.value})")
        if m.rail == Rail.UPI_AUTOPAY and m.pause_initiated_by == "customer":
            raise RailNotSupported(
                "a UPI subscription paused by the customer can only be resumed by the customer"
            )
        m.status = SubStatus.ACTIVE
        m.pause_initiated_by = None
        m.paused_at = None
        target = resume_on or self.now.date()
        m.charge_at = ensure_aware(
            datetime.combine(on_cycle_day(target, m.cycle_day), datetime.min.time()) + timedelta(hours=10)
        )
        if m.charge_at.date() < target:
            m.charge_at = ensure_aware(datetime.combine(target, datetime.min.time()) + timedelta(hours=10))
        self.emit(m, "subscription.resumed", self.now)
        return m

    def cancel(self, m: Mandate, at_cycle_end: bool = True) -> Mandate:
        if m.status in (SubStatus.CANCELLED, SubStatus.COMPLETED, SubStatus.EXPIRED):
            raise InvalidTransition(f"{m.status.value} is terminal")
        if at_cycle_end:
            st = self.store.get_sim_state(m.id)
            st["cancel_at_cycle_end"] = True
            self.store.set_sim_state(m.id, st)
            self.emit(m, "subscription.updated", self.now, suffix="cancel-scheduled")
            return m
        m.status = SubStatus.CANCELLED
        m.charge_at = None
        self.emit(m, "subscription.cancelled", self.now)
        return m

    def update(self, m: Mandate, **fields: object) -> Mandate:
        """Spec 2.4: cards only. UPI and eMandate cannot be updated at all."""
        if m.rail != Rail.CARD:
            raise RailNotSupported(
                f"subscriptions authorised via {m.rail.value} cannot be updated (Razorpay: cards only)"
            )
        if m.status not in (SubStatus.ACTIVE, SubStatus.AUTHENTICATED):
            raise InvalidTransition(f"update requires active or authenticated (was {m.status.value})")
        if "plan_amount_paise" in fields:
            m.plan_amount_paise = int(fields["plan_amount_paise"])  # type: ignore[arg-type]
        if fields.get("start_at"):
            m.charge_at = ensure_aware(fields["start_at"])  # type: ignore[arg-type]
        self.emit(m, "subscription.updated", self.now)
        return m

    def manual_charge(self, m: Mandate, invoice: Invoice) -> tuple[Mandate, bool, bool]:
        """Charge an existing invoice. Returns (mandate, succeeded, double_debited).

        If an eMandate attempt is still in flight, this is the hazard: both the
        in-flight debit and this one can land. The integrity guard exists to
        stop this call ever being made in that state.
        """
        if m.status not in (SubStatus.PENDING, SubStatus.HALTED):
            raise InvalidTransition(f"manual charge requires pending or halted (was {m.status.value})")
        if invoice.mandate_id != m.id:
            raise InvalidTransition("invoice belongs to a different subscription")
        if invoice.status == "paid":
            raise InvalidTransition("invoice is already paid")
        rng = self.rng("manual", m.id, invoice.id)
        st = self.store.get_sim_state(m.id)
        inflight = [f for f in st.get("inflight", []) if f["invoice_id"] == invoice.id]
        double = False

        if inflight and rng.random() < DOUBLE_DEBIT_PROB:
            double = True
            at1 = self.now
            at2 = self.now + timedelta(minutes=5)
            m.paid_count += 1
            invoice.status = "paid"
            invoice.attempt_in_flight = False
            self.store.upsert_invoice(invoice)
            m.status = SubStatus.ACTIVE
            m.auth_attempts = 0
            self.emit(m, "subscription.charged", at1,
                      self.payment_entity(m, invoice.id, True, None, at1, suffix="manual"))
            self.emit(m, "subscription.charged", at2,
                      self.payment_entity(m, invoice.id, True, None, at2, suffix="inflight-late"),
                      suffix="dd")
            st["inflight"] = [f for f in st.get("inflight", []) if f["invoice_id"] != invoice.id]
            self.store.set_sim_state(m.id, st)
            return m, True, True

        ok = rng.random() < 0.62
        at = self.now
        if ok:
            m.paid_count += 1
            m.status = SubStatus.ACTIVE
            m.auth_attempts = 0
            m.last_error_reason = None
            invoice.status = "paid"
            invoice.attempt_in_flight = False
            self.store.upsert_invoice(invoice)
            self.emit(m, "subscription.charged", at,
                      self.payment_entity(m, invoice.id, True, None, at, suffix="manual"))
        else:
            reason = m.last_error_reason or "payment_failed"
            self.emit(m, "subscription.pending", at,
                      self.payment_entity(m, invoice.id, False, reason, at, suffix="manual"))
        return m, ok, double

    def resolve_inflight(self, m: Mandate) -> list[dict]:
        """Resolve any eMandate attempts whose confirmation has now arrived."""
        st = self.store.get_sim_state(m.id)
        resolved = []
        remaining = []
        for f in st.get("inflight", []):
            from grace.util import from_iso

            if from_iso(f["resolve_at"]) and from_iso(f["resolve_at"]) <= self.now:
                resolved.append(f)
            else:
                remaining.append(f)
        st["inflight"] = remaining
        self.store.set_sim_state(m.id, st)
        return resolved


def days_between_cycle_and_salary(cycle_day: int, salary_day: int) -> int:
    """Circular day-of-month distance from the salary credit to the debit."""
    d = (cycle_day - salary_day) % 30
    return min(d, 30 - d)
