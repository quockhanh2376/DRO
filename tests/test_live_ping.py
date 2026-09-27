from __future__ import annotations

import subprocess
import time

import pytest

from app.core.live_ping import PingLimitReached, PingSessionManager


class FakeProcess:
    stdout = None

    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_ping_process_is_validated_bounded_replaced_and_stopped(monkeypatch):
    created = []

    def popen(args, **kwargs):
        process = FakeProcess()
        created.append((args, kwargs, process))
        return process

    monkeypatch.setattr("app.core.live_ping.subprocess.Popen", popen)
    manager = PingSessionManager(max_sessions=1)
    first = manager.start(7, "192.0.2.11")
    second = manager.start(7, "192.0.2.12")
    assert created[0][0] == ["ping", "-n", "192.0.2.11"]
    assert created[0][1]["stdout"] == subprocess.PIPE
    assert "shell" not in created[0][1]
    assert created[0][2].terminated and manager.get(7) is second
    with pytest.raises(PingLimitReached):
        manager.start(8, "192.0.2.13")
    manager.stop_all()
    assert created[1][2].terminated and manager.get(7) is None


def test_ping_process_rejects_non_ipv4_before_popen(monkeypatch):
    monkeypatch.setattr("app.core.live_ping.subprocess.Popen",
                        lambda *_args, **_kwargs: pytest.fail("unvalidated address reached ping"))
    manager = PingSessionManager()
    with pytest.raises(ValueError):
        manager.start(1, "192.0.2.1; touch /tmp/no")
    with pytest.raises(ValueError, match="IPv4"):
        manager.start(1, "2001:db8::1")


def test_manual_ping_stop_terminates_process_and_cancels_timeout(monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr("app.core.live_ping.subprocess.Popen", lambda *_args, **_kwargs: process)
    manager = PingSessionManager(timeout_seconds=1)
    session = manager.start(3, "192.0.2.30")
    stopped = manager.stop(3)
    assert stopped is session and session.stop_reason == "manual"
    assert process.terminated and manager.get(3) is session and not manager.is_active(3)
    time.sleep(.03)
    assert session.stop_reason == "manual"


def test_ping_automatically_stops_after_timeout_and_cleans_up_process(monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr("app.core.live_ping.subprocess.Popen", lambda *_args, **_kwargs: process)
    manager = PingSessionManager(timeout_seconds=.03)
    session = manager.start(4, "192.0.2.40")
    deadline = time.monotonic() + 1
    while manager.is_active(4) and time.monotonic() < deadline:
        time.sleep(.005)
    assert not manager.is_active(4)
    assert manager.get(4) is session and session.stop_reason == "timeout"
    assert session.stopped_at is not None and process.terminated and process.poll() is not None
