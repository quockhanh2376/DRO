"""ASGI entry point and app factory for the DRO API."""

from __future__ import annotations

import os
import secrets
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.core.optimizer import run_benchmark_cycle
from app.db.database import Database
from app.core.scheduler import RunCoordinator, SchedulerWorker
from app.api.routes import router


def create_app(database: Database | None = None, benchmark_cycle=run_benchmark_cycle) -> FastAPI:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    scheduler_database = database or Database()
    owns_scheduler_database = database is None
    coordinator = RunCoordinator()

    @asynccontextmanager
    async def lifespan(_application):
        worker = SchedulerWorker(scheduler_database, benchmark_cycle, coordinator)
        worker.start()
        try:
            yield
        finally:
            worker.stop()
            if owns_scheduler_database:
                scheduler_database.close()

    application = FastAPI(title="DRO API", version="0.1.0", lifespan=lifespan)
    application.add_middleware(
        SessionMiddleware, secret_key=os.getenv("DRO_SESSION_SECRET") or secrets.token_urlsafe(48),
        session_cookie="dro_session", max_age=12 * 60 * 60, same_site="lax",
        https_only=os.getenv("DRO_HTTPS_ENABLED", "false").lower() in {"1", "true", "yes"},
    )
    application.state.database = database
    application.state.benchmark_cycle = benchmark_cycle
    application.state.run_coordinator = coordinator
    application.mount("/static", StaticFiles(directory=Path(__file__).resolve().parents[1] / "web" / "static"),
                      name="static")
    application.include_router(router)

    from app.auth import auth_router
    from app.web.routes import router as web_router
    application.include_router(auth_router)
    application.include_router(web_router)

    @application.exception_handler(StarletteHTTPException)
    async def authentication_redirect(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 401 and not request.url.path.startswith("/api/"):
            return RedirectResponse("/login", status_code=303)
        return await http_exception_handler(request, exc)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "Request validation failed"})

    @application.exception_handler(SQLAlchemyError)
    async def database_error(_request: Request, _exc: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"})

    return application


app = create_app()
