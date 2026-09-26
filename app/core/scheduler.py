"""Single-process target scheduler with persisted due times and run guards."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from sqlalchemy import select

from app.core.rewrites import apply_automatic_decision
from app.db.database import Database
from app.db.models import ScheduleStateRecord, TargetRecord
from app.db.repositories import add_audit_event, get_setting, set_setting
from app.db.retention import cleanup_retention

logger = logging.getLogger(__name__)


class RunAlreadyActive(RuntimeError):
    pass


class RunCoordinator:
    """Prevent overlapping benchmark cycles for a target in this process."""

    def __init__(self):
        self._active: set[int] = set()
        self._lock = threading.Lock()

    @contextmanager
    def run(self, target_id: int) -> Iterator[None]:
        with self._lock:
            if target_id in self._active:
                raise RunAlreadyActive(f"Target {target_id} is already running")
            self._active.add(target_id)
        try:
            yield
        finally:
            with self._lock:
                self._active.discard(target_id)


def scheduler_enabled(session) -> bool:
    return (get_setting(session, "scheduler_enabled") or "false").lower() == "true"


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
    completed_at = completed_at or datetime.now(timezone.utc)
    state = session.get(ScheduleStateRecord, target.id)
    if state is None:
        state = ScheduleStateRecord(target_id=target.id)
        session.add(state)
    state.last_run_at = completed_at
    state.next_run_at = completed_at + timedelta(hours=target.interval_hours)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def run_due(database: Database, cycle: Callable, coordinator: RunCoordinator,
            now: datetime | None = None) -> int:
    """Run enabled due targets once; persist last and next run timestamps."""
    now = now or datetime.now(timezone.utc)
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
            elif state.next_run_at is None or _utc(state.next_run_at) <= now:
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
    now = now or datetime.now(timezone.utc)
    with database.session() as session:
        previous = get_setting(session, "retention_last_cleanup")
        if previous:
            try:
                timestamp = datetime.fromisoformat(previous)
                if _utc(timestamp) > now - timedelta(hours=24):
                    return None
            except ValueError:
                pass
        counts = cleanup_retention(session, now)
        set_setting(session, "retention_last_cleanup", now.isoformat())
    logger.info("Retention cleanup complete samples=%d runs=%d",
                counts["samples_deleted"], counts["runs_deleted"])
    return counts
