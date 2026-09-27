"""Single-process target scheduler with persisted due times and run guards."""

from __future__ import annotations

import logging
import math
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from sqlalchemy import select

from app.core.rewrites import apply_automatic_decision
from app.db.database import Database
from app.db.models import ScheduleStateRecord, TargetRecord
from app.db.repositories import add_audit_event, get_setting, set_setting
from app.db.retention import (cleanup_retention, cleanup_rotated_logs,
                              configured_log_retention_days, benchmark_history_retention)
from app.time_utils import as_utc_aware, next_run_time

logger = logging.getLogger(__name__)


class RunAlreadyActive(RuntimeError):
    pass


class RunCoordinator:
    """Run at most two target cycles with FIFO scheduling in this process."""

    def __init__(self, max_running: int = 2):
        if max_running < 1:
            raise ValueError("max_running must be positive")
        self.max_running = max_running
        self._active: set[int] = set()
        self._queue: list[tuple[int, Callable[[], None]]] = []
        self._condition = threading.Condition()
        self._stopping = False

    def is_active(self, target_id: int) -> bool:
        with self._condition:
            return target_id in self._active

    def state(self, target_id: int) -> tuple[str | None, int | None]:
        with self._condition:
            if target_id in self._active:
                return "running", None
            position = next((index for index, (queued_id, _) in enumerate(self._queue, 1)
                             if queued_id == target_id), None)
            return ("queued", position) if position is not None else (None, None)

    def submit(self, target_id: int, task: Callable[[], None]) -> tuple[str, int | None]:
        """Submit a manual run; duplicate active/queued requests reuse its state."""
        with self._condition:
            if self._stopping:
                raise RuntimeError("Run coordinator is stopping")
            state, position = self._state_locked(target_id)
            if state:
                return state, position
            self._queue.append((target_id, task))
            self._dispatch_locked()
            state, position = self._state_locked(target_id)
            return state or "queued", position

    def _state_locked(self, target_id: int) -> tuple[str | None, int | None]:
        if target_id in self._active:
            return "running", None
        position = next((index for index, (queued_id, _) in enumerate(self._queue, 1)
                         if queued_id == target_id), None)
        return ("queued", position) if position is not None else (None, None)

    def _dispatch_locked(self) -> None:
        while not self._stopping and len(self._active) < self.max_running and self._queue:
            target_id, task = self._queue.pop(0)
            self._active.add(target_id)
            threading.Thread(target=self._run_queued, args=(target_id, task),
                             name=f"dro-benchmark-{target_id}", daemon=True).start()
        self._condition.notify_all()

    def _run_queued(self, target_id: int, task: Callable[[], None]) -> None:
        try:
            task()
        except Exception as exc:
            logger.error("Manual benchmark failed target_id=%d error=%s", target_id, type(exc).__name__)
        finally:
            with self._condition:
                self._active.discard(target_id)
                self._dispatch_locked()

    def stop(self) -> None:
        """Discard process-local queued work during shutdown; new instances start clean."""
        with self._condition:
            self._stopping = True
            self._queue.clear()
            self._condition.notify_all()

    @contextmanager
    def run(self, target_id: int) -> Iterator[None]:
        with self._condition:
            if self._state_locked(target_id)[0]:
                raise RunAlreadyActive(f"Target {target_id} is already running or queued")
            while len(self._active) >= self.max_running or self._queue:
                if self._stopping:
                    raise RunAlreadyActive("Run coordinator is stopping")
                self._condition.wait()
                if self._stopping:
                    raise RunAlreadyActive("Run coordinator is stopping")
                if self._state_locked(target_id)[0]:
                    raise RunAlreadyActive(f"Target {target_id} is already running or queued")
            self._active.add(target_id)
        try:
            yield
        finally:
            with self._condition:
                self._active.discard(target_id)
                self._dispatch_locked()


def scheduler_enabled(session) -> bool:
    return (get_setting(session, "scheduler_enabled") or "false").lower() == "true"


def default_interval(session) -> tuple[float, str]:
    value, unit = get_setting(session, "default_interval_value"), get_setting(session, "default_interval_unit")
    try:
        interval = float(value) if value is not None else 2.0
    except ValueError:
        interval = 2.0
    if not math.isfinite(interval) or interval <= 0:
        interval = 2.0
    return interval, unit if unit in {"minutes", "hours"} else "hours"


def default_interval_hours(session) -> float:
    value, unit = default_interval(session)
    return value / 60 if unit == "minutes" else value


def set_scheduler_enabled(session, enabled: bool) -> None:
    set_setting(session, "scheduler_enabled", str(enabled).lower())
    add_audit_event(session, "scheduler_enabled" if enabled else "scheduler_disabled",
                    details={"enabled": enabled})
    if enabled:
        for target in session.scalars(select(TargetRecord).where(TargetRecord.enabled.is_(True))):
            state = session.get(ScheduleStateRecord, target.id)
            if state is None:
                session.add(ScheduleStateRecord(target_id=target.id, next_run_at=datetime.now(timezone.utc)))
            elif state.next_run_at is None:
                state.next_run_at = datetime.now(timezone.utc)


def record_target_run(session, target: TargetRecord, completed_at: datetime | None = None) -> None:
    completed_at = as_utc_aware(completed_at or datetime.now(timezone.utc))
    state = session.get(ScheduleStateRecord, target.id)
    if state is None:
        state = ScheduleStateRecord(target_id=target.id)
        session.add(state)
    state.last_run_at = completed_at
    state.next_run_at = next_run_time(completed_at, target.interval_hours)


def run_due(database: Database, cycle: Callable, coordinator: RunCoordinator,
            now: datetime | None = None) -> int:
    """Run enabled due targets once; persist last and next run timestamps."""
    now = as_utc_aware(now or datetime.now(timezone.utc))
    with database.session() as session:
        if not scheduler_enabled(session):
            return 0
        targets = list(session.scalars(select(TargetRecord).where(TargetRecord.enabled.is_(True))).all())
        due_ids = []
        for target in targets:
            state = session.get(ScheduleStateRecord, target.id)
            if state is None:
                session.add(ScheduleStateRecord(target_id=target.id, next_run_at=now))
                due_ids.append(target.id)
            elif state.next_run_at is None or as_utc_aware(state.next_run_at) <= now:
                due_ids.append(target.id)

    completed = 0
    for target_id in due_ids:
        try:
            with coordinator.run(target_id):
                with database.session() as session:
                    target = session.get(TargetRecord, target_id)
                    if not target or not target.enabled:
                        continue
                    output = cycle(session, target)
                with database.session() as session:
                    target = session.get(TargetRecord, target_id)
                    if target:
                        apply_automatic_decision(session, target, output)
                        record_target_run(session, target)
                completed += 1
        except RunAlreadyActive:
            logger.info("Scheduled run skipped target_id=%d reason=already_running", target_id)
        except Exception as exc:
            logger.error("Scheduled run failed target_id=%d error=%s", target_id, type(exc).__name__)
            with database.session() as session:
                target = session.get(TargetRecord, target_id)
                if target:
                    record_target_run(session, target)
    return completed


class SchedulerWorker:
    def __init__(self, database: Database, cycle: Callable, coordinator: RunCoordinator,
                 poll_seconds: int = 15):
        self.database, self.cycle, self.coordinator = database, cycle, coordinator
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._thread or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="dro-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                cleanup_if_due(self.database)
                run_due(self.database, self.cycle, self.coordinator)
            except Exception as exc:
                logger.error("Scheduler poll failed error=%s", type(exc).__name__)
            self._stop.wait(self.poll_seconds)


def cleanup_if_due(database: Database, now: datetime | None = None) -> dict[str, int] | None:
    now = as_utc_aware(now or datetime.now(timezone.utc))
    with database.session() as session:
        previous = get_setting(session, "retention_last_cleanup")
        if previous:
            try:
                timestamp = datetime.fromisoformat(previous)
                cleanup_interval = min(timedelta(hours=24), benchmark_history_retention(session))
                if as_utc_aware(timestamp) > now - cleanup_interval:
                    return None
            except ValueError:
                pass
        counts = cleanup_retention(session, now)
        retention_days = configured_log_retention_days(session)
        log_file = os.getenv("DRO_LOG_FILE")
        if log_file:
            counts["logs_deleted"] = cleanup_rotated_logs(log_file, retention_days, now)
        set_setting(session, "retention_last_cleanup", now.isoformat())
    logger.info("Retention cleanup complete runs=%d results=%d samples=%d logs=%d",
                counts["runs_deleted"], counts["results_deleted"], counts["samples_deleted"],
                counts.get("logs_deleted", 0))
    return counts
