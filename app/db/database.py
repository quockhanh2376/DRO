"""SQLite engine and session setup."""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def database_url() -> str:
    """Resolve explicit URL/path, with a service default on Linux and local data DB elsewhere."""
    if url := os.getenv("DRO_DATABASE_URL"):
        return url
    if path := os.getenv("DRO_DB_PATH"):
        return f"sqlite:///{Path(path).expanduser().resolve().as_posix()}"
    default = Path("/var/lib/dro/dro.db") if sys.platform.startswith("linux") else Path("data/dro.db")
    return f"sqlite:///{default.resolve().as_posix()}"


def sqlite_file_path(url: str) -> Path | None:
    if not url.startswith("sqlite:"):
        return None
    database = make_url(url).database
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser().resolve()


def configure_sqlite_connection(connection, _record, path: Path | None = None) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
    if path and path.exists():
        path.chmod(0o600)


class Database:
    def __init__(self, url: str | None = None):
        self.url = url or database_url()
        self.sqlite_path = sqlite_file_path(self.url)
        if self.sqlite_path:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.engine: Engine = create_engine(self.url, future=True)
        if self.url.startswith("sqlite:"):
            event.listen(self.engine, "connect",
                         lambda connection, record: configure_sqlite_connection(connection, record, self.sqlite_path))
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield a transactional session, committing on success and rolling back on error."""
        with self.session_factory() as session:
            try:
                yield session
                if session.in_transaction():
                    session.commit()
            except Exception:
                session.rollback()
                raise

    def close(self) -> None:
        self.engine.dispose()
