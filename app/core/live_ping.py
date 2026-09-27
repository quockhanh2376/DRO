"""Bounded, per-target continuous ping processes for the Targets UI."""

from __future__ import annotations

import ipaddress
import logging
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
MAX_PING_SESSIONS = 8
PING_TIMEOUT_SECONDS = 60


class PingLimitReached(RuntimeError):
    pass


@dataclass
class PingSession:
    target_id: int
    ip: str
    process: subprocess.Popen
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=100))
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop_reason: str | None = None
    stopped_at: datetime | None = None
    timer: threading.Timer | None = None


class PingSessionManager:
    def __init__(self, max_sessions: int = MAX_PING_SESSIONS,
                 timeout_seconds: float = PING_TIMEOUT_SECONDS):
        self.max_sessions = max_sessions
        self.timeout_seconds = timeout_seconds
        self._sessions: dict[int, PingSession] = {}
        self._finished: dict[int, PingSession] = {}
        self._lock = threading.Lock()

    def start(self, target_id: int, ip: str) -> PingSession:
        address = ipaddress.ip_address(ip)
        if not isinstance(address, ipaddress.IPv4Address):
            raise ValueError("Ping requires an IPv4 address")
        with self._lock:
            old = self._sessions.pop(target_id, None)
            if old:
                if old.timer:
                    old.timer.cancel()
                self._terminate(old.process)
            self._finished.pop(target_id, None)
            if len(self._sessions) >= self.max_sessions:
                raise PingLimitReached("Too many active ping sessions")
            process = subprocess.Popen(
                ["ping", "-n", str(address)], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                start_new_session=True,
            )
            session = PingSession(target_id, str(address), process)
            self._sessions[target_id] = session
            session.timer = threading.Timer(self.timeout_seconds, self._auto_stop, (target_id, session))
            session.timer.daemon = True
            session.timer.start()
            threading.Thread(target=self._read_output, args=(session,), daemon=True).start()
            return session

    def get(self, target_id: int) -> PingSession | None:
        with self._lock:
            return self._sessions.get(target_id) or self._finished.get(target_id)

    def is_active(self, target_id: int) -> bool:
        with self._lock:
            session = self._sessions.get(target_id)
            return bool(session and session.process.poll() is None)

    def stop(self, target_id: int) -> PingSession | None:
        session = self.get(target_id)
        if session:
            self._finish(target_id, session, "manual", terminate=True)
        return session

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._finished.clear()
        for session in sessions:
            if session.timer:
                session.timer.cancel()
            self._terminate(session.process)

    def _auto_stop(self, target_id: int, session: PingSession) -> None:
        self._finish(target_id, session, "timeout", terminate=True)

    def _finish(self, target_id: int, session: PingSession, reason: str, terminate: bool) -> None:
        with self._lock:
            if self._sessions.get(target_id) is not session:
                return
            self._sessions.pop(target_id, None)
            session.stop_reason = reason
            session.stopped_at = datetime.now(timezone.utc)
            if session.timer and threading.current_thread() is not session.timer:
                session.timer.cancel()
            self._finished[target_id] = session
            while len(self._finished) > self.max_sessions * 4:
                self._finished.pop(next(iter(self._finished)))
        if terminate:
            self._terminate(session.process)
        logger.info("Live ping ended target_id=%d ip=%s reason=%s", target_id, session.ip, reason)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def _read_output(self, session: PingSession) -> None:
        stream = session.process.stdout
        if stream is None:
            return
        try:
            for line in stream:
                with session.lock:
                    session.lines.append(line.rstrip())
        finally:
            self._finish(session.target_id, session, "process-ended", terminate=False)


ping_sessions = PingSessionManager()
