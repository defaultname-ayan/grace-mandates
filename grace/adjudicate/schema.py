"""Adjudicator output schema (spec 8.2).

Flattened relative to the spec's nested PauseParams/StepDownParams, and free of
numeric/length constraints. Both are deliberate: nested optional objects and
tight JSON-Schema constraints are the two things most likely to make a
structured-output call fail at runtime, and every one of those constraints is
re-validated in policy/gate.py anyway, where a violation is logged rather than
raised. Schema simplicity here costs nothing and removes a failure mode.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from grace.models import Action, Cause


class Decision(BaseModel):
    cause: Cause = Field(description="Adjudicated root cause of the risk to this mandate.")
    cause_confidence: float = Field(description="Calibrated probability, 0-1, that the cause is correct. Use 0.5 when genuinely unsure.")
    action: Action = Field(description="The single action to take. MUST be one of allowed_actions.")
    action_confidence: float = Field(description="Calibrated probability, 0-1, that this action is the right one.")
    pause_cycles: Optional[int] = Field(default=None, description="For action=pause: 1 or 2 billing cycles.")
    resume_on: Optional[str] = Field(default=None, description="For action=pause: ISO date (YYYY-MM-DD) to resume on.")
    step_down_target_plan_id: Optional[str] = Field(default=None, description="For action=step_down_plan: the cheaper plan id.")
    rationale: str = Field(description="Why, citing the specific evidence fields relied on. Read by a finance operator and an auditor.")
    evidence_used: list[str] = Field(default_factory=list, description="Short refs to the evidence relied on, e.g. 'reason=insufficient_funds', 'days_to_salary=3'.")
    customer_message: Optional[str] = Field(default=None, description="Optional short plain message the merchant could send the customer.")
    escalate: bool = Field(default=False, description="True if a human must review before anything happens.")
    escalate_reason: Optional[str] = Field(default=None, description="Why this needs a human.")

    def clamped(self) -> "Decision":
        """Coerce free-form model output into sane ranges. Never raises."""
        d = self.model_copy(deep=True)
        d.cause_confidence = max(0.0, min(1.0, float(d.cause_confidence or 0.0)))
        d.action_confidence = max(0.0, min(1.0, float(d.action_confidence or 0.0)))
        if d.pause_cycles is not None:
            d.pause_cycles = max(1, min(2, int(d.pause_cycles)))
        d.rationale = (d.rationale or "")[:600]
        d.evidence_used = [str(x)[:80] for x in (d.evidence_used or [])][:8]
        if d.customer_message:
            d.customer_message = d.customer_message[:280]
        return d
