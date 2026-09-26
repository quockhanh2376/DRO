from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.application import create_app
from app.db.database import Database
from app.db.models import Base, TargetRecord
from app.db.repositories import add_rewrite_history, save_benchmark_run, save_optimizer_state, save_target
from app.models.benchmark import BenchmarkResult, DecisionResult, PendingCandidateState
from app.models.target import Target
from sqlalchemy import select


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("ADGUARD_URL", "http://admin:ui-secret@adguard.example")
    database = Database(f"sqlite:///{(tmp_path / 'web.db').as_posix()}")
    Base.metadata.create_all(database.engine)
    app = create_app(database=database, benchmark_cycle=lambda *_args: {})
    with TestClient(app) as client:
        yield client, database
    database.close()


def test_dashboard_and_targets_render(web):
    client, _database = web
    assert client.get("/").status_code == 200
    assert "Dashboard" in client.get("/").text
    response = client.get("/targets")
    assert response.status_code == 200 and "Add target" in response.text


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
    assert edit.status_code == 200 and 'value="4"' in edit.text and "recommend" in edit.text
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
    assert "Run now" in detail.text and "Initial rewrite" in detail.text
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
