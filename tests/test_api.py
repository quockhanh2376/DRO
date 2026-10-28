from __future__ import annotations

import pytest
import re
from fastapi.testclient import TestClient

from app.api.application import create_app
from app.api import routes
from app.db.database import Database
from app.db.models import Base
from app.db.repositories import add_rewrite_history, save_benchmark_run, save_optimizer_state
from app.models.benchmark import BenchmarkResult, BenchmarkSample, DecisionResult, PendingCandidateState


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DRO_ADMIN_USER", "admin")
    monkeypatch.setenv("DRO_ADMIN_PASSWORD", "test-admin-password")
    database = Database(f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    Base.metadata.create_all(database.engine)

    def benchmark_cycle(session, target):
        sample = BenchmarkSample(ip="1.2.3.4", run_number=1, http_status=200,
                                 connect_ms=2, tls_ms=4, total_ms=12)
        result = BenchmarkResult(ip="1.2.3.4", samples=[sample], valid_runs=1,
                                 requested_runs=1, healthy=False, average_ms=12,
                                 median_ms=12, min_ms=12, max_ms=12, jitter_ms=0)
        decision = DecisionResult(action="KEEP", current_ip="1.2.3.4", reason="Current is best")
        run = save_benchmark_run(session, target.id, [result], {"candidate_count": 1}, decision)
        save_optimizer_state(session, target.id, "1.2.3.4", PendingCandidateState(), decision)
        return {"benchmark_run_id": run.id, "decision": decision.model_dump(),
                "current_ip": "1.2.3.4", "current_rewrite_included": True}

    monkeypatch.setattr(routes, "read_adguard_rewrite", lambda _hostname: (None, False))
    app = create_app(database=database, benchmark_cycle=benchmark_cycle)
    with TestClient(app) as client:
        login_page = client.get("/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
        assert client.post("/login", data={"username": "admin", "password": "test-admin-password",
                                           "csrf_token": csrf}, follow_redirects=False).status_code == 303
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/targets").text).group(1)
        client.headers["x-csrf-token"] = csrf
        yield client, database
    database.close()


def test_health_and_system_status(api):
    client, _ = api
    assert client.get("/health").json() == {"status": "ok"}
    status = client.get("/api/v1/system/status").json()
    assert status["database"] == "ok" and status["target_count"] == 0


def test_target_crud_and_validation_does_not_echo_secrets(api):
    client, _ = api
    assert client.get("/api/v1/targets").json() == []
    created = client.post("/api/v1/targets", json={"hostname": "Example.COM"})
    assert created.status_code == 201 and created.json()["hostname"] == "example.com"
    target_id = created.json()["id"]
    assert client.post("/api/v1/targets", json={"hostname": "example.com"}).status_code == 409
    assert client.get(f"/api/v1/targets/{target_id}").json()["runs_per_ip"] == 10
    updated = client.patch(f"/api/v1/targets/{target_id}",
                           json={"runs_per_ip": 4, "hostname": "renamed.example"})
    assert updated.status_code == 200 and updated.json()["runs_per_ip"] == 4
    assert updated.json()["hostname"] == "renamed.example" and updated.json()["id"] == target_id
    invalid = client.patch(f"/api/v1/targets/{target_id}", json={"port": 99999})
    assert invalid.status_code == 422
    secret = client.post("/api/v1/targets", json={"hostname": "safe.example", "ADGUARD_PASS": "never-return"})
    assert secret.status_code == 422 and "never-return" not in secret.text
    assert client.delete(f"/api/v1/targets/{target_id}").status_code == 400
    assert client.delete(f"/api/v1/targets/{target_id}",
                         headers={"x-confirm-action": "confirm"}).status_code == 204
    assert client.get(f"/api/v1/targets/{target_id}").status_code == 404


def test_manual_run_and_history_are_read_only_views(api):
    client, database = api
    target = client.post("/api/v1/targets", json={"hostname": "history.example"}).json()
    run_result = client.post(f"/api/v1/targets/{target['id']}/run")
    assert run_result.status_code == 200 and run_result.json()["decision"]["action"] == "KEEP"
    run_id = run_result.json()["benchmark_run_id"]
    assert len(client.get(f"/api/v1/targets/{target['id']}/runs").json()) == 1
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["results"][0]["samples"][0]["total_ms"] == 12
    with database.session() as session:
        add_rewrite_history(session, target["id"], "1.2.3.4", "5.6.7.8", "Recorded history", run_id)
    history = client.get(f"/api/v1/targets/{target['id']}/rewrite-history").json()
    assert history[0]["reason"] == "Recorded history"
    rewrite = client.get(f"/api/v1/targets/{target['id']}/rewrite").json()
    assert rewrite == {"target_id": target["id"], "hostname": "history.example",
                       "ip": "1.2.3.4", "source": "database"}
    assert client.get("/api/v1/runs/999").status_code == 404


def test_api_manual_run_rejects_concurrent_target_run(api):
    client, _database = api
    target = client.post("/api/v1/targets", json={"hostname": "api-busy.example"}).json()
    with client.app.state.run_coordinator.run(target["id"]):
        response = client.post(f"/api/v1/targets/{target['id']}/run")
    assert response.status_code == 409
    assert response.json()["detail"] == "A benchmark is already running for this target"


def test_api_runs_and_rewrite_pagination_and_status_auth(api, tmp_path):
    client, database = api
    target = client.post("/api/v1/targets", json={"hostname": "paginate.example"}).json()
    t_id = target["id"]

    # Add 5 benchmark runs and 5 rewrite history entries
    from datetime import datetime, timezone, timedelta
    from app.models.benchmark import BenchmarkResult, DecisionResult
    now = datetime.now(timezone.utc)
    with database.session() as session:
        for i in range(5):
            res = BenchmarkResult(ip="1.2.3.4", valid_runs=1, requested_runs=1, healthy=True, average_ms=10.0 + i)
            dec = DecisionResult(action="KEEP", reason=f"run-{i}")
            run = save_benchmark_run(session, t_id, [res], {}, dec)
            run.completed_at = now - timedelta(minutes=10 - i)
            add_rewrite_history(session, t_id, f"1.1.1.{i}", f"1.1.1.{i+1}", f"reason-{i}")
        session.commit()

    # Test limit and offset on runs
    runs_p1 = client.get(f"/api/v1/targets/{t_id}/runs?limit=2&offset=0").json()
    assert len(runs_p1) == 2
    assert runs_p1[0]["decision_reason"] == "run-4"  # most recent
    assert runs_p1[1]["decision_reason"] == "run-3"

    runs_p2 = client.get(f"/api/v1/targets/{t_id}/runs?limit=2&offset=2").json()
    assert len(runs_p2) == 2
    assert runs_p2[0]["decision_reason"] == "run-2"
    assert runs_p2[1]["decision_reason"] == "run-1"

    runs_p3 = client.get(f"/api/v1/targets/{t_id}/runs?limit=2&offset=4").json()
    assert len(runs_p3) == 1
    assert runs_p3[0]["decision_reason"] == "run-0"

    # Test limit and offset on rewrite-history
    rewrites_p1 = client.get(f"/api/v1/targets/{t_id}/rewrite-history?limit=2&offset=0").json()
    assert len(rewrites_p1) == 2
    assert rewrites_p1[0]["reason"] == "reason-4"

    rewrites_p2 = client.get(f"/api/v1/targets/{t_id}/rewrite-history?limit=2&offset=2").json()
    assert len(rewrites_p2) == 2
    assert rewrites_p2[0]["reason"] == "reason-2"

    # Test parameter validation limits
    assert client.get(f"/api/v1/targets/{t_id}/runs?limit=0").status_code == 422
    assert client.get(f"/api/v1/targets/{t_id}/runs?limit=501").status_code == 422
    assert client.get(f"/api/v1/targets/{t_id}/runs?offset=-1").status_code == 422

    # Test system/status requires authentication (Item 4)
    with TestClient(create_app(database=database)) as unauth_client:
        # Public health endpoint is 200
        assert unauth_client.get("/health").status_code == 200
        # system/status requires admin auth
        assert unauth_client.get("/api/v1/system/status").status_code == 401
