from datetime import datetime, timedelta, timezone

from app.core.scheduler import record_target_run, run_due, set_scheduler_enabled, RunCoordinator
from app.db.database import Database
from app.db.models import Base, ScheduleStateRecord
from app.db.repositories import save_target
from app.models.target import Target
from app.time_utils import (VIETNAM_TZ, as_utc_aware, format_vietnam_time,
                            next_run_time, to_vietnam_time)


def test_utc_vietnam_conversion_and_naive_sqlite_values():
    utc = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)
    expected = datetime(2026, 9, 27, 10, 0, tzinfo=VIETNAM_TZ)
    assert to_vietnam_time(utc) == expected
    assert to_vietnam_time(datetime(2026, 9, 27, 3, 0)) == expected
    assert as_utc_aware(expected) == utc
    assert format_vietnam_time(datetime(2026, 9, 27, 8, 59, 20, tzinfo=timezone.utc)) == (
        "2026-09-27 15:59:20 ICT")


def test_next_run_uses_vietnam_operational_clock_and_returns_utc():
    completed = datetime(2026, 9, 27, 10, 15, tzinfo=VIETNAM_TZ)
    next_run = next_run_time(completed, 2)
    assert next_run == datetime(2026, 9, 27, 5, 15, tzinfo=timezone.utc)
    assert to_vietnam_time(next_run) == datetime(2026, 9, 27, 12, 15, tzinfo=VIETNAM_TZ)


def test_scheduler_accepts_vietnam_now_against_utc_database_schedule(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'vietnam-schedule.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    with db.session() as session:
        target = save_target(session, Target(hostname="schedule.example", interval_hours=3))
        target_id = target.id
        set_scheduler_enabled(session, True)
        state = session.get(ScheduleStateRecord, target_id)
        state.next_run_at = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)

    called = []
    def cycle(_session, _target):
        called.append(True)
        return {"decision": {"action": "KEEP"}}

    vietnam_now = datetime(2026, 9, 27, 10, 0, tzinfo=VIETNAM_TZ)
    assert run_due(db, cycle, RunCoordinator(), vietnam_now - timedelta(seconds=1)) == 0
    assert run_due(db, cycle, RunCoordinator(), vietnam_now) == 1
    assert called == [True]
    db.close()


def test_record_target_run_stores_normalized_utc_times(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'vietnam-record.db').as_posix()}")
    Base.metadata.create_all(db.engine)
    completed = datetime(2026, 9, 27, 10, 0, tzinfo=VIETNAM_TZ)
    with db.session() as session:
        target = save_target(session, Target(hostname="record.example", interval_hours=2))
        target_id = target.id
        record_target_run(session, target, completed)
    with db.session() as session:
        state = session.get(ScheduleStateRecord, target_id)
        assert state.last_run_at.tzinfo is None
        assert state.last_run_at == datetime(2026, 9, 27, 3, 0)
        assert as_utc_aware(state.last_run_at) == datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)
        assert as_utc_aware(state.next_run_at) == datetime(2026, 9, 27, 5, 0, tzinfo=timezone.utc)
    db.close()
