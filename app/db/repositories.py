"""Small SQLAlchemy persistence functions for the optimizer core."""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLogRecord, BenchmarkResultRecord, BenchmarkRunRecord, BenchmarkSampleRecord,
    OptimizerStateRecord, RewriteHistoryRecord, SettingRecord, TargetRecord, utc_now,
)
from app.models.benchmark import BenchmarkResult, DecisionResult, PendingCandidateState
from app.models.target import Target
from app.security import is_secret_key, sanitize_for_storage


def save_target(session: Session, target: Target) -> TargetRecord:
    values = sanitize_for_storage(target.model_dump())
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


def add_rewrite_history(session: Session, target_id: int, old_ip: str | None, new_ip: str,
                        reason: str, benchmark_run_id: int | None = None) -> RewriteHistoryRecord:
    record = RewriteHistoryRecord(target_id=target_id,
                                  old_ip=sanitize_for_storage(old_ip), new_ip=sanitize_for_storage(new_ip),
                                  reason=sanitize_for_storage(reason), benchmark_run_id=benchmark_run_id)
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
