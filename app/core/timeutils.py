"""Timezone-aware time helpers. Everything is stored and compared in UTC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import overload


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


@overload
def ensure_utc(value: datetime) -> datetime: ...


@overload
def ensure_utc(value: None) -> None: ...


def ensure_utc(value: datetime | None) -> datetime | None:
    """Normalise a datetime to aware UTC.

    Naive values read back from databases that dropped the tzinfo are assumed
    to already be UTC, which matches how we always write them.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def from_timestamp(value: int | float | str, *, unit: str = "s") -> datetime:
    """Convert a provider epoch timestamp into aware UTC."""
    number = float(value)
    if unit == "ms":
        number /= 1000
    elif unit == "us":
        number /= 1_000_000
    elif unit == "ns":
        number /= 1_000_000_000
    elif unit != "s":
        raise ValueError(f"unsupported timestamp unit: {unit}")
    return datetime.fromtimestamp(number, tz=UTC)


def to_millis(value: datetime) -> int:
    return int(ensure_utc(value).timestamp() * 1000)


def is_expired(expires_at: datetime | None, *, now: datetime | None = None) -> bool:
    if expires_at is None:
        return False
    return (now or utcnow()) >= ensure_utc(expires_at)


def format_countdown(expires_at: datetime | None, *, now: datetime | None = None) -> str:
    """``MM:SS`` remaining, clamped at zero. Used on payment screens."""
    if expires_at is None:
        return "--:--"
    remaining = ensure_utc(expires_at) - (now or utcnow())
    seconds = max(0, int(remaining.total_seconds()))
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def humanize_datetime(value: datetime | None) -> str:
    if value is None:
        return "-"
    return ensure_utc(value).strftime("%Y-%m-%d %H:%M UTC")


def short_date(value: datetime | None) -> str:
    if value is None:
        return "-"
    return ensure_utc(value).strftime("%Y-%m-%d")


__all__ = [
    "UTC",
    "ensure_utc",
    "format_countdown",
    "from_timestamp",
    "humanize_datetime",
    "is_expired",
    "short_date",
    "timedelta",
    "to_millis",
    "utcnow",
]
