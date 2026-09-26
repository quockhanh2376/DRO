"""Safe, bounded cleanup of benchmark data while preserving rewrite history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import BenchmarkResultRecord, BenchmarkRunRecord, BenchmarkSampleRecord
from app.db.repositories import get_setting


def configured_log_retention_days(session: Session) -> int:
    try:
        return max(1, int(get_setting(session, "log_retention_days") or "7"))
    except ValueError:
        return 7


def cleanup_rotated_logs(log_file: str | Path, retention_days: int,
                         now: datetime | None = None) -> int:
    if retention_days < 1:
        raise ValueError("log retention must be at least one day")
    log_file = Path(log_file)
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - retention_days * 86400
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
    now = now or datetime.now(timezone.utc)
    samples_before = now - timedelta(days=30)
    runs_before = now - timedelta(days=180)
    sample_query = delete(BenchmarkSampleRecord).where(BenchmarkSampleRecord.created_at < samples_before)
    run_query = delete(BenchmarkRunRecord).where(BenchmarkRunRecord.completed_at < runs_before)
    old_run_samples = session.scalar(select(func.count()).select_from(BenchmarkSampleRecord).join(
        BenchmarkResultRecord).join(BenchmarkRunRecord).where(
            BenchmarkRunRecord.completed_at < runs_before,
            BenchmarkSampleRecord.created_at >= samples_before,
        )) or 0
    samples = (session.execute(sample_query).rowcount or 0) + old_run_samples
    runs = session.execute(run_query).rowcount or 0
    return {"samples_deleted": samples, "runs_deleted": runs}
