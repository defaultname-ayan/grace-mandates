"""Shared primitives: deterministic hashing, timezone-safe datetimes, money."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import overload

UTC = timezone.utc


def utcnow() -> datetime:
    """Timezone-aware 'now'. Never use datetime.utcnow() (naive + deprecated)."""
    return datetime.now(UTC)


@overload
def ensure_aware(dt: datetime) -> datetime: ...
@overload
def ensure_aware(dt: None) -> None: ...
def ensure_aware(dt: datetime | None) -> datetime | None:
    """Coerce a naive datetime to UTC. Mixing naive and aware raises TypeError.

    Overloaded so a non-None input types as datetime; without this every
    caller inherits an Optional it can never actually receive.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_iso(dt: datetime | None) -> str | None:
    dt = ensure_aware(dt)
    return dt.isoformat() if dt else None


def from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return ensure_aware(datetime.fromisoformat(s))


def parse_iso(s: str) -> datetime:
    """For NOT NULL columns: an empty value is a data error, not a None."""
    if not s:
        raise ValueError("expected an ISO datetime, got empty value")
    return ensure_aware(datetime.fromisoformat(s))


def stable_hash(*parts: object) -> int:
    """Deterministic 64-bit hash.

    Python's builtin hash() on str is randomised per process by PYTHONHASHSEED,
    so it must never be used for a holdout split or common random numbers.
    """
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def stable_unit(*parts: object) -> float:
    """Deterministic float in [0, 1) derived from the arguments."""
    return stable_hash(*parts) / 2**64


def rupees(paise: int) -> float:
    return round(paise / 100.0, 2)


def fmt_inr(paise: int) -> str:
    """Indian-grouped currency string, e.g. 1234567 paise -> Rs 12,345.67."""
    neg = paise < 0
    whole, frac = divmod(abs(int(paise)), 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{'-' if neg else ''}Rs {s}.{frac:02d}"


def month_key(d: date | datetime) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def add_months(d: date, months: int) -> date:
    """Calendar-safe month arithmetic, clamping day-of-month (31 Jan +1m -> 28/29 Feb)."""
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = (nxt - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))


def on_cycle_day(anchor: date, cycle_day: int) -> date:
    """The cycle_day of anchor's month, clamped to the month length."""
    if anchor.month == 12:
        nxt = date(anchor.year + 1, 1, 1)
    else:
        nxt = date(anchor.year, anchor.month + 1, 1)
    last_day = (nxt - timedelta(days=1)).day
    return date(anchor.year, anchor.month, min(cycle_day, last_day))
