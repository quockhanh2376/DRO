"""SQLite backup and validated restore helpers."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from app.security import SECRET_ENV_FILE

REQUIRED_TABLES = {"targets", "benchmark_runs", "benchmark_results", "benchmark_samples",
                   "optimizer_state", "rewrite_history", "settings", "audit_log",
                   "admin_credentials", "schedule_state"}


def validate_database(path: Path) -> None:
    if not path.is_file():
        raise ValueError("SQLite database file does not exist")
    try:
        with closing(sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as exc:
        raise ValueError("Invalid SQLite database") from exc
    if result != ("ok",) or not REQUIRED_TABLES.issubset(tables):
        raise ValueError("SQLite integrity or DRO schema validation failed")


def backup_database(source: Path, destination: Path, overwrite: bool = False) -> Path:
    source, destination = source.resolve(), destination.resolve()
    validate_database(source)
    if source == destination:
        raise ValueError("Backup destination must differ from the live database")
    if destination == SECRET_ENV_FILE.resolve():
        raise ValueError("Backup destination cannot be /etc/dro/dro.env")
    if destination.exists() and not overwrite:
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".dro-backup-", suffix=".db", dir=destination.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source)) as source_db, closing(sqlite3.connect(temp_name)) as backup_db:
            source_db.backup(backup_db)
        validate_database(Path(temp_name))
        os.replace(temp_name, destination)
        os.chmod(destination, 0o600)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return destination


def restore_database(source: Path, destination: Path, confirmed: bool = False) -> Path:
    source, destination = source.resolve(), destination.resolve()
    validate_database(source)
    if source == destination:
        raise ValueError("Restore source must differ from the live database")
    if source == SECRET_ENV_FILE.resolve() or destination == SECRET_ENV_FILE.resolve():
        raise ValueError("Restore cannot use or replace /etc/dro/dro.env")
    if destination.exists() and not confirmed:
        raise PermissionError("Restore requires explicit confirmation")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".dro-restore-", suffix=".db", dir=destination.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source)) as source_db, closing(sqlite3.connect(temp_name)) as staged_db:
            source_db.backup(staged_db)
        validate_database(Path(temp_name))
        Path(f"{destination}-wal").unlink(missing_ok=True)
        Path(f"{destination}-shm").unlink(missing_ok=True)
        os.replace(temp_name, destination)
        os.chmod(destination, 0o600)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return destination
