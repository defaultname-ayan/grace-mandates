"""One interface, two implementations (spec 10.1)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class FeatureNotEnabled(RuntimeError):
    """Razorpay: 'pause is not allowed, feature is not enabled' on this account."""


@dataclass
class ActionResult:
    ok: bool
    action: str
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    note: str | None = None


@runtime_checkable
class SubscriptionsClient(Protocol):
    def create_plan(self, *, name: str, amount_paise: int, period: str = "monthly",
                    interval: int = 1) -> dict: ...
    def create_subscription(self, *, plan_id: str, total_count: int,
                            start_at: int | None = None, notes: dict | None = None) -> dict: ...
    def fetch(self, sub_id: str) -> dict: ...
    def pause(self, sub_id: str) -> ActionResult: ...
    def resume(self, sub_id: str) -> ActionResult: ...
    def cancel(self, sub_id: str, *, at_cycle_end: bool) -> ActionResult: ...
    def update(self, sub_id: str, **fields: Any) -> ActionResult: ...
    def invoices(self, sub_id: str) -> list[dict]: ...
    def charge_invoice(self, sub_id: str, invoice_id: str) -> ActionResult: ...
