"""Runtime configuration. Environment overrides, with safe defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Bounds:
    """Hard limits enforced in code. The model can never widen these (spec 9.2)."""

    MAX_PAUSE_CYCLES: int = 2
    MAX_INTERVENTIONS_PER_CYCLE: int = 1
    MAX_INTERVENTIONS_TOTAL: int = 3
    MAX_RESUME_HORIZON_DAYS: int = 62
    CONF_PAUSE: float = 0.55
    CONF_MONEY: float = 0.65           # manual_charge, step_down_plan
    CONF_CANCEL_CAUSE: float = 0.70    # cause_confidence for cancel_at_cycle_end
    UPI_AFA_CAP_PAISE: int = 1_500_000  # Rs 15,000


@dataclass(frozen=True)
class Config:
    seed: int = _i("GRACE_SEED", 20260905)
    theta_low: float = _f("GRACE_THETA_LOW", 0.15)
    theta_high: float = _f("GRACE_THETA_HIGH", 0.60)
    #: "gemini" (default) or "anthropic". Both implement the same adjudicator
    #: interface; the policy layer is identical either way.
    provider: str = os.getenv("GRACE_PROVIDER", "gemini").lower()
    model: str = os.getenv("GRACE_MODEL", "")  # empty -> the provider's default
    effort: str = os.getenv("GRACE_EFFORT", "high")
    batch_effort: str = os.getenv("GRACE_BATCH_EFFORT", "medium")
    max_workers: int = _i("GRACE_MAX_WORKERS", 6)
    bounds: Bounds = field(default_factory=Bounds)

    # List prices (USD per 1M tokens) for claude-opus-5, used for cost reporting only.
    price_in_per_mtok: float = 5.0
    price_out_per_mtok: float = 25.0
    usd_inr: float = 88.0


CONFIG = Config()
