"""ASGI entry point and app factory for the DRO API."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.core.optimizer import run_benchmark_cycle
from app.db.database import Database
from app.api.routes import router


def create_app(database: Database | None = None, benchmark_cycle=run_benchmark_cycle) -> FastAPI:
    application = FastAPI(title="DRO API", version="0.1.0")
    application.state.database = database
    application.state.benchmark_cycle = benchmark_cycle
    application.include_router(router)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "Request validation failed"})

    @application.exception_handler(SQLAlchemyError)
    async def database_error(_request: Request, _exc: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"})

    return application


app = create_app()
