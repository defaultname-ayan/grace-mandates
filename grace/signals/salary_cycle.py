"""Salary-cycle signals (spec 6.2)."""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Iterable

from grace.signals.holidays import HolidayCalendar, default_calendar
from grace.util import add_months, on_cycle_day


def _salary_date_in_month(anchor: date, salary_day: int) -> date:
    return on_cycle_day(anchor, salary_day)


def next_salary_date(today: date, salary_day: int | None) -> date | None:
    if salary_day is None:
        return None
    this_month = _salary_date_in_month(today, salary_day)
    if this_month >= today:
        return this_month
    return _salary_date_in_month(add_months(today, 1), salary_day)


def days_to_salary(today: date, salary_day: int | None) -> int | None:
    """0 on salary day; otherwise days until the next one. None if unknown."""
    nxt = next_salary_date(today, salary_day)
    return None if nxt is None else (nxt - today).days


def suggested_resume_date(
    today: date,
    salary_day: int | None,
    cycle_day: int,
    calendar: HolidayCalendar | None = None,
    settle_days: int = 2,
) -> date:
    """First date >= today+1 that is >= settle_days after the next salary credit
    and is not a bank holiday. With no salary signal, fall back to the next
    cycle day. Always returns a date strictly after today.
    """
    cal = calendar or default_calendar()
    nxt = next_salary_date(today, salary_day)
    if nxt is None:
        candidate = on_cycle_day(add_months(today, 1), cycle_day)
    else:
        candidate = nxt + timedelta(days=settle_days)
    if candidate <= today:
        candidate = today + timedelta(days=1)
    while cal.is_bank_holiday(candidate):
        candidate += timedelta(days=1)
    return candidate


def infer_salary_day(success_dates: Iterable[date], min_support: int = 3, tol: int = 2) -> int | None:
    """Infer salary day from successful-debit dates.

    Returns the modal day-of-month if at least `min_support` successes fall
    within +/- `tol` days of it. Returns None when the signal is too weak,
    which is the honest answer for a new customer.
    """
    days = [d.day for d in success_dates]
    if len(days) < min_support:
        return None
    best_day, best_support = None, 0
    for cand in Counter(days):
        support = sum(1 for d in days if min(abs(d - cand), 31 - abs(d - cand)) <= tol)
        if support > best_support or (support == best_support and best_day is not None and cand < best_day):
            best_day, best_support = cand, support
    if best_support >= min_support:
        return best_day
    return None
