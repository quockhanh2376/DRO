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


@pytest.mark.parametrize("endpoints", [[], ["http://one", "http://two"]])
def test_adguard_lookup_requires_exactly_one_endpoint(monkeypatch, endpoints):
    def unavailable():
        raise AdGuardError("AdGuard endpoint is unavailable or ambiguous")

    monkeypatch.setattr(optimizer, "configured_adguard_client", unavailable)
    assert optimizer.read_adguard_rewrite("example.com") == (None, False)


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


def test_discovery_failure_keeps_current_rewrite_and_persists_failed_run(monkeypatch, tmp_path):
    class Discovery:
        def discover(self, _hostname):
            raise DiscoveryError("public DNS unavailable")

        def close(self):
            pass

    monkeypatch.setattr(optimizer, "PublicDnsDiscovery", Discovery)
    monkeypatch.setattr(optimizer, "read_adguard_rewrite", lambda _host: ("9.9.9.9", True))
    monkeypatch.setattr(optimizer, "HttpsBenchmarkRunner",
                        lambda *_args: pytest.fail("DNS failure must skip network benchmarks"))
    db = Database(f"sqlite:///{(tmp_path / 'discovery-failure.db').as_posix()}")
    Base.metadata.create_all(db.engine)

    with db.session() as session:
        target = save_target(session, Target(hostname="discovery-failure.example"))
        output = optimizer.run_benchmark_cycle(session, target)
        assert output["decision"]["action"] == "KEEP"
        assert output["decision"]["current_ip"] == "9.9.9.9"
        assert output["candidate_ips"] == ["9.9.9.9"]
        assert output["candidates"] == []

    with db.session() as session:
        run = session.get(BenchmarkRunRecord, output["benchmark_run_id"])
        assert run and run.summary["discovery_error"] == "public DNS unavailable"
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
