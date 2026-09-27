"""Timezone helpers: persist instants in UTC and display operational time in Vietnam."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def as_utc_aware(value: datetime) -> datetime:
    """Normalize timestamps; naive values from SQLite are historical UTC values."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_vietnam_time(value: datetime) -> datetime:
    return as_utc_aware(value).astimezone(VIETNAM_TZ)


def format_vietnam_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    return to_vietnam_time(value).strftime("%Y-%m-%d %H:%M:%S ICT")


def next_run_time(completed_at: datetime, interval_hours: float) -> datetime:
    """Calculate the interval from the local operational clock, returning UTC for storage."""
    local_run = to_vietnam_time(completed_at)
    return (local_run + timedelta(hours=interval_hours)).astimezone(UTC)
