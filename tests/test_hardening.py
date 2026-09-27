from __future__ import annotations

import re
import sqlite3
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api.application import create_app
from app.core import rewrites
from app.core.scheduler import RunAlreadyActive, RunCoordinator, run_due, set_scheduler_enabled
from app.db.database import Database
from app.db.models import (AdminCredentialRecord, AuditLogRecord, Base, BenchmarkRunRecord,
                           BenchmarkResultRecord, BenchmarkSampleRecord, OptimizerStateRecord,
                           RewriteHistoryRecord, ScheduleStateRecord, TargetRecord)
from app.db.repositories import (add_rewrite_history, get_setting, save_benchmark_run,
                                 save_optimizer_state, save_target, set_setting)
from app.db.retention import (benchmark_history_retention, cleanup_retention,
                             cleanup_rotated_logs, configured_benchmark_history_retention)
from app.maintenance import backup_database, restore_database, validate_database
from app.integrations.adguard import AdGuardClient, AdGuardError
from app.models.benchmark import BenchmarkResult, DecisionResult, PendingCandidateState
from app.models.target import Target


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DRO_ADMIN_USER", "admin")
    monkeypatch.setenv("DRO_ADMIN_PASSWORD", "strong-test-password-123")
    monkeypatch.setenv("DRO_SESSION_SECRET", "test-session-signing-secret-long-enough")
    monkeypatch.setenv("DRO_HTTPS_ENABLED", "true")
    database = Database(f"sqlite:///{(tmp_path / 'secure.db').as_posix()}")
    Base.metadata.create_all(database.engine)
    with TestClient(create_app(database=database, benchmark_cycle=lambda *_args: {}),
                    base_url="https://testserver") as client:
        login = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
        cookie = login.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie
        assert client.post("/login", data={"username": "admin", "password": "strong-test-password-123",
                                           "csrf_token": token}, follow_redirects=False).status_code == 303
        token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/targets").text).group(1)
        client.headers["x-csrf-token"] = token
        yield client, database
    database.close()


def test_login_logout_hash_and_csrf(admin_client):
    client, database = admin_client
    with database.session() as session:
        credential = session.get(AdminCredentialRecord, 1)
        assert credential and "scrypt$" in credential.password_hash
        assert "strong-test-password-123" not in credential.password_hash
    assert client.get("/api/v1/targets").status_code == 200
    assert client.post("/api/v1/targets", json={"hostname": "missing-csrf.example"},
                       headers={"x-csrf-token": "invalid"}).status_code == 403
    assert client.post("/logout", data={"csrf_token": "invalid"},
                       headers={"x-csrf-token": "invalid"}, follow_redirects=False).status_code == 403
    assert client.post("/logout", headers={"x-csrf-token": client.headers["x-csrf-token"]},
                       follow_redirects=False).status_code == 303
    assert client.get("/api/v1/targets").status_code == 401


def test_unauthenticated_and_missing_csrf_mutations_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DRO_ADMIN_USER", "admin")
    monkeypatch.setenv("DRO_ADMIN_PASSWORD", "strong-test-password-123")
    db = Database(f"sqlite:///{(tmp_path / 'csrf.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    with TestClient(create_app(database=db)) as client:
        page_response = client.get("/", follow_redirects=False)
        assert page_response.status_code == 303
        assert page_response.headers["location"] == "/login"
        assert client.get("/login").status_code == 200
        assert client.get("/api/v1/targets").status_code == 401
        assert client.get("/api/v1/targets").json() == {"detail": "Authentication required"}
        assert client.post("/api/v1/targets", json={"hostname": "blocked.example"}).status_code == 401
        page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        assert client.post("/login", data={"username": "admin", "password": "strong-test-password-123",
                                           "csrf_token": token}, follow_redirects=False).status_code == 303
        assert client.post("/api/v1/targets", json={"hostname": "blocked.example"}).status_code == 403
    db.close()


def test_lock_and_unlock_are_audited(admin_client):
    client, database = admin_client
    created = client.post("/api/v1/targets", json={"hostname": "locked.example"}).json()
    target_id = created["id"]
    locked = client.post(f"/api/v1/targets/{target_id}/lock", json={"ip": "192.0.2.10"})
    assert locked.status_code == 200 and locked.json()["manual_lock_ip"] == "192.0.2.10"
    assert client.post(f"/api/v1/targets/{target_id}/unlock").json()["manual_lock_ip"] is None
    with database.session() as session:
        events = list(session.scalars(select(AuditLogRecord.event).where(
            AuditLogRecord.target_id == target_id, AuditLogRecord.event.in_(["ip_locked", "ip_unlocked"]))))
        assert events == ["ip_locked", "ip_unlocked"]


def test_manual_rollback_requires_confirmation_and_restores_previous_ip(admin_client, monkeypatch):
    client, database = admin_client
    target_id = client.post("/api/v1/targets", json={"hostname": "rollback.example"}).json()["id"]
    with database.session() as session:
        save_optimizer_state(session, target_id, "192.0.2.20", PendingCandidateState(),
                             DecisionResult(action="KEEP", reason="seed"))
        add_rewrite_history(session, target_id, "192.0.2.10", "192.0.2.20", "Applied")
    from app.api import routes
    calls = []
    monkeypatch.setattr(routes, "read_adguard_rewrite", lambda _host: ("192.0.2.20", True))
    monkeypatch.setattr(routes, "set_rewrite", lambda _session, _record, new_ip, reason, run_id:
                        calls.append((new_ip, reason, run_id)) or {"healthy": True})
    assert client.post(f"/api/v1/targets/{target_id}/rollback", json={"confirm": False}).status_code == 400
    response = client.post(f"/api/v1/targets/{target_id}/rollback", json={"confirm": True})
    assert response.status_code == 200 and calls[0][0] == "192.0.2.10"


def test_failed_post_change_health_immediately_rolls_back(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{(tmp_path / 'rollback.db').as_posix()}")
    Base.metadata.create_all(db.engine)

    class FakeAdGuard:
        ip = "192.0.2.1"
        def get_rewrite(self, _host): return {"answer": self.ip}
        def update_rewrite(self, _old_host, _old_ip, _host, ip): self.ip = ip
        def add_rewrite(self, _host, ip): self.ip = ip
        def delete_rewrite(self, _host, _ip): self.ip = None
        def close(self): pass

    client = FakeAdGuard()
    monkeypatch.setattr(rewrites, "_adguard_client", lambda: client)
    monkeypatch.setattr(rewrites, "_healthy", lambda _target, _ip: False)
    with db.session_factory() as session:
        target = save_target(session, Target(hostname="auto-rollback.example"))
        session.commit()
        outcome = rewrites.set_rewrite(session, target, "192.0.2.2", "test candidate")
        assert outcome["rolled_back"] and client.ip == "192.0.2.1"
        assert outcome["verified_current_ip"] == client.ip
        state = session.get(OptimizerStateRecord, target.id)
        assert state and state.current_rewrite_ip == "192.0.2.1"
        history = list(session.scalars(select(RewriteHistoryRecord).order_by(RewriteHistoryRecord.id)))
        assert [item.new_ip for item in history] == ["192.0.2.2", "192.0.2.1"]
        assert session.scalar(select(AuditLogRecord.event).where(AuditLogRecord.event == "rewrite_rollback"))
    db.close()


def test_successful_rewrite_updates_existing_and_verifies_before_persisting(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{(tmp_path / 'rewrite-success.db').as_posix()}")
    Base.metadata.create_all(db.engine)

    class FakeAdGuard:
        ip = "192.0.2.1"
        updates = 0
        adds = 0

        def get_rewrite(self, _host): return {"answer": self.ip}
        def update_rewrite(self, _old_host, old_ip, _host, ip):
            assert self.ip == old_ip
            self.updates += 1
            self.ip = ip
        def add_rewrite(self, _host, ip): self.adds += 1; self.ip = ip
        def delete_rewrite(self, _host, _ip): self.ip = None
        def close(self): pass

    client = FakeAdGuard()
    monkeypatch.setattr(rewrites, "_adguard_client", lambda: client)
    monkeypatch.setattr(rewrites, "_healthy", lambda _target, _ip: True)
    with db.session_factory() as session:
        target = save_target(session, Target(hostname="manual-apply.example"))
        session.commit()
        result = rewrites.set_rewrite(session, target, "192.0.2.2", "Manual Apply Best IP")
        assert result["healthy"] and result["changed"]
        assert result["verified_current_ip"] == client.ip == "192.0.2.2"
        assert client.ip == "192.0.2.2" and client.updates == 1 and client.adds == 0
        history = session.scalar(select(RewriteHistoryRecord).where(
            RewriteHistoryRecord.target_id == target.id))
        assert history and history.old_ip == "192.0.2.1" and history.new_ip == "192.0.2.2"
        assert session.scalar(select(AuditLogRecord.event).where(
            AuditLogRecord.target_id == target.id, AuditLogRecord.event == "rewrite_applied"))
    db.close()


def test_adguard_http_update_failure_does_not_persist_false_rewrite_history(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{(tmp_path / 'rewrite-http-failure.db').as_posix()}")
    Base.metadata.create_all(db.engine)

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("rewrite/list"):
            return httpx.Response(200, json=[{"domain": "http-failure.example", "answer": "192.0.2.1"}])
        if request.method == "PUT" and request.url.path.endswith("rewrite/update"):
            return httpx.Response(500, json={"error": "write failed"})
        return httpx.Response(200, json={"ok": True})

    client = AdGuardClient("http://adguard", client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(rewrites, "_adguard_client", lambda: client)
    with db.session_factory() as session:
        target = save_target(session, Target(hostname="http-failure.example"))
        session.commit()
        with pytest.raises(AdGuardError, match="PUT rewrite/update failed"):
            rewrites.set_rewrite(session, target, "192.0.2.2", "Manual Apply Best IP")
        assert session.scalar(select(RewriteHistoryRecord.id)) is None
        assert session.scalar(select(AuditLogRecord.event).where(
            AuditLogRecord.event == "rewrite_applied")) is None
    db.close()


def test_duplicate_rewrite_is_rejected_without_adding_or_updating(tmp_path, monkeypatch):
    from app.integrations.adguard import AdGuardError

    db = Database(f"sqlite:///{(tmp_path / 'rewrite-duplicates.db').as_posix()}")
    Base.metadata.create_all(db.engine)

    class DuplicateAdGuard:
        writes = 0
        def get_rewrite(self, _host): raise AdGuardError("multiple rewrites for domain")
        def update_rewrite(self, *_args): self.writes += 1
        def add_rewrite(self, *_args): self.writes += 1
        def close(self): pass

    client = DuplicateAdGuard()
    monkeypatch.setattr(rewrites, "_adguard_client", lambda: client)
    with db.session_factory() as session:
        target = save_target(session, Target(hostname="duplicate.example"))
        session.commit()
        with pytest.raises(AdGuardError, match="multiple rewrites"):
            rewrites.set_rewrite(session, target, "192.0.2.2", "Manual Apply Best IP")
        assert client.writes == 0
        assert session.scalar(select(RewriteHistoryRecord.id)) is None
    db.close()


def test_manual_apply_rejects_rewrite_changed_since_benchmark(tmp_path, monkeypatch):
    from app.integrations.adguard import AdGuardError

    db = Database(f"sqlite:///{(tmp_path / 'rewrite-stale.db').as_posix()}")
    Base.metadata.create_all(db.engine)

    class FakeAdGuard:
        writes = 0
        def get_rewrite(self, _host): return {"answer": "192.0.2.9"}
        def update_rewrite(self, *_args): self.writes += 1
        def add_rewrite(self, *_args): self.writes += 1
        def close(self): pass

    client = FakeAdGuard()
    monkeypatch.setattr(rewrites, "_adguard_client", lambda: client)
    with db.session_factory() as session:
        target = save_target(session, Target(hostname="stale-rewrite.example"))
        session.commit()
        with pytest.raises(AdGuardError, match="changed since the benchmark"):
            rewrites.set_rewrite(session, target, "192.0.2.2", "Manual Apply Best IP",
                                 expected_old_ip="192.0.2.1")
        assert client.writes == 0
        assert session.scalar(select(RewriteHistoryRecord.id)) is None
    db.close()


def test_database_failure_after_rewrite_compensates_external_change(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{(tmp_path / 'persist-before-dns.db').as_posix()}")
    Base.metadata.create_all(db.engine)

    class FakeAdGuard:
        ip = "192.0.2.1"
        def get_rewrite(self, _host): return {"answer": self.ip}
        def update_rewrite(self, _old_host, _old_ip, _host, ip): self.ip = ip
        def add_rewrite(self, _host, ip): self.ip = ip
        def delete_rewrite(self, _host, _ip): self.ip = None
        def close(self): pass

    client = FakeAdGuard()
    monkeypatch.setattr(rewrites, "_adguard_client", lambda: client)
    monkeypatch.setattr(rewrites, "_healthy", lambda _target, _ip: True)
    with db.session() as session:
        target = save_target(session, Target(hostname="persist-before-dns.example"))
        target_id = target.id

    with db.session_factory() as session:
        target = session.get(TargetRecord, target_id)
        commit = session.commit
        commit_count = 0

        def fail_after_external_write():
            nonlocal commit_count
            commit_count += 1
            if commit_count == 2:
                raise RuntimeError("simulated final database failure")
            commit()

        monkeypatch.setattr(session, "commit", fail_after_external_write)
        with pytest.raises(RuntimeError, match="final database failure"):
            rewrites.set_rewrite(session, target, "192.0.2.2", "test")

    assert client.ip == "192.0.2.1"
    with db.session() as session:
        assert session.get(OptimizerStateRecord, target_id) is None
        assert session.scalar(select(RewriteHistoryRecord.id)) is None
        assert session.scalar(select(AuditLogRecord.event).where(
            AuditLogRecord.event == "rewrite_change_started"))
    db.close()


def test_daily_auto_change_limit_falls_back_to_recommend(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{(tmp_path / 'limit.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    monkeypatch.setattr(rewrites, "_adguard_client", lambda: pytest.fail("must not write DNS"))
    with db.session() as session:
        target = save_target(session, Target(hostname="limited.example", mode="auto", auto_apply=True))
        set_setting(session, "global_auto_master", "true")
        set_setting(session, "max_auto_changes_per_day", "1")
        add_rewrite_history(session, target.id, "192.0.2.1", "192.0.2.2", "previous", automatic=True)
        output = rewrites.apply_automatic_decision(session, target, {
            "decision": {"action": "UPDATE", "candidate_ip": "192.0.2.3"}})
        assert output["change_limited"] is True
        assert "AUTO treated as RECOMMEND" in output["reason"]
        assert session.scalar(select(AuditLogRecord.event).where(
            AuditLogRecord.event == "auto_change_limited"))
    db.close()


def test_locked_ip_blocks_automatic_rewrite(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{(tmp_path / 'locked-auto.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    monkeypatch.setattr(rewrites, "_adguard_client", lambda: pytest.fail("locked target cannot write DNS"))
    with db.session() as session:
        target = save_target(session, Target(hostname="locked-auto.example", mode="auto", auto_apply=True,
                                             manual_lock_ip="192.0.2.1"))
        set_setting(session, "global_auto_master", "true")
        result = rewrites.apply_automatic_decision(session, target, {
            "decision": {"action": "UPDATE", "candidate_ip": "192.0.2.2"}})
        assert result["rewrite_applied"] is False
        assert "manual IP lock" in result["reason"]
    db.close()


@pytest.mark.parametrize(("master", "target_opt_in", "should_write"), [
    (False, True, False),
    (True, False, False),
    (True, True, True),
])
def test_automatic_rewrite_requires_global_and_per_target_opt_in(
        tmp_path, monkeypatch, master, target_opt_in, should_write):
    db = Database(f"sqlite:///{(tmp_path / f'auto-{master}-{target_opt_in}.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    writes = []
    monkeypatch.setattr(rewrites, "set_rewrite", lambda *_args, **_kwargs:
                        writes.append(True) or {"changed": True, "healthy": True})
    with db.session() as session:
        target = save_target(session, Target(hostname="dual-gate.example", mode="auto",
                                             auto_apply=target_opt_in))
        if master:
            set_setting(session, "global_auto_master", "true")
        result = rewrites.apply_automatic_decision(session, target, {
            "decision": {"action": "UPDATE", "candidate_ip": "192.0.2.9", "reason": "safe win"}})
        assert bool(writes) is should_write
        assert result["rewrite_applied"] is should_write
        if not should_write:
            assert result["change_limited"] is False
            assert session.scalar(select(AuditLogRecord.event).where(
                AuditLogRecord.event == "auto_change_blocked"))
    db.close()


def test_scheduler_persists_times_and_guards_duplicate_runs(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'scheduler.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    with db.session() as session:
        target = save_target(session, Target(hostname="scheduled.example", interval_hours=3))
        target_id = target.id
    coordinator = RunCoordinator()
    with coordinator.run(target_id), pytest.raises(RunAlreadyActive):
        with coordinator.run(target_id):
            pass
    runs = []
    def cycle(_session, _target):
        runs.append(1)
        return {"decision": {"action": "KEEP"}}
    assert run_due(db, cycle, coordinator) == 0
    assert runs == []
    with db.session() as session:
        set_scheduler_enabled(session, True)
    with db.session() as session:
        assert get_setting(session, "scheduler_enabled") == "true"
    assert run_due(db, cycle, coordinator) == 1
    assert run_due(db, cycle, coordinator) == 0
    assert len(runs) == 1
    with db.session() as session:
        state = session.get(ScheduleStateRecord, target_id)
        assert state and state.last_run_at and state.next_run_at - state.last_run_at == timedelta(hours=3)
    db.close()


def test_log_retention_removes_only_old_rotated_files(tmp_path):
    now = datetime.now(timezone.utc)
    active = tmp_path / "dro.log"
    old_log = tmp_path / "dro.log.2.gz"
    recent_log = tmp_path / "dro.log.1"
    for path in (active, old_log, recent_log):
        path.write_text("log")
    os.utime(old_log, (now.timestamp() - 2 * 86400,) * 2)
    os.utime(recent_log, (now.timestamp() - 12 * 3600,) * 2)
    assert cleanup_rotated_logs(active, 1, now) == 1
    assert active.exists() and recent_log.exists() and not old_log.exists()
    with pytest.raises(ValueError):
        cleanup_rotated_logs(active, 0, now)


def test_retention_deletes_old_benchmark_trees_and_keeps_rewrite_history(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'retention.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    now = datetime.now(timezone.utc)
    with db.session() as session:
        target = save_target(session, Target(hostname="retention.example"))
        old_run = BenchmarkRunRecord(target_id=target.id, completed_at=now - timedelta(days=181))
        session.add(old_run)
        session.flush()
        result = BenchmarkResultRecord(run_id=old_run.id, ip="192.0.2.1", valid_runs=1,
                                       requested_runs=1, healthy=False)
        session.add(result)
        session.flush()
        session.add(BenchmarkSampleRecord(result_id=result.id, run_number=1,
                                          created_at=now - timedelta(days=31)))
        history = add_rewrite_history(session, target.id, None, "192.0.2.1", "retain", old_run.id)
        history_id = history.id
        recent_run = BenchmarkRunRecord(target_id=target.id, completed_at=now)
        session.add(recent_run)
        session.flush()
        recent_result = BenchmarkResultRecord(run_id=recent_run.id, ip="192.0.2.2", valid_runs=1,
                                              requested_runs=1, healthy=False)
        session.add(recent_result)
        session.flush()
        session.add(BenchmarkSampleRecord(result_id=recent_result.id, run_number=1,
                                          created_at=now - timedelta(days=2)))
        result_counts = cleanup_retention(session, now)
        assert result_counts == {"samples_deleted": 1, "results_deleted": 1, "runs_deleted": 1}
        assert session.get(RewriteHistoryRecord, history_id) is not None
        assert session.get(BenchmarkRunRecord, recent_run.id) is not None
        assert session.scalar(select(BenchmarkSampleRecord.id)) is not None
    db.close()


def test_default_72_hour_retention_cleans_old_run_tree_and_preserves_fk_integrity(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'retention-72h.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    now = datetime.now(timezone.utc)
    with db.session() as session:
        assert configured_benchmark_history_retention(session) == (72, "hours")
        assert benchmark_history_retention(session) == timedelta(hours=72)
        target = save_target(session, Target(hostname="72hours.example"))
        old = BenchmarkRunRecord(target_id=target.id, completed_at=now - timedelta(hours=73))
        recent = BenchmarkRunRecord(target_id=target.id, completed_at=now - timedelta(hours=71))
        session.add_all([old, recent])
        session.flush()
        old_result = BenchmarkResultRecord(run_id=old.id, ip="192.0.2.10", valid_runs=1,
                                           requested_runs=1, healthy=False)
        recent_result = BenchmarkResultRecord(run_id=recent.id, ip="192.0.2.11", valid_runs=1,
                                              requested_runs=1, healthy=True)
        session.add_all([old_result, recent_result])
        session.flush()
        session.add_all([BenchmarkSampleRecord(result_id=old_result.id, run_number=1),
                         BenchmarkSampleRecord(result_id=recent_result.id, run_number=1)])
        result = cleanup_retention(session, now)
        assert result == {"samples_deleted": 1, "results_deleted": 1, "runs_deleted": 1}
        assert session.get(BenchmarkRunRecord, old.id) is None
        assert session.get(BenchmarkRunRecord, recent.id) is not None
        assert session.scalar(select(BenchmarkSampleRecord.id).where(
            BenchmarkSampleRecord.result_id == recent_result.id)) is not None
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    db.close()


def test_sqlite_backup_restore_and_invalid_restore_validation(tmp_path):
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    live = tmp_path / "live.db"
    source_db = Database(f"sqlite:///{source.as_posix()}")
    live_db = Database(f"sqlite:///{live.as_posix()}")
    Base.metadata.create_all(source_db.engine)
    Base.metadata.create_all(live_db.engine)
    with source_db.session() as session:
        save_target(session, Target(hostname="backup.example"))
    with live_db.session() as session:
        save_target(session, Target(hostname="replace.example"))
    source_db.close()
    live_db.close()
    backup_database(source, backup)
    with pytest.raises(FileExistsError):
        backup_database(source, backup)
    with pytest.raises(PermissionError):
        restore_database(backup, live)
    restore_database(backup, live, confirmed=True)
    validate_database(live)
    with sqlite3.connect(live) as connection:
        names = {row[0] for row in connection.execute("SELECT hostname FROM targets")}
    assert names == {"backup.example"}
    invalid = tmp_path / "invalid.db"
    invalid.write_text("not a db")
    with pytest.raises(ValueError, match="Invalid SQLite"):
        restore_database(invalid, live, confirmed=True)
