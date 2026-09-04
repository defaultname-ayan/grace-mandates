"""Domain models (spec 4)."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from grace.util import ensure_aware


class Rail(str, Enum):
    CARD = "card"
    UPI_AUTOPAY = "upi"
    EMANDATE = "emandate"


class SubStatus(str, Enum):
    CREATED = "created"
    AUTHENTICATED = "authenticated"
    ACTIVE = "active"
    PENDING = "pending"
    HALTED = "halted"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    EXPIRED = "expired"


TERMINAL_STATES = {SubStatus.CANCELLED, SubStatus.COMPLETED, SubStatus.EXPIRED}


class Cause(str, Enum):
    """Adjudicated root cause. Deliberately coarser than raw reason codes."""

    LIQUIDITY_TIMING = "liquidity_timing"
    LIQUIDITY_STRUCTURAL = "liquidity_structural"
    BANK_TECHNICAL = "bank_technical"
    INSTRUMENT_INVALID = "instrument_invalid"
    LIMIT_EXCEEDED = "limit_exceeded"
    CUSTOMER_INTENT_TEMPORARY = "customer_intent_temporary"
    CUSTOMER_INTENT_PRICE = "customer_intent_price"
    CUSTOMER_INTENT_DONE = "customer_intent_done"
    REMAP_IN_FLIGHT = "remap_in_flight"
    UNKNOWN = "unknown"


class Action(str, Enum):
    NOOP = "noop"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL_AT_CYCLE_END = "cancel_at_cycle_end"
    MANUAL_CHARGE = "manual_charge"
    STEP_DOWN_PLAN = "step_down_plan"
    SHIFT_START = "shift_start"
    REQUEST_REAUTH = "request_reauth"
    ESCALATE = "escalate"


#: Actions that move money or change the customer's billing relationship.
INTERVENTIONS = {
    Action.PAUSE,
    Action.RESUME,
    Action.CANCEL_AT_CYCLE_END,
    Action.MANUAL_CHARGE,
    Action.STEP_DOWN_PLAN,
    Action.SHIFT_START,
    Action.REQUEST_REAUTH,
}

#: Invasiveness order used by the prompt and by regret reporting (spec 8.3).
INVASIVENESS = {
    Action.NOOP: 0,
    Action.ESCALATE: 0,
    Action.PAUSE: 1,
    Action.MANUAL_CHARGE: 2,
    Action.RESUME: 2,
    Action.SHIFT_START: 3,
    Action.CANCEL_AT_CYCLE_END: 4,
    Action.STEP_DOWN_PLAN: 5,
    Action.REQUEST_REAUTH: 6,
}

#: Counterfactual key each action is scored under in the eval (spec 5.2).
#: ESCALATE/RESUME execute no money movement, so they score as noop.
ACTION_TO_CF_KEY = {
    Action.NOOP: "noop",
    Action.ESCALATE: "noop",
    Action.RESUME: "noop",
    Action.PAUSE: "pause",
    Action.MANUAL_CHARGE: "manual_charge",
    Action.CANCEL_AT_CYCLE_END: "cancel_at_cycle_end",
    Action.STEP_DOWN_PLAN: "step_down_plan",
    Action.SHIFT_START: "pause",
    Action.REQUEST_REAUTH: "request_reauth",
}

CF_KEYS = ["noop", "pause", "manual_charge", "cancel_at_cycle_end", "step_down_plan", "request_reauth"]


class Customer(BaseModel):
    id: str
    bank: str
    salary_day: Optional[int] = None
    tenure_months: int = 0
    ltv_band: Literal["low", "mid", "high"] = "mid"
    travel_flag: bool = False


class Mandate(BaseModel):
    """Our view of a Razorpay subscription."""

    id: str
    customer_id: str
    rail: Rail
    plan_amount_paise: int
    cycle_day: int
    status: SubStatus
    auth_attempts: int = 0
    paid_count: int = 0
    total_count: int = 12
    charge_at: Optional[datetime] = None
    pause_initiated_by: Optional[str] = None
    paused_at: Optional[datetime] = None
    last_error_reason: Optional[str] = None
    last_error_source: Optional[str] = None
    interventions_this_cycle: int = 0
    interventions_total: int = 0

    @field_validator("charge_at", "paused_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        return ensure_aware(v)

    @property
    def remaining_count(self) -> int:
        return max(0, self.total_count - self.paid_count)


class Invoice(BaseModel):
    id: str
    mandate_id: str
    cycle_index: int
    amount_paise: int
    status: Literal["issued", "paid", "failed"] = "issued"
    attempt_in_flight: bool = False
    attempts: int = 0
    issued_at: Optional[datetime] = None

    @field_validator("issued_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        return ensure_aware(v)


class Event(BaseModel):
    id: str
    mandate_id: str
    name: str
    at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    processed: bool = False

    @field_validator("at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_aware(v)

    @property
    def error_code(self) -> str | None:
        ent = (self.payload.get("payment") or {}).get("entity") or {}
        return ent.get("error_code")

    @property
    def error_description(self) -> str | None:
        ent = (self.payload.get("payment") or {}).get("entity") or {}
        return ent.get("error_description")

    @property
    def invoice_id(self) -> str | None:
        ent = (self.payload.get("payment") or {}).get("entity") or {}
        return ent.get("invoice_id")


class Truth(BaseModel):
    """Latent ground truth. NEVER visible to the adjudicator or the predictor at inference."""

    #: Mandate is at risk of being LOST this cycle absent intervention.
    #: True for payment failures AND for customer cancel intents. This is the
    #: label the predictor targets and the one false_intervention_rate uses --
    #: scoring a cancel-intent save as a "false" intervention would invert the
    #: headline metric.
    will_fail: bool
    #: Whether the scheduled debit itself would fail. False for cancel intents,
    #: whose payment would have gone through fine.
    payment_will_fail: bool = False
    at_risk_reason: Literal["payment_failure", "cancel_intent", "none"] = "none"
    raw_reason: Optional[str] = None
    cause: Cause = Cause.UNKNOWN
    cancel_intent: Literal["temporary", "price", "done", "none"] = "none"
    cancel_intent_text: Optional[str] = None
    survival_under: dict[str, float] = Field(default_factory=dict)


class Evidence(BaseModel):
    mandate: Mandate
    customer: Customer
    recent_events: list[Event] = Field(default_factory=list)
    bank_health: dict[str, Any] = Field(default_factory=dict)
    days_to_salary: Optional[int] = None
    is_bank_holiday_on_charge_day: bool = False
    in_downtime: bool = False
    cancel_intent_text: Optional[str] = None
    allowed_actions: list[Action] = Field(default_factory=list)
    p_fail: float = 0.0
    salary_day_inferred: bool = False
    has_pending_invoice: bool = False
    prior_fail_count_6m: int = 0
    prior_fail_streak: int = 0
    #: An eMandate debit has been sent but its confirmation has not arrived.
    #: Merchant-visible in production; charging again here can double-debit.
    emandate_attempt_in_flight: bool = False
    #: Calibrated risk level at which a pre-emptive pause pays for itself,
    #: selected on the training split. Below it, acting costs more than it saves.
    preemptive_threshold: float = 0.60
