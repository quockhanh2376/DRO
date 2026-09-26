from __future__ import annotations

import pytest
import re
from fastapi.testclient import TestClient

from app.api.application import create_app
from app.core import rewrites
from app.db.database import Database
from app.db.models import Base, TargetRecord
from app.db.repositories import (add_rewrite_history, get_setting, save_benchmark_run,
                                 save_optimizer_state, save_target)
from app.models.benchmark import BenchmarkResult, DecisionResult, PendingCandidateState
from app.models.target import Target
from sqlalchemy import select


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("DRO_ADMIN_USER", "admin")
    monkeypatch.setenv("DRO_ADMIN_PASSWORD", "test-admin-password")
    monkeypatch.setenv("ADGUARD_URL", "http://admin:ui-secret@adguard.example")
    database = Database(f"sqlite:///{(tmp_path / 'web.db').as_posix()}")
    Base.metadata.create_all(database.engine)
    app = create_app(database=database, benchmark_cycle=lambda *_args: {})
    with TestClient(app) as client:
        login_page = client.get("/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        assert client.post("/login", data={"username": "admin", "password": "test-admin-password",
                                           "csrf_token": csrf}, follow_redirects=False).status_code == 303
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/targets").text).group(1)
        client.headers["x-csrf-token"] = csrf
        yield client, database
    database.close()


def test_dashboard_and_targets_render(web):
    client, _database = web
    assert client.get("/").status_code == 200
    assert "Dashboard" in client.get("/").text
    response = client.get("/targets")
    assert response.status_code == 200 and "Add target" in response.text
    with _database.session() as session:
        save_target(session, Target(hostname="listed.example"))
    targets_page = client.get("/targets").text
    assert "Run Now" in targets_page
    assert 'hx-target="#run-result"' in targets_page and 'hx-swap="innerHTML"' in targets_page
    assert "Running benchmark..." in targets_page
    assert targets_page.index("</table>") < targets_page.index('id="run-result"')


def test_create_edit_target_forms_and_validation(web):
    client, _database = web
    created = client.post("/targets", data={"hostname": "Example.com", "enabled": "true",
                                             "mode": "recommend", "interval_hours": "4",
                                             "runs_per_ip": "6", "switch_threshold_ms": "42",
                                             "switch_threshold_percent": "7"}, follow_redirects=True)
    assert created.status_code == 200 and "example.com" in created.text
    with _database.session() as session:
        target_id = session.scalar(select(TargetRecord.id))
    edit = client.get(f"/targets/{target_id}/edit")
    assert edit.status_code == 200 and 'value="4.0"' in edit.text and "recommend" in edit.text
    updated = client.post(f"/targets/{target_id}/edit", data={"hostname": "changed.example",
                                                                "enabled": "true", "mode": "auto",
                                                                "interval_hours": "3", "runs_per_ip": "5",
                                                                "switch_threshold_ms": "50",
                                                                "switch_threshold_percent": "5"},
                          follow_redirects=True)
    assert updated.status_code == 200 and "changed.example" in updated.text
    invalid = client.post("/targets", data={"hostname": "not a hostname", "mode": "auto"})
    assert invalid.status_code == 422 and "not a hostname" in invalid.text


def test_target_detail_history_and_no_secrets_in_html(web):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="detail.example"))
        result = BenchmarkResult(ip="1.2.3.4", valid_runs=10, requested_runs=10, healthy=True,
                                average_ms=10, median_ms=9, min_ms=8, max_ms=12, jitter_ms=1)
        decision = DecisionResult(action="KEEP", current_ip="1.2.3.4", reason="Current is best")
        run = save_benchmark_run(session, target.id, [result],
                                 {"public_ips": ["1.2.3.4"], "candidate_ips": ["1.2.3.4"]}, decision)
        save_optimizer_state(session, target.id, "1.2.3.4", PendingCandidateState(), decision)
        add_rewrite_history(session, target.id, None, "1.2.3.4", "Initial rewrite", run.id)
        target_id = target.id
    detail = client.get(f"/targets/{target_id}")
    assert detail.status_code == 200
    assert "1.2.3.4" in detail.text and "Current is best" in detail.text
    assert "Run Now" in detail.text and "Initial rewrite" in detail.text
    assert "ui-secret" not in detail.text and "admin" not in detail.text
    settings = client.get("/settings")
    assert settings.status_code == 200 and "AdGuard URL" in settings.text
    assert "ui-secret" not in settings.text and "admin" not in settings.text
    history = client.get("/history")
    assert history.status_code == 200 and "detail.example" in history.text


def test_target_pages_not_found_and_manual_run_redirect(web):
    client, _database = web
    assert client.get("/targets/999999").status_code == 404
    assert client.get("/targets/999999/edit").status_code == 404
    created = client.post("/targets", data={"hostname": "run.example"}, follow_redirects=False)
    assert created.status_code == 303
    assert client.post("/targets/1/run", follow_redirects=False).status_code == 303


def test_settings_default_interval_conversion_applies_only_to_new_targets(web):
    client, database = web
    invalid = client.post("/settings/default-interval", data={"value": "0", "unit": "minutes"})
    assert invalid.status_code == 422 and "positive interval" in invalid.text
    conversions = [("30", "minutes", 0.5), ("2", "hours", 2.0), ("6", "hours", 6.0)]
    for index, (value, unit, expected_hours) in enumerate(conversions):
        response = client.post("/settings/default-interval", data={"value": value, "unit": unit},
                               follow_redirects=False)
        assert response.status_code == 303
        created = client.post("/api/v1/targets", json={"hostname": f"default-{index}.example"})
        assert created.status_code == 201
        assert created.json()["interval_hours"] == expected_hours

    form_page = client.get("/targets")
    assert 'name="interval_hours" min="0" step="any" required value="6.0"' in form_page.text
    client.post("/settings/default-interval", data={"value": "30", "unit": "minutes"})
    created_from_form = client.post("/targets", data={"hostname": "form-default.example"},
                                    follow_redirects=False)
    assert created_from_form.status_code == 303
    with database.session() as session:
        form_target = session.scalar(select(TargetRecord).where(TargetRecord.hostname == "form-default.example"))
        assert form_target and form_target.interval_hours == 0.5

    custom = client.post("/api/v1/targets", json={"hostname": "custom-interval.example",
                                                    "interval_hours": 4.0}).json()
    client.post("/settings/default-interval", data={"value": "30", "unit": "minutes"})
    assert client.get(f"/api/v1/targets/{custom['id']}").json()["interval_hours"] == 4.0
    with database.session() as session:
        assert get_setting(session, "default_interval_value") == "30.0"
        assert get_setting(session, "default_interval_unit") == "minutes"


def test_scheduler_setting_persists_and_run_now_works_when_disabled(web):
    client, database = web
    target = client.post("/api/v1/targets", json={"hostname": "manual-while-paused.example"}).json()
    calls = []
    client.app.state.benchmark_cycle = lambda *_args: calls.append("run") or {}
    with database.session() as session:
        assert get_setting(session, "scheduler_enabled") is None
    assert "Disabled" in client.get("/settings").text
    response = client.post(f"/targets/{target['id']}/run", follow_redirects=False)
    assert response.status_code == 303 and calls == ["run"]

    client.post("/settings/scheduler", data={"enabled": "true"})
    with database.session() as session:
        assert get_setting(session, "scheduler_enabled") == "true"
    assert "Enabled" in client.get("/settings").text
    client.post("/settings/scheduler", data={"enabled": "false"})
    with database.session() as session:
        assert get_setting(session, "scheduler_enabled") == "false"
    assert "Disabled" in client.get("/settings").text


def test_log_retention_setting_persists_and_rejects_less_than_one_day(web):
    client, database = web
    saved = client.post("/settings/log-retention", data={"days": "14"}, follow_redirects=False)
    assert saved.status_code == 303
    with database.session() as session:
        assert get_setting(session, "log_retention_days") == "14"
    page = client.get("/settings")
    assert 'name="days" min="1"' in page.text and 'value="14"' in page.text

    invalid = client.post("/settings/log-retention", data={"days": "0"})
    assert invalid.status_code == 422 and "at least 1 day" in invalid.text
    with database.session() as session:
        assert get_setting(session, "log_retention_days") == "14"


def test_run_now_shows_benchmark_result_without_rewrite(web, monkeypatch):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="manual-run.example", mode="auto"))
        target_id = target.id
    current = BenchmarkResult(ip="192.0.2.21", valid_runs=10, requested_runs=10, healthy=True,
                              average_ms=20, median_ms=19, min_ms=18, max_ms=22, jitter_ms=2)
    best = BenchmarkResult(ip="192.0.2.22", valid_runs=10, requested_runs=10, healthy=True,
                           average_ms=10, median_ms=9, min_ms=8, max_ms=12, jitter_ms=1)
    decision = DecisionResult(action="UPDATE", current_ip=current.ip, candidate_ip=best.ip,
                              reason="Candidate is materially faster")

    def benchmark_cycle(session, target):
        run = save_benchmark_run(session, target.id, [current, best], {
            "current_rewrite_ip": current.ip,
            "public_ips": [current.ip, best.ip],
            "candidate_ips": [current.ip, best.ip],
        }, decision)
        save_optimizer_state(session, target.id, current.ip,
                             PendingCandidateState(candidate_ip=best.ip, consecutive_wins=2), decision)
        return {"benchmark_run_id": run.id, "decision": decision.model_dump()}

    client.app.state.benchmark_cycle = benchmark_cycle
    monkeypatch.setattr(rewrites, "set_rewrite", lambda *_args, **_kwargs: pytest.fail("DNS write attempted"))
    response = client.post(f"/targets/{target_id}/run", headers={"HX-Request": "true"})
    assert response.status_code == 200
    detail = response.text
    for expected in (current.ip, best.ip, "Best IP", "Avg", "Median", "Min", "Max", "Jitter",
                     "UPDATE", "Candidate is materially faster", "manual-run.example",
                     "Current rewrite IP", "Discovered candidates", "Run timestamp"):
        assert expected in detail
    assert 'class="best-candidate"' in detail and 'data-run-id="1"' in detail


def test_run_now_rejects_concurrent_target_run(web):
    client, database = web
    with database.session() as session:
        target = save_target(session, Target(hostname="busy.example"))
        target_id = target.id
    with client.app.state.run_coordinator.run(target_id):
        response = client.post(f"/targets/{target_id}/run")
    assert response.status_code == 409
