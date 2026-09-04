"""Deterministic feature extraction (spec 7).

Every feature is derivable from data a merchant actually has: the Razorpay
event stream, the plan, the customer record, and a public bank-health feed.
Nothing here touches ground truth.
"""
from __future__ import annotations

import math

from grace.models import Evidence, Rail
from grace.sim.vocab import reason_family

FEATURE_NAMES = [
    "rail_card", "rail_upi", "rail_emandate",
    "amount_log",
    "prior_fail_count_6m", "prior_fail_streak",
    "reason_liquidity", "reason_technical", "reason_instrument", "reason_other",
    "salary_0_2", "salary_3_6", "salary_7plus", "salary_unknown",
    "bank_td_pct", "bank_bd_pct",
    "in_downtime", "holiday_on_charge_day",
    "tenure_lt6", "tenure_6_17", "tenure_18plus",
    "paid_count", "auth_attempts",
    "amount_near_upi_cap", "cycle_day_month_end",
]

UPI_CAP_PAISE = 1_500_000


def featurise(ev: Evidence) -> list[float]:
    m, c = ev.mandate, ev.customer
    fam = reason_family(m.last_error_reason)
    d = ev.days_to_salary
    t = c.tenure_months
    return [
        1.0 if m.rail == Rail.CARD else 0.0,
        1.0 if m.rail == Rail.UPI_AUTOPAY else 0.0,
        1.0 if m.rail == Rail.EMANDATE else 0.0,
        math.log10(max(1, m.plan_amount_paise)),
        float(ev.prior_fail_count_6m),
        float(ev.prior_fail_streak),
        1.0 if fam == "liquidity" else 0.0,
        1.0 if fam == "technical" else 0.0,
        1.0 if fam == "instrument" else 0.0,
        1.0 if fam in ("other", "limit", "customer") else 0.0,
        1.0 if (d is not None and d <= 2) else 0.0,
        1.0 if (d is not None and 3 <= d <= 6) else 0.0,
        1.0 if (d is not None and d >= 7) else 0.0,
        1.0 if d is None else 0.0,
        float(ev.bank_health.get("td_pct", 0.0)),
        float(ev.bank_health.get("bd_pct", 0.0)),
        1.0 if ev.in_downtime else 0.0,
        1.0 if ev.is_bank_holiday_on_charge_day else 0.0,
        1.0 if t < 6 else 0.0,
        1.0 if 6 <= t < 18 else 0.0,
        1.0 if t >= 18 else 0.0,
        float(m.paid_count),
        float(m.auth_attempts),
        1.0 if (m.rail == Rail.UPI_AUTOPAY and m.plan_amount_paise >= 0.9 * UPI_CAP_PAISE) else 0.0,
        1.0 if m.cycle_day >= 28 else 0.0,
    ]
