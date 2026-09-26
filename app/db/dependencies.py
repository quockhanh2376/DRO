"""Request-scoped database session dependency."""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy.orm import Session

from app.db.database import Database


def get_session(request: Request) -> Generator[Session, None, None]:
    database = request.app.state.database or Database()
    owned = request.app.state.database is None
    try:
        with database.session_factory() as session:
            yield session
            if session.in_transaction():
                session.commit()
    except Exception:
        if "session" in locals():
            session.rollback()
        raise
    finally:
        if owned:
            database.close()
