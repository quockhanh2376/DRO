"""Safe, bounded cleanup of benchmark data while preserving rewrite history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import BenchmarkResultRecord, BenchmarkRunRecord, BenchmarkSampleRecord
from app.db.repositories import get_setting
from app.time_utils import as_utc_aware


def configured_log_retention_days(session: Session) -> int:
    try:
        return max(1, int(get_setting(session, "log_retention_days") or "7"))
    except ValueError:
        return 7


def configured_benchmark_history_retention(session: Session) -> tuple[int, str]:
    """Return the persisted benchmark retention, defaulting safely to 72 hours."""
    try:
        value = int(get_setting(session, "benchmark_history_retention_value") or "72")
        unit = get_setting(session, "benchmark_history_retention_unit") or "hours"
        if value < 1 or unit not in {"hours", "days"}:
            raise ValueError
        return value, unit
    except (TypeError, ValueError):
        return 72, "hours"


def benchmark_history_retention(session: Session) -> timedelta:
    value, unit = configured_benchmark_history_retention(session)
    return timedelta(**({"hours": value} if unit == "hours" else {"days": value}))


def _delete_benchmark_runs_before(session: Session, cutoff: datetime) -> dict[str, int]:
    old_runs = select(BenchmarkRunRecord.id).where(BenchmarkRunRecord.completed_at < cutoff)
    old_results = select(BenchmarkResultRecord.id).where(BenchmarkResultRecord.run_id.in_(old_runs))
    samples = session.execute(delete(BenchmarkSampleRecord).where(
        BenchmarkSampleRecord.result_id.in_(old_results))).rowcount or 0
    results = session.execute(delete(BenchmarkResultRecord).where(
        BenchmarkResultRecord.run_id.in_(old_runs))).rowcount or 0
    runs = session.execute(delete(BenchmarkRunRecord).where(
        BenchmarkRunRecord.id.in_(old_runs))).rowcount or 0
    return {"samples_deleted": samples, "results_deleted": results, "runs_deleted": runs}


def clear_benchmark_history(session: Session) -> dict[str, int]:
    """Delete benchmark data only; caller's DB transaction makes the operation atomic."""
    return _delete_benchmark_runs_before(session, datetime.max.replace(tzinfo=timezone.utc))


def cleanup_rotated_logs(log_file: str | Path, retention_days: int,
                         now: datetime | None = None) -> int:
    if retention_days < 1:
        raise ValueError("log retention must be at least one day")
    log_file = Path(log_file)
    cutoff = as_utc_aware(now or datetime.now(timezone.utc)).timestamp() - retention_days * 86400
    deleted = 0
    for path in log_file.parent.glob(f"{log_file.name}.*"):
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except FileNotFoundError:
            continue
    return deleted


def cleanup_retention(session: Session, now: datetime | None = None) -> dict[str, int]:
    now = as_utc_aware(now or datetime.now(timezone.utc))
    return _delete_benchmark_runs_before(session, now - benchmark_history_retention(session))
