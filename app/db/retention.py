"""Safe, bounded cleanup of benchmark data while preserving rewrite history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import BenchmarkResultRecord, BenchmarkRunRecord, BenchmarkSampleRecord


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
