"""Indian bank holiday calendar (spec 6.3).

Two components:
  * a hand-entered NATIONAL fixed-date list (data/holidays_2026.json), and
  * the weekly rule, applied programmatically: banks close every Sunday and on
    the 2nd and 4th Saturday of each month.

State-level holidays are NOT modelled. Documented as a known limitation.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data" / "holidays_2026.json"


def _nth_weekday_of_month(d: date) -> int:
    """1 for the 1st occurrence of this weekday in the month, 2 for the 2nd, ..."""
    return (d.day - 1) // 7 + 1


class HolidayCalendar:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DATA
        self._fixed: dict[date, str] = {}
        self.provenance = "missing"
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.provenance = raw.get("_provenance", "unknown")
            for row in raw.get("dates", []):
                self._fixed[date.fromisoformat(row["date"])] = row["name"]

    def is_bank_holiday(self, d: date) -> bool:
        if d in self._fixed:
            return True
        if d.weekday() == 6:  # Sunday
            return True
        return d.weekday() == 5 and _nth_weekday_of_month(d) in (2, 4)  # 2nd/4th Saturday

    def reason(self, d: date) -> str | None:
        if d in self._fixed:
            return self._fixed[d]
        if d.weekday() == 6:
            return "Sunday"
        if d.weekday() == 5 and _nth_weekday_of_month(d) in (2, 4):
            return f"{ {2: '2nd', 4: '4th' }[_nth_weekday_of_month(d)] } Saturday"
        return None

    def next_business_day(self, d: date) -> date:
        cur = d
        for _ in range(30):
            cur += timedelta(days=1)
            if not self.is_bank_holiday(cur):
                return cur
        return cur  # pragma: no cover - 30 consecutive holidays is impossible

    def previous_business_day(self, d: date) -> date:
        """Razorpay charges on T-1 when T is a bank holiday (eMandate)."""
        cur = d
        for _ in range(30):
            cur -= timedelta(days=1)
            if not self.is_bank_holiday(cur):
                return cur
        return cur  # pragma: no cover


@lru_cache(maxsize=1)
def default_calendar() -> HolidayCalendar:
    return HolidayCalendar()
