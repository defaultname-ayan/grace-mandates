"""Real Razorpay test-mode client (spec 10.3).

Test-mode keys only. Every call is echoed with its request and response so the
demo shows real API traffic, not a mock. Failures here are content for
docs/WHAT-BROKE.md, not blockers: the simulator covers what test mode cannot do.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

from grace.rzp.base import ActionResult, FeatureNotEnabled

PAUSE_NOT_ENABLED = "pause is not allowed, feature is not enabled"


class RailNotSupported(RuntimeError):
    pass


def _client():
    import razorpay  # lazy: the offline path must not need this package

    key = os.getenv("RAZORPAY_KEY_ID")
    secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key or not secret:
        raise RuntimeError(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. Copy .env.example to .env "
            "and use TEST-MODE keys (rzp_test_...)."
        )
    if not key.startswith("rzp_test"):
        raise RuntimeError(f"refusing to run against a non-test key ({key[:12]}...). "
                           "Grace never touches live keys.")
    return razorpay.Client(auth=(key, secret))


class LiveClient:
    name = "live"

    def __init__(self):
        self.c = _client()

    def create_plan(self, *, name: str, amount_paise: int, period: str = "monthly",
                    interval: int = 1) -> dict:
        return self.c.plan.create({
            "period": period, "interval": interval,
            "item": {"name": name, "amount": amount_paise, "currency": "INR",
                     "description": "Grace buildathon demo plan"},
            "notes": {"source": "grace"},
        })

    def create_subscription(self, *, plan_id: str, total_count: int = 12,
                            start_at: int | None = None, notes: dict | None = None) -> dict:
        body: dict[str, Any] = {"plan_id": plan_id, "total_count": total_count,
                                "customer_notify": 0, "notes": notes or {"source": "grace"}}
        if start_at:
            body["start_at"] = start_at
        return self.c.subscription.create(body)

    def fetch(self, sub_id: str) -> dict:
        return self.c.subscription.fetch(sub_id)

    def invoices(self, sub_id: str) -> list[dict]:
        return self.c.invoice.all({"subscription_id": sub_id}).get("items", [])

    def pause(self, sub_id: str) -> ActionResult:
        req = {"method": "POST", "path": f"/v1/subscriptions/{sub_id}/pause",
               "body": {"pause_at": "now"}}
        try:
            res = self.c.subscription.pause(sub_id, {"pause_at": "now"})
            return ActionResult(True, "pause", req, res)
        except Exception as e:
            if PAUSE_NOT_ENABLED in str(e).lower():
                raise FeatureNotEnabled(
                    "Razorpay reports pause is not enabled on this account. Ask Razorpay support "
                    "to enable Subscriptions pause/resume; Grace runs the affected mandates on the "
                    "simulator until then."
                ) from e
            return ActionResult(False, "pause", req, {}, error=str(e))

    def resume(self, sub_id: str) -> ActionResult:
        req = {"method": "POST", "path": f"/v1/subscriptions/{sub_id}/resume",
               "body": {"resume_at": "now"}}
        try:
            return ActionResult(True, "resume", req, self.c.subscription.resume(sub_id, {"resume_at": "now"}))
        except Exception as e:
            return ActionResult(False, "resume", req, {}, error=str(e))

    def cancel(self, sub_id: str, *, at_cycle_end: bool = True) -> ActionResult:
        req = {"method": "POST", "path": f"/v1/subscriptions/{sub_id}/cancel",
               "body": {"cancel_at_cycle_end": at_cycle_end}}
        try:
            return ActionResult(True, "cancel", req,
                                self.c.subscription.cancel(sub_id, {"cancel_at_cycle_end": at_cycle_end}))
        except Exception as e:
            return ActionResult(False, "cancel", req, {}, error=str(e))

    def update(self, sub_id: str, **fields: Any) -> ActionResult:
        """Cards only. Refused before any HTTP call for other rails."""
        method = (fields.pop("payment_method", "") or "").lower()
        if method in ("upi", "emandate"):
            raise RailNotSupported(
                f"Razorpay does not allow updating a subscription authorised via {method}."
            )
        req = {"method": "PATCH", "path": f"/v1/subscriptions/{sub_id}", "body": fields}
        try:
            return ActionResult(True, "update", req, self.c.subscription.update(sub_id, fields))
        except Exception as e:
            return ActionResult(False, "update", req, {}, error=str(e))

    def charge_invoice(self, sub_id: str, invoice_id: str) -> ActionResult:
        raise NotImplementedError(
            "Razorpay does not document an API to charge a subscription invoice on demand; it is a "
            "dashboard action in test mode. Grace simulates it and shows the dashboard step in the "
            "demo rather than pretending an endpoint exists."
        )


def live_demo(amount_paise: int = 49900, keep: bool = False, wait: int = 0,
              echo: Callable[[str], None] = print) -> dict:
    """Create a real plan + subscription in TEST mode and exercise the lifecycle."""
    out: dict[str, Any] = {"steps": []}

    def step(name: str, ok: bool, detail: Any) -> None:
        out["steps"].append({"step": name, "ok": ok, "detail": detail})
        echo(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    try:
        c = LiveClient()
    except RuntimeError as e:
        echo(f"  [SKIP] {e}")
        out["skipped"] = str(e)
        return out

    echo("\nRazorpay TEST mode - real API calls")
    plan = c.create_plan(name="Grace demo", amount_paise=amount_paise)
    step("create plan", True, plan["id"])
    sub = c.create_subscription(plan_id=plan["id"], total_count=12)
    step("create subscription", True, f"{sub['id']} status={sub['status']}")
    out["plan_id"], out["subscription_id"] = plan["id"], sub["id"]

    echo(f"\n  Authorise it here to continue: {sub.get('short_url')}")
    echo("  (a subscription must reach `active` before it can be paused)")

    status = sub["status"]
    if wait:
        deadline = time.time() + wait
        while time.time() < deadline and status != "active":
            time.sleep(5)
            status = c.fetch(sub["id"])["status"]
            echo(f"  ... status={status}")

    if status != "active":
        step("pause/resume", False,
             f"skipped: subscription is '{status}', and Razorpay only permits pausing an "
             f"'active' subscription. Re-run with --wait after authorising.")
        out["status"] = status
        return out

    try:
        r = c.pause(sub["id"])
        step("pause", r.ok, r.response.get("status") if r.ok else r.error)
        if r.ok:
            out["pause_initiated_by"] = r.response.get("pause_initiated_by")
            step("pause_initiated_by (undocumented for customer-initiated)", True,
                 out["pause_initiated_by"])
        r = c.resume(sub["id"])
        step("resume", r.ok, r.response.get("status") if r.ok else r.error)
        if r.ok:
            # Known unknown #1: does resume keep the cycle or reschedule?
            out["charge_at_after_resume"] = r.response.get("charge_at")
            step("charge_at after resume (answers known-unknown #1)", True,
                 out["charge_at_after_resume"])
    except FeatureNotEnabled as e:
        step("pause", False, str(e))

    if not keep:
        r = c.cancel(sub["id"], at_cycle_end=False)
        step("cancel", r.ok, r.response.get("status") if r.ok else r.error)
    return out
