from __future__ import annotations

import os
import stat
import pytest

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select

from app.db.database import Database
from app.db.models import (
    AuditLogRecord, Base, BenchmarkResultRecord, BenchmarkRunRecord,
    BenchmarkSampleRecord, OptimizerStateRecord, RewriteHistoryRecord, TargetRecord,
)
from app.db.repositories import (
    add_audit_event, add_rewrite_history, get_pending_state, get_target,
    save_benchmark_run, save_optimizer_state, save_target, get_setting, set_setting,
)
from app.models.benchmark import BenchmarkResult, BenchmarkSample, DecisionResult, PendingCandidateState
from app.models.target import Target


def test_target_create_read_update_and_optimizer_state_survives_sessions(tmp_path):
    db_path = tmp_path / "dro.db"
    db = Database(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(db.engine)
    if os.name == "posix":
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    with db.session() as session:
        row = save_target(session, Target(hostname="persist.example.com"))
        target_id = row.id
    with db.session() as session:
        target = get_target(session, "persist.example.com")
        assert target and target.runs_per_ip == 10
        updated = save_target(session, Target(hostname="persist.example.com", runs_per_ip=5))
        assert updated.id == target_id and updated.runs_per_ip == 5
        state = save_optimizer_state(
            session, target_id, "1.2.3.4",
            PendingCandidateState(candidate_ip="5.6.7.8", consecutive_wins=1),
            DecisionResult(action="HOLD", candidate_ip="5.6.7.8", current_ip="1.2.3.4",
                           reason="Qualifying win", wins=1),
        )
        assert state.last_decision_reason == "Qualifying win"
    with db.session() as new_session:
        assert get_pending_state(new_session, target_id) == PendingCandidateState(
            candidate_ip="5.6.7.8", consecutive_wins=1)
        state = new_session.get(OptimizerStateRecord, target_id)
        assert state and state.current_rewrite_ip == "1.2.3.4"
    db.close()


def test_save_benchmark_results_samples_history_and_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("ADGUARD_PASS", "db-secret")
    db = Database(f"sqlite:///{(tmp_path / 'results.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    with db.session() as session:
        target = save_target(session, Target(hostname="bench.example.com"))
        sample = BenchmarkSample(ip="1.2.3.4", run_number=1, http_status=200,
                                 connect_ms=2, tls_ms=4, total_ms=12,
                                 error="Authorization: Basic sample-secret")
        result = BenchmarkResult(ip="1.2.3.4", samples=[sample], valid_runs=1,
                                 requested_runs=1, healthy=False, average_ms=12,
                                 median_ms=12, min_ms=12, max_ms=12, jitter_ms=0)
        decision = DecisionResult(action="KEEP", current_ip="1.2.3.4", reason="Current is best")
        run = save_benchmark_run(session, target.id, [result],
                                 {"candidate_count": 1, "ADGUARD_PASS": "db-secret"}, decision)
        history = add_rewrite_history(session, target.id, "1.2.3.4", "5.6.7.8", "Manual validation", run.id)
        add_audit_event(session, "benchmark_completed", target.id,
                        {"run_id": run.id, "nested": {"Authorization": "header-secret"}})
        set_setting(session, "display_mode", "password=config-secret")
        set_setting(session, "free_text", "db-secret")
        with pytest.raises(ValueError):
            set_setting(session, "ADGUARD_PASS", "db-secret")
        run_id, target_id, history_id = run.id, target.id, history.id
    with db.session() as session:
        run = session.get(BenchmarkRunRecord, run_id)
        assert run and run.summary == {"candidate_count": 1}
        assert run.decision_reason == "Current is best"
        result = session.scalar(select(BenchmarkResultRecord).where(BenchmarkResultRecord.run_id == run_id))
        assert result and result.ip == "1.2.3.4" and result.average_ms == 12
        sample_row = session.scalar(select(BenchmarkSampleRecord).where(BenchmarkSampleRecord.result_id == result.id))
        assert sample_row and sample_row.total_ms == 12
        assert "sample-secret" not in (sample_row.error or "")
        history = session.get(RewriteHistoryRecord, history_id)
        assert history and history.old_ip == "1.2.3.4" and history.new_ip == "5.6.7.8"
        audit = session.scalar(select(AuditLogRecord).where(AuditLogRecord.target_id == target_id))
        assert audit and audit.details == {"run_id": run_id, "nested": {}}
        assert get_setting(session, "display_mode") == "password=[REDACTED]"
        assert get_setting(session, "free_text") == "[REDACTED]"
        session.delete(sample_row)
    with db.session() as session:
        assert session.get(RewriteHistoryRecord, history_id) is not None
    db.close()


def test_initial_alembic_upgrade_downgrade_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "migration.db"
    monkeypatch.setenv("DRO_DB_PATH", str(path))
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.check(config)
    engine_db = Database(f"sqlite:///{path.as_posix()}")
    table_names = set(inspect(engine_db.engine).get_table_names())
    assert {"targets", "benchmark_runs", "benchmark_results", "benchmark_samples",
            "optimizer_state", "rewrite_history", "settings", "audit_log"}.issubset(table_names)
    columns = {column["name"] for table in table_names for column in inspect(engine_db.engine).get_columns(table)}
    assert "password" not in columns
    assert "password_hash" in columns
    target_columns = {column["name"]: column
                      for column in inspect(engine_db.engine).get_columns("targets")}
    assert target_columns["auto_apply"]["nullable"] is False
    assert target_columns["auto_apply"]["default"] in ("0", "false", "FALSE")
    indexes = {index["name"] for table in ("benchmark_runs", "rewrite_history")
               for index in inspect(engine_db.engine).get_indexes(table)}
    assert {"ix_benchmark_runs_target_completed", "ix_rewrite_history_target_created"} <= indexes
    interval_type = next(column["type"] for column in inspect(engine_db.engine).get_columns("targets")
                         if column["name"] == "interval_hours")
    assert "FLOAT" in str(interval_type).upper()
    engine_db.close()
    command.downgrade(config, "base")
    engine_db = Database(f"sqlite:///{path.as_posix()}")
    assert inspect(engine_db.engine).get_table_names() == ["alembic_version"]
    engine_db.close()
    command.upgrade(config, "head")
    engine_db = Database(f"sqlite:///{path.as_posix()}")
    assert "optimizer_state" in inspect(engine_db.engine).get_table_names()
    engine_db.close()
