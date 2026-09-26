"""Small SQLAlchemy persistence functions for the optimizer core."""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.db.models import (
    AuditLogRecord, BenchmarkResultRecord, BenchmarkRunRecord, BenchmarkSampleRecord,
    OptimizerStateRecord, RewriteHistoryRecord, SettingRecord, TargetRecord, utc_now,
)
from app.models.benchmark import BenchmarkResult, DecisionResult, PendingCandidateState
from app.models.target import Target
from app.security import is_secret_key, sanitize_for_storage


def save_target(session: Session, target: Target, record: TargetRecord | None = None) -> TargetRecord:
    values = sanitize_for_storage(target.model_dump())
    if record is None:
        record = session.scalar(select(TargetRecord).where(TargetRecord.hostname == target.hostname))
    if record is None:
        record = TargetRecord(**values)
        session.add(record)
    else:
        for name, value in values.items():
            setattr(record, name, value)
        record.updated_at = utc_now()
    session.flush()
    return record


def get_target(session: Session, hostname: str) -> TargetRecord | None:
    return session.scalar(select(TargetRecord).where(TargetRecord.hostname == hostname))


def get_target_by_id(session: Session, target_id: int) -> TargetRecord | None:
    return session.get(TargetRecord, target_id)


def list_targets(session: Session) -> list[TargetRecord]:
    return list(session.scalars(select(TargetRecord).order_by(TargetRecord.hostname)).all())


def delete_target(session: Session, record: TargetRecord) -> None:
    session.delete(record)


def save_benchmark_run(session: Session, target_id: int, results: Sequence[BenchmarkResult],
                       summary: dict[str, Any] | None = None,
                       decision: DecisionResult | None = None,
                       include_samples: bool = True) -> BenchmarkRunRecord:
    run = BenchmarkRunRecord(target_id=target_id, summary=sanitize_for_storage(summary or {}),
                             decision_action=decision.action if decision else None,
                             decision_reason=sanitize_for_storage(decision.reason) if decision else None)
    for result in results:
        saved = BenchmarkResultRecord(
            ip=sanitize_for_storage(result.ip), valid_runs=result.valid_runs, requested_runs=result.requested_runs,
            healthy=result.healthy, average_ms=result.average_ms, median_ms=result.median_ms,
            min_ms=result.min_ms, max_ms=result.max_ms, jitter_ms=result.jitter_ms,
        )
        if include_samples:
            saved.samples = [BenchmarkSampleRecord(**{
                **sample.model_dump(exclude={"ip"}),
                "error": sanitize_for_storage(sample.error),
            })
                             for sample in result.samples]
        run.results.append(saved)
    session.add(run)
    session.flush()
    return run


def get_pending_state(session: Session, target_id: int) -> PendingCandidateState:
    state = session.get(OptimizerStateRecord, target_id)
    return PendingCandidateState(candidate_ip=state.pending_candidate_ip,
                                 consecutive_wins=state.consecutive_wins) if state else PendingCandidateState()


def get_current_rewrite_ip(session: Session, target_id: int) -> str | None:
    state = session.get(OptimizerStateRecord, target_id)
    return state.current_rewrite_ip if state else None


def save_optimizer_state(session: Session, target_id: int, current_ip: str | None,
                         pending: PendingCandidateState, decision: DecisionResult) -> OptimizerStateRecord:
    state = session.get(OptimizerStateRecord, target_id)
    if state is None:
        state = OptimizerStateRecord(target_id=target_id)
        session.add(state)
    state.current_rewrite_ip = sanitize_for_storage(current_ip)
    state.pending_candidate_ip = sanitize_for_storage(pending.candidate_ip)
    state.consecutive_wins = pending.consecutive_wins
    state.last_decision_action = decision.action
    state.last_decision_reason = sanitize_for_storage(decision.reason)
    state.updated_at = utc_now()
    session.flush()
    return state


def add_rewrite_history(session: Session, target_id: int, old_ip: str | None, new_ip: str | None,
                        reason: str, benchmark_run_id: int | None = None,
                        automatic: bool = False) -> RewriteHistoryRecord:
    record = RewriteHistoryRecord(target_id=target_id,
                                  old_ip=sanitize_for_storage(old_ip), new_ip=sanitize_for_storage(new_ip),
                                  reason=sanitize_for_storage(reason), benchmark_run_id=benchmark_run_id,
                                  automatic=automatic)
    session.add(record)
    session.flush()
    return record


def set_setting(session: Session, key: str, value: str) -> SettingRecord:
    if is_secret_key(key):
        raise ValueError("setting keys cannot contain secret fields")
    value = sanitize_for_storage(value)
    setting = session.get(SettingRecord, key)
    if setting is None:
        setting = SettingRecord(key=key, value=value)
        session.add(setting)
    else:
        setting.value = value
        setting.updated_at = utc_now()
    session.flush()
    return setting


def get_setting(session: Session, key: str) -> str | None:
    setting = session.get(SettingRecord, key)
    return setting.value if setting else None


def add_audit_event(session: Session, event: str, target_id: int | None = None,
                    details: dict[str, Any] | None = None) -> AuditLogRecord:
    record = AuditLogRecord(target_id=target_id, event=event, details=sanitize_for_storage(details or {}))
    session.add(record)
    session.flush()
    return record


def list_benchmark_runs(session: Session, target_id: int) -> list[BenchmarkRunRecord]:
    return list(session.scalars(select(BenchmarkRunRecord).where(BenchmarkRunRecord.target_id == target_id)
                                .order_by(BenchmarkRunRecord.completed_at.desc())).all())


def get_benchmark_run(session: Session, run_id: int) -> BenchmarkRunRecord | None:
    return session.scalar(select(BenchmarkRunRecord).options(
        selectinload(BenchmarkRunRecord.results).selectinload(BenchmarkResultRecord.samples)
    ).where(BenchmarkRunRecord.id == run_id))


def list_rewrite_history(session: Session, target_id: int) -> list[RewriteHistoryRecord]:
    return list(session.scalars(select(RewriteHistoryRecord).where(RewriteHistoryRecord.target_id == target_id)
                                .order_by(RewriteHistoryRecord.created_at.desc())).all())


def count_automatic_rewrites_since(session: Session, since) -> int:
    return session.scalar(select(func.count()).select_from(RewriteHistoryRecord).where(
        RewriteHistoryRecord.automatic.is_(True), RewriteHistoryRecord.created_at >= since)) or 0


def count_targets(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(TargetRecord)) or 0


def count_benchmark_runs(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(BenchmarkRunRecord)) or 0
