from __future__ import annotations

from types import SimpleNamespace

from app.core import system_diagnostics as diagnostics


class FakeAdGuard:
    def __init__(self, rewrites=None, error=None):
        self.rewrites = rewrites or []
        self.error = error
        self.closed = False

    def list_rewrites(self):
        if self.error:
            raise self.error
        return self.rewrites

    def close(self):
        self.closed = True


def healthy_environment(monkeypatch):
    monkeypatch.setattr(diagnostics, "_resolve", lambda *_args: ["203.0.113.10"])
    monkeypatch.setattr(diagnostics, "_connect", lambda *_args: True)
    monkeypatch.setattr(diagnostics, "_connect_https", lambda *_args: True)


def run(fake_adguard, targets=None, *, scheduler=True, worker=True, queue=None, https=True):
    return diagnostics.run_diagnostics(
        targets=targets or [], adguard_client_factory=lambda: fake_adguard,
        scheduler_enabled=scheduler, scheduler_running=worker,
        queue_states=queue or [], https_reached=https)


def test_read_only_checks_return_all_healthy(monkeypatch):
    healthy_environment(monkeypatch)
    target = SimpleNamespace(hostname="healthy.example", enabled=True, port=443)
    result = run(FakeAdGuard([{"domain": "healthy.example", "answer": "203.0.113.10"}]), [target])

    assert result["overall"] == "healthy"
    assert {row["name"] for row in result["checks"]} == {
        "DRO Backend", "AdGuard API", "AdGuard Rewrite Read", "Nginx / HTTPS",
        "DNS Resolution", "Internet Connectivity", "Scheduler", "Queue Coordinator", "Enabled Targets",
    }
    assert result["targets"][0]["status"] == "healthy"


def test_disabled_or_unavailable_scheduler_and_http_proxy_are_warning(monkeypatch):
    healthy_environment(monkeypatch)
    result = run(FakeAdGuard(), scheduler=False, worker=False, https=False)
    assert result["overall"] == "warning"
    by_name = {row["name"]: row for row in result["checks"]}
    assert by_name["Scheduler"]["status"] == "warning"
    assert by_name["Nginx / HTTPS"]["status"] == "warning"


def test_failed_network_checks_return_failed_overall(monkeypatch):
    monkeypatch.setattr(diagnostics, "_resolve", lambda *_args: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(diagnostics, "_connect", lambda *_args: False)
    monkeypatch.setattr(diagnostics, "_connect_https", lambda *_args: False)
    target = SimpleNamespace(hostname="broken.example", enabled=True, port=443)
    result = run(FakeAdGuard(), [target])
    assert result["overall"] == "failed"
    assert next(row for row in result["checks"] if row["name"] == "DNS Resolution")["status"] == "failed"
    assert next(row for row in result["checks"] if row["name"] == "Enabled Targets")["status"] == "failed"


def test_adguard_failure_does_not_abort_other_diagnostics(monkeypatch):
    healthy_environment(monkeypatch)
    result = run(FakeAdGuard(error=RuntimeError("secret-free simulated outage")))
    by_name = {row["name"]: row for row in result["checks"]}
    assert result["overall"] == "failed"
    assert by_name["AdGuard API"]["status"] == "failed"
    assert by_name["AdGuard Rewrite Read"]["status"] == "failed"
    assert by_name["DNS Resolution"]["status"] == "healthy"
    assert by_name["Internet Connectivity"]["status"] == "healthy"
    assert by_name["Scheduler"]["status"] == "healthy"


def test_failed_enabled_target_appears_in_compact_target_summary(monkeypatch):
    monkeypatch.setattr(diagnostics, "_resolve", lambda host, *_args: (
        (_ for _ in ()).throw(OSError()) if host == "failed.example" else ["203.0.113.10"]))
    monkeypatch.setattr(diagnostics, "_connect", lambda *_args: True)
    monkeypatch.setattr(diagnostics, "_connect_https", lambda *_args: True)
    targets = [SimpleNamespace(hostname="failed.example", enabled=True, port=443),
               SimpleNamespace(hostname="healthy.example", enabled=True, port=443)]
    result = run(FakeAdGuard(), targets)
    assert result["overall"] == "warning"
    assert result["targets"][0]["hostname"] == "failed.example"
    assert result["targets"][0]["status"] == "failed"
    enabled_check = next(row for row in result["checks"] if row["name"] == "Enabled Targets")
    assert enabled_check == {"name": "Enabled Targets", "status": "warning", "detail": "1/2 reachable"}
