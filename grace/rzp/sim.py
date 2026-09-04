"""Simulator-backed client. Mirrors the Razorpay call shapes exactly."""
from __future__ import annotations

from datetime import date
from typing import Any

from grace.models import Mandate, Rail
from grace.rzp.base import ActionResult
from grace.sim.engine import InvalidTransition, RailNotSupported, SimEngine
from grace.store import Store
from grace.util import stable_hash


class SimClient:
    name = "sim"

    def __init__(self, store: Store, engine: SimEngine):
        self.store = store
        self.engine = engine
        self.double_debits = 0

    def _m(self, sub_id: str) -> Mandate:
        m = self.store.get_mandate(sub_id)
        if m is None:
            raise KeyError(f"unknown subscription {sub_id}")
        return m

    def _save(self, m: Mandate) -> None:
        self.store.upsert_mandate(m)

    def fetch(self, sub_id: str) -> dict:
        return self.engine.subscription_entity(self._m(sub_id))

    def invoices(self, sub_id: str) -> list[dict]:
        return [i.model_dump(mode="json") for i in self.store.invoices_for(sub_id)]

    def pause(self, sub_id: str, resume_on: str | None = None) -> ActionResult:
        req = {"method": "POST", "path": f"/v1/subscriptions/{sub_id}/pause", "body": {"pause_at": "now"}}
        m = self._m(sub_id)
        try:
            m = self.engine.pause(m)
        except (InvalidTransition, RailNotSupported) as e:
            self._save(m)
            return ActionResult(False, "pause", req, {}, error=str(e))
        if resume_on:
            st = self.store.get_sim_state(sub_id)
            st["scheduled_resume_at"] = resume_on
            self.store.set_sim_state(sub_id, st)
        self._save(m)
        return ActionResult(True, "pause", req, self.engine.subscription_entity(m),
                            note=f"resume scheduled for {resume_on}" if resume_on else None)

    def resume(self, sub_id: str, resume_on: str | None = None) -> ActionResult:
        req = {"method": "POST", "path": f"/v1/subscriptions/{sub_id}/resume", "body": {"resume_at": "now"}}
        m = self._m(sub_id)
        try:
            m = self.engine.resume(m, date.fromisoformat(resume_on) if resume_on else None)
        except (InvalidTransition, RailNotSupported) as e:
            return ActionResult(False, "resume", req, {}, error=str(e))
        self._save(m)
        return ActionResult(True, "resume", req, self.engine.subscription_entity(m))

    def cancel(self, sub_id: str, *, at_cycle_end: bool = True) -> ActionResult:
        req = {"method": "POST", "path": f"/v1/subscriptions/{sub_id}/cancel",
               "body": {"cancel_at_cycle_end": at_cycle_end}}
        m = self._m(sub_id)
        try:
            m = self.engine.cancel(m, at_cycle_end=at_cycle_end)
        except InvalidTransition as e:
            return ActionResult(False, "cancel", req, {}, error=str(e))
        self._save(m)
        return ActionResult(True, "cancel", req, self.engine.subscription_entity(m))

    def update(self, sub_id: str, **fields: Any) -> ActionResult:
        req = {"method": "PATCH", "path": f"/v1/subscriptions/{sub_id}",
               "body": {**fields, "schedule_change_at": "cycle_end"}}
        m = self._m(sub_id)
        if m.rail != Rail.CARD:
            return ActionResult(False, "update", req, {},
                                error=f"subscriptions authorised via {m.rail.value} cannot be updated")
        try:
            m = self.engine.update(m, **fields)
        except (InvalidTransition, RailNotSupported) as e:
            return ActionResult(False, "update", req, {}, error=str(e))
        self._save(m)
        return ActionResult(True, "update", req, self.engine.subscription_entity(m))

    def charge_invoice(self, sub_id: str, invoice_id: str) -> ActionResult:
        req = {"method": "POST", "path": f"/v1/invoices/{invoice_id}/charge",
               "note": "dashboard-only in Razorpay test mode; simulated here"}
        m = self._m(sub_id)
        inv = self.store.get_invoice(invoice_id)
        if inv is None:
            return ActionResult(False, "manual_charge", req, {}, error="unknown invoice")
        if inv.mandate_id != sub_id:
            return ActionResult(False, "manual_charge", req, {}, error="invoice belongs to another subscription")
        try:
            m, ok, double = self.engine.manual_charge(m, inv)
        except InvalidTransition as e:
            return ActionResult(False, "manual_charge", req, {}, error=str(e))
        self._save(m)
        if double:
            self.double_debits += 1
        return ActionResult(ok, "manual_charge", req, self.engine.subscription_entity(m),
                            error=None if ok else "charge failed",
                            note="DOUBLE DEBIT" if double else None)

    # Not used by the batch; present so the Protocol is satisfied.
    def create_plan(self, *, name: str, amount_paise: int, period: str = "monthly",
                    interval: int = 1) -> dict:
        # stable_hash, not hash(): builtin str hashing is per-process randomised
        # and would break the "same seed, identical run" guarantee.
        return {"id": f"plan_sim_{stable_hash('plan', name, amount_paise) % 10**10:010d}",
                "item": {"amount": amount_paise}, "period": period, "interval": interval}

    def create_subscription(self, *, plan_id: str, total_count: int, start_at: int | None = None,
                            notes: dict | None = None) -> dict:
        return {"id": f"sub_sim_{stable_hash('sub', plan_id, total_count, start_at) % 10**10:010d}",
                "plan_id": plan_id, "total_count": total_count, "status": "created",
                "start_at": start_at, "notes": notes or {}}
