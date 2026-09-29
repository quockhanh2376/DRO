from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core import optimizer
from app.core.discovery import DiscoveryError
from app.db.database import Database
from app.db.models import AuditLogRecord, Base, BenchmarkRunRecord, OptimizerStateRecord
from app.db.repositories import save_target
from app.integrations.adguard import AdGuardError
from app.models.benchmark import BenchmarkResult
from app.models.target import Target


def test_existing_adguard_rewrite_is_returned(monkeypatch):
    class Client:
        def get_rewrite(self, hostname):
            assert hostname == "service.example.com"
            return {"domain": hostname, "answer": "192.0.2.186"}

        def close(self):
            pass

    monkeypatch.setattr(optimizer, "configured_adguard_client", Client)
    assert optimizer.read_adguard_rewrite("service.example.com") == ("192.0.2.186", True)


@pytest.mark.parametrize("endpoints", [[], ["http://one", "http://two"]])
def test_adguard_lookup_requires_exactly_one_endpoint(monkeypatch, endpoints):
    def unavailable():
        raise AdGuardError("AdGuard endpoint is unavailable or ambiguous")

    monkeypatch.setattr(optimizer, "configured_adguard_client", unavailable)
    assert optimizer.read_adguard_rewrite("example.com") == (None, False)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        ("No AdGuard endpoint discovered", "no endpoint discovered"),
        ("Multiple AdGuard endpoints discovered", "multiple endpoints discovered"),
    ],
)
def test_adguard_lookup_logs_distinct_endpoint_discovery_failures(monkeypatch, error, message):
    warnings = []
    monkeypatch.setattr(optimizer, "configured_adguard_client",
                        lambda: (_ for _ in ()).throw(AdGuardError(error)))
    monkeypatch.setattr(optimizer.logger, "warning", lambda template, *args: warnings.append(template % args))
    assert optimizer.read_adguard_rewrite("example.com") == (None, False)
    assert message in warnings[0]


@pytest.mark.parametrize("current_is_public", [False, True])
def test_public_ips_remain_distinct_from_ordered_candidates(monkeypatch, tmp_path, current_is_public):
    public = ["1.1.1.1", "2.2.2.2", "1.1.1.1"]
    if current_is_public:
        public.insert(1, "9.9.9.9")

    class Discovery:
        def discover(self, _hostname):
            return public.copy()

        def close(self):
            pass

    class Runner:
        def __init__(self, *_args):
            pass

        def benchmark(self, _hostname, ips, **_kwargs):
            assert ips == list(dict.fromkeys([*public, "9.9.9.9", "8.8.8.8"]))
            return [BenchmarkResult(ip=ip, healthy=True, valid_runs=1, requested_runs=1,
                                    average_ms=10, median_ms=10, min_ms=10, max_ms=10,
                                    jitter_ms=0) for ip in ips]

    monkeypatch.setattr(optimizer, "read_adguard_rewrite", lambda _host: ("9.9.9.9", True))
    monkeypatch.setattr(optimizer, "PublicDnsDiscovery", Discovery)
    monkeypatch.setattr(optimizer, "HttpsBenchmarkRunner", Runner)
    db = Database(f"sqlite:///{(tmp_path / 'optimizer.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    with db.session() as session:
        record = save_target(session, Target(hostname="example.com", manual_lock_ip="8.8.8.8"))
        output = optimizer.run_benchmark_cycle(session, record)
        assert output["public_ips"] == public
        assert output["candidate_ips"] == list(dict.fromkeys([*public, "9.9.9.9", "8.8.8.8"]))
        assert output["current_rewrite_included"] is True
        assert output["current_rewrite_in_public_dns"] is current_is_public
    db.close()


@pytest.mark.parametrize("empty_answer", [False, True])
def test_resolution_failure_skips_benchmark_and_preserves_existing_rewrite(monkeypatch, tmp_path, empty_answer):
    class Discovery:
        def discover(self, _hostname):
            if empty_answer:
                return []
            raise DiscoveryError("public DNS unavailable")

        def close(self):
            pass

    monkeypatch.setattr(optimizer, "PublicDnsDiscovery", Discovery)
    monkeypatch.setattr(optimizer, "read_adguard_rewrite", lambda _host: ("9.9.9.9", True))
    monkeypatch.setattr(optimizer, "HttpsBenchmarkRunner",
                        lambda *_args: pytest.fail("resolution failure must skip network benchmarks"))
    db = Database(f"sqlite:///{(tmp_path / f'resolution-failure-{empty_answer}.db').as_posix()}")
    Base.metadata.create_all(db.engine)

    with db.session() as session:
        target = save_target(session, Target(hostname="go.fyi.appabc", mode="auto", auto_apply=True))
        output = optimizer.run_benchmark_cycle(session, target)
        assert output["decision"]["action"] == "RESOLUTION_FAILED"
        assert output["decision"]["reason"] == "Public DNS discovery failed or returned no valid IP."
        assert output["current_ip"] is None
        assert output["candidate_ips"] == []
        assert output["candidates"] == []
        state = session.get(OptimizerStateRecord, target.id)
        assert state.current_rewrite_ip == "9.9.9.9"
        assert state.last_decision_action == "RESOLUTION_FAILED"

    with db.session() as session:
        run = session.get(BenchmarkRunRecord, output["benchmark_run_id"])
        assert run and run.summary["resolution_failed"] is True
        assert run.summary["current_rewrite_ip"] is None
        assert run.summary["candidate_ips"] == []
        assert run.decision_action == "RESOLUTION_FAILED"
        assert session.scalar(select(func.count()).select_from(AuditLogRecord)) == 1
    db.close()


def test_cycle_persistence_rolls_back_when_audit_write_fails(monkeypatch, tmp_path):
    class Discovery:
        def discover(self, _hostname):
            return ["1.1.1.1"]

        def close(self):
            pass

    class Runner:
        def __init__(self, *_args):
            pass

        def benchmark(self, _hostname, ips, **_kwargs):
            return [BenchmarkResult(ip=ip, healthy=True, valid_runs=1, requested_runs=1,
                                    average_ms=10, median_ms=10, min_ms=10, max_ms=10,
                                    jitter_ms=0) for ip in ips]

    monkeypatch.setattr(optimizer, "read_adguard_rewrite", lambda _host: (None, False))
    monkeypatch.setattr(optimizer, "PublicDnsDiscovery", Discovery)
    monkeypatch.setattr(optimizer, "HttpsBenchmarkRunner", Runner)

    def partial_audit_failure(session, event, target_id, details):
        session.add(AuditLogRecord(event=event, target_id=target_id, details=details))
        session.flush()
        raise RuntimeError("simulated audit persistence failure")

    monkeypatch.setattr(optimizer, "add_audit_event", partial_audit_failure)
    db = Database(f"sqlite:///{(tmp_path / 'rollback.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    with db.session() as session:
        target = save_target(session, Target(hostname="rollback.example.com"))
        target_id = target.id

    with pytest.raises(RuntimeError, match="simulated audit"):
        with db.session() as session:
            target = session.get(type(target), target_id)
            optimizer.run_benchmark_cycle(session, target)

    with db.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(BenchmarkRunRecord)) == 0
        assert session.scalar(select(func.count()).select_from(OptimizerStateRecord)) == 0
        assert session.scalar(select(func.count()).select_from(AuditLogRecord)) == 0
    db.close()


def test_public_candidate_never_replaces_unavailable_authoritative_rewrite(monkeypatch, tmp_path):
    class Discovery:
        def discover(self, _hostname): return ["108.157.32.2"]
        def close(self): pass

    class Runner:
        def __init__(self, *_args): pass
        def benchmark(self, _hostname, ips, **_kwargs):
            assert ips == ["108.157.32.2"]
            return [BenchmarkResult(ip=ips[0], healthy=True, valid_runs=1, requested_runs=1,
                                    average_ms=10, median_ms=10, min_ms=10, max_ms=10,
                                    jitter_ms=0)]

    monkeypatch.setattr(optimizer, "PublicDnsDiscovery", Discovery)
    monkeypatch.setattr(optimizer, "HttpsBenchmarkRunner", Runner)
    monkeypatch.setattr(optimizer, "read_adguard_rewrite", lambda _hostname: (None, False))
    db = Database(f"sqlite:///{(tmp_path / 'optimizer-current-source.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    with db.session() as session:
        target = save_target(session, Target(hostname="source.example"))
        state = OptimizerStateRecord(target_id=target.id, current_rewrite_ip="108.157.32.65")
        session.add(state)
        session.commit()
        output = optimizer.run_benchmark_cycle(session, target)
        assert output["public_ips"] == ["108.157.32.2"]
        assert output["current_ip"] is None
        assert output["candidate_ips"] == ["108.157.32.2"]
        run = session.get(BenchmarkRunRecord, output["benchmark_run_id"])
        assert run.summary["current_rewrite_ip"] is None
        assert run.summary["current_rewrite_lookup_succeeded"] is False
        assert session.get(OptimizerStateRecord, target.id).current_rewrite_ip is None
    db.close()


def test_benchmark_run_persists_aggregated_candidate_failure_summaries(monkeypatch, tmp_path):
    class Discovery:
        def discover(self, _hostname): return ["113.171.12.192", "113.171.12.178"]
        def close(self): pass

    class Runner:
        def __init__(self, *_args): pass
        def benchmark(self, _hostname, ips, **_kwargs):
            return [BenchmarkResult(ip=ip, healthy=False, valid_runs=0, requested_runs=10,
                                    health_reason="10x HTTP 403") for ip in ips]

    monkeypatch.setattr(optimizer, "PublicDnsDiscovery", Discovery)
    monkeypatch.setattr(optimizer, "read_adguard_rewrite", lambda _host: (None, True))
    monkeypatch.setattr(optimizer, "HttpsBenchmarkRunner", Runner)
    db = Database(f"sqlite:///{(tmp_path / 'failure-summary.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    with db.session() as session:
        target = save_target(session, Target(hostname="www.ato.gov.au"))
        output = optimizer.run_benchmark_cycle(session, target)
        run = session.get(BenchmarkRunRecord, output["benchmark_run_id"])
        assert run.summary["candidate_health_reasons"] == {
            "113.171.12.192": "10x HTTP 403", "113.171.12.178": "10x HTTP 403"}
        assert run.summary["failure_summary"] == "10x HTTP 403; 10x HTTP 403"
    db.close()
