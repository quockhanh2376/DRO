from __future__ import annotations

import json
import subprocess
import logging

import httpx
import pytest

from app.core.benchmark import calculate_statistics
from app.core.diagnostics import ping_ipv4
from app.core.decision import DecisionEngine, rank_candidates
from app.core.discovery import DiscoveryError, PublicDnsDiscovery
from app.integrations.adguard import (
    AdGuardClient, AdGuardError, configured_adguard_client, discover_adguard,
)
from app.integrations import adguard as adguard_module
from app.models.benchmark import BenchmarkResult, BenchmarkSample, PendingCandidateState
from app.models.target import Target
from app.core import benchmark as benchmark_module
from app.core import optimizer as optimizer_module
from app import main as main_module
from app.security import load_secret_environment, redact_secrets
from app.db.database import Database
from app.db.models import Base
from app.db.repositories import get_pending_state


def result(ip: str, avg: float, healthy: bool = True, median: float | None = None, jitter: float = 1) -> BenchmarkResult:
    return BenchmarkResult(ip=ip, healthy=healthy, average_ms=avg, median_ms=median if median is not None else avg,
                           jitter_ms=jitter, valid_runs=10, requested_runs=10)


def test_target_defaults_and_validation():
    target = Target(hostname="Example.COM")
    assert target.hostname == "example.com"
    assert target.auto_apply is False
    assert (target.interval_hours, target.runs_per_ip, target.switch_threshold_ms,
            target.switch_threshold_percent, target.required_consecutive_wins) == (2, 10, 50, 5, 2)
    for kwargs in ({"hostname": "bad host"}, {"hostname": "https://example.com"},
                   {"hostname": "example.com/path"}, {"hostname": "bad_name.example"},
                   {"hostname": "127.0.0.1"}, {"hostname": "localhost"},
                   {"hostname": "x.com", "port": 65536}, {"hostname": "x.com", "runs_per_ip": 0},
                   {"hostname": "x.com", "timeout_seconds": 0},
                   {"hostname": "x.com", "switch_threshold_percent": 101}):
        with pytest.raises(ValueError):
            Target(**kwargs)


def test_statistics_and_health_threshold():
    samples = [BenchmarkSample(ip="1.2.3.4", run_number=i, http_status=200, connect_ms=1, tls_ms=2,
                               total_ms=float(i)) for i in range(1, 11)]
    stats = calculate_statistics(samples)
    assert stats.healthy and stats.valid_runs == 10
    assert stats.average_ms == pytest.approx(5.5)
    assert stats.median_ms == pytest.approx(5.5)
    assert stats.min_ms == 1 and stats.max_ms == 10
    assert stats.jitter_ms == pytest.approx(2.872281323)
    assert not calculate_statistics(samples[:7] + [BenchmarkSample(ip="1.2.3.4", run_number=8)] * 3, 10).healthy


def test_statistics_edge_cases_do_not_require_pstdev_for_zero_or_one_sample(monkeypatch):
    one = BenchmarkSample(ip="1.2.3.4", run_number=1, http_status=200,
                          connect_ms=1, tls_ms=2, total_ms=3)
    original_pstdev = benchmark_module.statistics.pstdev

    def fail_for_single_sample(values):
        if len(values) < 2:
            raise benchmark_module.statistics.StatisticsError("at least two samples required")
        return original_pstdev(values)

    monkeypatch.setattr(benchmark_module.statistics, "pstdev", fail_for_single_sample)
    assert calculate_statistics([]).jitter_ms is None
    assert calculate_statistics([one]).jitter_ms == 0.0
    assert calculate_statistics([one, one.model_copy(update={"run_number": 2, "total_ms": 5})]).jitter_ms == 1.0


def test_candidate_ranking():
    ranked = rank_candidates([result("1.1.1.1", 100, median=95, jitter=8),
                              result("2.2.2.2", 100, median=90, jitter=9),
                              result("3.3.3.3", 100, median=90, jitter=2), result("4.4.4.4", 1, False)])
    assert [r.ip for r in ranked] == ["3.3.3.3", "2.2.2.2", "1.1.1.1"]


def test_candidate_ranking_accepts_missing_median_or_jitter():
    candidates = [
        BenchmarkResult(ip="1.1.1.1", healthy=True, average_ms=10, median_ms=None, jitter_ms=None),
        BenchmarkResult(ip="2.2.2.2", healthy=True, average_ms=10, median_ms=20, jitter_ms=1),
    ]
    assert [candidate.ip for candidate in rank_candidates(candidates)] == ["2.2.2.2", "1.1.1.1"]


def test_decision_keep_hold_update_threshold_and_streak_reset():
    engine = DecisionEngine()
    current, candidate = result("1.1.1.1", 1000), result("2.2.2.2", 980)
    assert engine.decide(current.ip, current, [current, candidate]).action == "KEEP"  # below 50 ms and 5%
    candidate = result("2.2.2.2", 940)
    first = engine.decide(current.ip, current, [current, candidate])
    assert first.action == "HOLD" and first.wins == 1
    second = engine.decide(current.ip, current, [current, candidate], PendingCandidateState(candidate_ip=candidate.ip, consecutive_wins=1))
    assert second.action == "UPDATE" and second.wins == 2
    # 50 ms threshold qualifies despite less than 5 percent.
    assert engine.decide(current.ip, current, [current, result("3.3.3.3", 950)]).action == "HOLD"
    # 5 percent threshold qualifies even when improvement is under 50 ms.
    assert engine.decide(current.ip, result(current.ip, 100), [result(current.ip, 100), result("4.4.4.4", 94)]).action == "HOLD"
    reset = engine.decide(current.ip, current, [current, result("3.3.3.3", 940)],
                          PendingCandidateState(candidate_ip=candidate.ip, consecutive_wins=1))
    assert reset.action == "HOLD" and reset.wins == 1 and reset.candidate_ip == "3.3.3.3"
    assert engine.decide(current.ip, current, [current]).action == "KEEP"


def test_failover_and_manual_lock():
    engine = DecisionEngine()
    current, alternative = result("1.1.1.1", 100, healthy=False), result("2.2.2.2", 150)
    assert engine.decide(current.ip, current, [current, alternative]).action == "FAILOVER"
    assert engine.decide(current.ip, current, [current, alternative], manual_lock_ip=current.ip).action == "LOCKED"


@pytest.mark.parametrize("manual_lock_ip", [None, "", "  ", "192.0.2.10"])
def test_manual_lock_requires_non_whitespace_ip(manual_lock_ip):
    engine = DecisionEngine()
    current = result("192.0.2.1", 100)
    alternative = result("192.0.2.2", 40)
    decision = engine.decide(current.ip, current, [current, alternative], manual_lock_ip=manual_lock_ip)
    if manual_lock_ip and manual_lock_ip.strip():
        assert decision.action == "LOCKED"
    else:
        assert decision.action == "HOLD"


def test_failover_does_not_consume_pending_wins():
    engine = DecisionEngine()
    current = result("192.0.2.1", 100, healthy=False)
    alternative = result("192.0.2.2", 90)
    decision = engine.decide(
        current.ip, current, [current, alternative],
        pending=PendingCandidateState(candidate_ip=alternative.ip, consecutive_wins=1),
    )
    assert decision.action == "FAILOVER"
    assert decision.wins == 0


def test_doh_follows_cname_deduplicates_and_skips_invalid_records():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["name"] == "example.com":
            return httpx.Response(200, json={"Status": 0, "Answer": [
                {"type": 5, "data": "edge.example.net."}, {"type": 1, "data": "1.2.3.4"},
                {"type": 1, "data": "bad"}, {"type": 28, "data": "::1"}]})
        return httpx.Response(200, json={"Status": 0, "Answer": [
            {"type": 1, "data": "1.2.3.4"}, {"type": 1, "data": "5.6.7.8"}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert PublicDnsDiscovery(client).discover("example.com") == ["1.2.3.4", "5.6.7.8"]


def test_malformed_doh_raises_clear_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"Answer": "bad"})))
    with pytest.raises(DiscoveryError, match="Malformed DNS-over-HTTPS"):
        PublicDnsDiscovery(client).discover("example.com")


def test_adguard_mocked_api_operations():
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("rewrite/list"):
            return httpx.Response(200, json=[{"domain": "example.com", "answer": "1.2.3.4"}])
        return httpx.Response(200, json={"ok": True})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    adguard = AdGuardClient("http://adguard", "user", "secret", client)
    assert adguard.list_rewrites()[0]["domain"] == "example.com"
    assert adguard.get_rewrite("example.com")["answer"] == "1.2.3.4"
    adguard.add_rewrite("a.com", "1.1.1.1")
    adguard.update_rewrite("a.com", "1.1.1.1", "a.com", "2.2.2.2")
    adguard.delete_rewrite("a.com", "2.2.2.2")
    assert len(calls) == 5
    update = next(request for request in calls if request.url.path.endswith("rewrite/update"))
    assert update.method == "PUT"
    assert json.loads(update.content) == {
        "target": {"domain": "a.com", "answer": "1.1.1.1"},
        "update": {"domain": "a.com", "answer": "2.2.2.2", "enabled": True},
    }
    assert not any(request.method == "POST" and request.url.path.endswith("rewrite/update")
                   for request in calls)
    assert "secret" not in repr(adguard)


def test_adguard_rewrite_lookup_normalizes_hostname():
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(
        200, json=[{"domain": "Service.Example.com.", "answer": "192.0.2.186"}])))
    adguard = AdGuardClient("http://adguard", client=client)
    assert adguard.get_rewrite("service.example.com")["answer"] == "192.0.2.186"


def test_ping_ipv4_validates_and_parses_linux_output(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0,
            "4 packets transmitted, 4 received, 0% packet loss\\nrtt min/avg/max/mdev = 1.2/2.3/4.5/0.6 ms\\n", "")

    monkeypatch.setattr("app.core.diagnostics.subprocess.run", run)
    result = ping_ipv4("192.0.2.4")
    assert result.status == "OK" and result.packet_loss_percent == 0
    assert (result.min_ms, result.avg_ms, result.max_ms, result.mdev_ms) == (1.2, 2.3, 4.5, 0.6)
    assert calls[0][0] == ["ping", "-c", "4", "-W", "1", "192.0.2.4"]
    assert calls[0][1]["timeout"] == 8 and calls[0][1]["capture_output"] is True
    assert "shell" not in calls[0][1]

    monkeypatch.setattr("app.core.diagnostics.subprocess.run", lambda args, **_kwargs:
                        subprocess.CompletedProcess(args, 1,
                            "4 packets transmitted, 2 received, 50% packet loss\\n"
                            "rtt min/avg/max/mdev = 2.0/3.0/4.0/0.5 ms\\n", ""))
    assert ping_ipv4("192.0.2.4").status == "Warning"


def test_ping_ipv4_rejects_invalid_or_ipv6_before_subprocess(monkeypatch):
    monkeypatch.setattr("app.core.diagnostics.subprocess.run",
                        lambda *_args, **_kwargs: pytest.fail("invalid IP must not be pinged"))
    with pytest.raises(ValueError):
        ping_ipv4("not-an-ip")
    with pytest.raises(ValueError, match="IPv4"):
        ping_ipv4("2001:db8::1")


@pytest.mark.parametrize("endpoints", [[], ["http://one", "http://two"], ["http://one"]])
def test_configured_adguard_client_requires_exactly_one_endpoint(monkeypatch, endpoints):
    monkeypatch.setattr(adguard_module, "load_secret_environment", lambda: None)
    monkeypatch.setattr(adguard_module, "discover_adguard", lambda _url: endpoints)
    monkeypatch.setattr(adguard_module, "AdGuardClient", lambda base_url: base_url)
    if len(endpoints) == 1:
        assert configured_adguard_client() == endpoints[0]
    else:
        expected = "No AdGuard endpoint discovered" if not endpoints else "Multiple AdGuard endpoints discovered"
        with pytest.raises(AdGuardError, match=expected):
            configured_adguard_client()


def test_adguard_malformed_or_ambiguous_rewrite_response_fails_safely():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json=[{"domain": "example.com", "answer": "1.2.3.4"},
                                                    {"domain": "example.com", "answer": "5.6.7.8"}])))
    adguard = AdGuardClient("http://adguard", client=client)
    with pytest.raises(AdGuardError, match="multiple rewrites"):
        adguard.get_rewrite("example.com")

    malformed = httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"not-json")))
    with pytest.raises(AdGuardError, match="malformed JSON"):
        AdGuardClient("http://adguard", client=malformed).list_rewrites()


def test_benchmark_errors_redact_query_secrets():
    def failed(command, **_kwargs):
        return subprocess.CompletedProcess(command, 2, "", "failed https://example.com/?token=private-value")

    result = benchmark_module.HttpsBenchmarkRunner(runs_per_ip=1, command_runner=failed).benchmark_ip(
        "example.com", "1.2.3.4", path="/?token=private-value")
    assert result.samples[0].error == "failed https://example.com/?token=[REDACTED]"


def test_adguard_discovery_uses_configured_url_without_scanning():
    calls = []
    found = discover_adguard("http://adguard.local/", subnet="192.168.1.0/24",
                             probe=lambda url: calls.append(url) or False)
    assert found == ["http://adguard.local"]
    assert calls == []


def test_adguard_discovery_detects_localhost():
    found = discover_adguard(probe=lambda url: url == "http://127.0.0.1")
    assert found == ["http://127.0.0.1"]


def test_adguard_discovery_no_instance_found():
    assert discover_adguard(probe=lambda _url: False) == []


def test_adguard_subnet_discovery_returns_multiple_choices():
    found = discover_adguard(subnet="192.0.2.0/30", probe=lambda _url: True)
    assert len(found) == 4  # two localhost candidates and two subnet hosts
    assert len(set(found)) == len(found)


def test_secret_env_file_loading_and_precedence(tmp_path, monkeypatch):
    env_file = tmp_path / "dro.env"
    env_file.write_text("ADGUARD_USER=file-user\nADGUARD_PASS='file-secret'\n", encoding="utf-8")
    monkeypatch.setenv("ADGUARD_USER", "environment-user")
    monkeypatch.delenv("ADGUARD_PASS", raising=False)
    load_secret_environment(env_file)
    assert __import__("os").environ["ADGUARD_USER"] == "environment-user"
    assert __import__("os").environ["ADGUARD_PASS"] == "file-secret"


def test_redaction_hides_password_and_authorization(caplog):
    secret_text = "password=hunter2 Authorization: Bearer abc123"
    redacted = redact_secrets(secret_text)
    assert "hunter2" not in redacted and "abc123" not in redacted
    assert "[REDACTED]" in redacted
    with caplog.at_level(logging.ERROR):
        logger = logging.getLogger("dro.security.test")
        logger.error("%s", redact_secrets(secret_text))
    assert "hunter2" not in caplog.text and "abc123" not in caplog.text


def test_adguard_error_does_not_expose_credentials(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="password=body-secret Authorization: Basic header-secret")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    adguard = AdGuardClient("http://adguard", "user", "stored-secret", client)
    with caplog.at_level(logging.ERROR), pytest.raises(Exception) as error:
        adguard.list_rewrites()
    assert "stored-secret" not in repr(adguard)
    assert "body-secret" not in str(error.value) and "header-secret" not in str(error.value)
    assert "stored-secret" not in caplog.text and "header-secret" not in caplog.text


def test_schannel_retry_only_on_revocation_offline(monkeypatch):
    monkeypatch.setattr(benchmark_module.os, "name", "nt")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 35, "", "CRYPT_E_REVOCATION_OFFLINE")
        return subprocess.CompletedProcess(command, 0, "200\t0.01\t0.02\t0.03", "")
    result = benchmark_module.HttpsBenchmarkRunner(runs_per_ip=1, command_runner=run).benchmark_ip("example.com", "1.2.3.4")
    assert result.valid_runs == 1
    assert "--ssl-no-revoke" not in calls[0]
    assert "--ssl-no-revoke" in calls[1]
    assert "-k" not in calls[1]


def test_other_windows_tls_failures_do_not_retry(monkeypatch):
    monkeypatch.setattr(benchmark_module.os, "name", "nt")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 35, "", "certificate verify failed")
    result = benchmark_module.HttpsBenchmarkRunner(runs_per_ip=1, command_runner=run).benchmark_ip("example.com", "1.2.3.4")
    assert result.valid_runs == 0 and len(calls) == 1
    assert "--ssl-no-revoke" not in calls[0]


def test_cli_includes_current_rewrite_and_decides(monkeypatch, capsys, tmp_path):
    class FakeAdGuard:
        def __init__(self, base_url=None):
            pass
        def get_rewrite(self, hostname):
            return {"domain": hostname, "answer": "9.9.9.9"}
        def close(self):
            pass
    class FakeDiscovery:
        def discover(self, hostname):
            return ["1.1.1.1"]
        def close(self):
            pass
    class FakeRunner:
        def __init__(self, *args):
            pass
        def benchmark(self, hostname, ips, **kwargs):
            assert ips == ["1.1.1.1", "9.9.9.9"]
            return [result("1.1.1.1", 100), result("9.9.9.9", 200)]
    monkeypatch.setattr(optimizer_module, "configured_adguard_client", lambda: FakeAdGuard())
    monkeypatch.setattr(optimizer_module, "PublicDnsDiscovery", FakeDiscovery)
    monkeypatch.setattr(optimizer_module, "HttpsBenchmarkRunner", FakeRunner)
    db_url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    db = Database(db_url)
    Base.metadata.create_all(db.engine)
    monkeypatch.setattr(main_module, "Database", lambda: Database(db_url))
    assert main_module.benchmark_command("example.com") == 0
    output = json.loads(capsys.readouterr().out)
    assert output["current_ip"] == "9.9.9.9"
    assert output["current_rewrite_included"] is True
    assert output["decision"]["action"] == "HOLD"
    with Database(db_url).session() as session:
        from app.db.models import BenchmarkRunRecord
        from sqlalchemy import select
        saved_run = session.scalar(select(BenchmarkRunRecord))
        assert saved_run and saved_run.decision_action == "HOLD"
        assert get_pending_state(session, saved_run.target_id) == PendingCandidateState(candidate_ip="1.1.1.1", consecutive_wins=1)
    # A fresh session sees the persisted win streak.
    with Database(db_url).session() as session:
        assert get_pending_state(session, 1).consecutive_wins == 1
    assert main_module.benchmark_command("example.com") == 0
    second = json.loads(capsys.readouterr().out)
    assert second["decision"]["action"] == "UPDATE"


def test_adguard_check_is_read_only_and_hides_credentials(monkeypatch, capsys):
    calls = []
    class FakeAdGuard:
        def __init__(self, base_url=None):
            self.base_url = base_url
        def list_rewrites(self):
            calls.append("GET rewrite/list")
            return [{"domain": "example.com", "answer": "1.2.3.4"}]
        def close(self):
            pass
    monkeypatch.setenv("ADGUARD_URL", "http://user:private@adguard.local")
    monkeypatch.setattr(main_module, "AdGuardClient", FakeAdGuard)
    assert main_module.adguard_check_command() == 0
    output = capsys.readouterr().out
    assert '"connected": true' in output
    assert '"rewrite_count": 1' in output
    assert "private" not in output and "user" not in output
    assert calls == ["GET rewrite/list"]
