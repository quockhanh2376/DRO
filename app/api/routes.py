"""FastAPI routes backed by the existing core and SQLite repositories."""

from __future__ import annotations

from collections.abc import Generator

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.optimizer import read_adguard_rewrite, run_benchmark_cycle
from app.db.database import Database
from app.db.models import (
    BenchmarkResultRecord, TargetRecord,
)
from app.db.repositories import (
    count_benchmark_runs, count_targets, delete_target as repo_delete_target, get_benchmark_run,
    get_current_rewrite_ip, get_target, get_target_by_id, list_benchmark_runs,
    list_rewrite_history as repo_list_rewrite_history,
    list_targets as repo_list_targets, save_target,
)
from app.models.target import Target
from app.api.schemas import TargetCreate, TargetPatch, TargetRead

router = APIRouter()


def get_session(request: Request) -> Generator[Session, None, None]:
    database = request.app.state.database or Database()
    owned = request.app.state.database is None
    try:
        with database.session() as session:
            yield session
    finally:
        if owned:
            database.close()


def target_read(record: TargetRecord) -> TargetRead:
    values = {key: getattr(record, key) for key in Target.model_fields}
    return TargetRead(id=record.id, **values)


def not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Target not found")


def get_target_or_404(session: Session, target_id: int) -> TargetRecord:
    record = get_target_by_id(session, target_id)
    if record is None:
        raise not_found()
    return record


@router.get("/health")
def health(session: Session = Depends(get_session)) -> dict:
    session.execute(select(1))
    return {"status": "ok"}


@router.get("/api/v1/system/status")
def system_status(session: Session = Depends(get_session)) -> dict:
    return {
        "status": "ok",
        "database": "ok",
        "target_count": count_targets(session),
        "benchmark_run_count": count_benchmark_runs(session),
    }


@router.get("/api/v1/targets", response_model=list[TargetRead])
def list_targets(session: Session = Depends(get_session)) -> list[TargetRead]:
    records = repo_list_targets(session)
    return [target_read(record) for record in records]


@router.post("/api/v1/targets", response_model=TargetRead, status_code=201)
def create_target(body: TargetCreate, session: Session = Depends(get_session)) -> TargetRead:
    if get_target(session, body.hostname):
        raise HTTPException(status_code=409, detail="Target already exists")
    try:
        return target_read(save_target(session, body))
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Target already exists") from exc


@router.get("/api/v1/targets/{target_id}", response_model=TargetRead)
def get_target_route(target_id: int, session: Session = Depends(get_session)) -> TargetRead:
    return target_read(get_target_or_404(session, target_id))


@router.patch("/api/v1/targets/{target_id}", response_model=TargetRead)
def patch_target(target_id: int, body: TargetPatch, session: Session = Depends(get_session)) -> TargetRead:
    record = get_target_or_404(session, target_id)
    current = Target.model_validate({key: getattr(record, key) for key in Target.model_fields})
    try:
        updated = Target.model_validate({**current.model_dump(), **body.model_dump(exclude_unset=True)})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid target configuration") from exc
    try:
        return target_read(save_target(session, updated, record=record))
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Target hostname already exists") from exc


@router.delete("/api/v1/targets/{target_id}", status_code=204)
def delete_target(target_id: int, session: Session = Depends(get_session)) -> Response:
    repo_delete_target(session, get_target_or_404(session, target_id))
    return Response(status_code=204)


@router.post("/api/v1/targets/{target_id}/run")
def run_target(target_id: int, request: Request, session: Session = Depends(get_session)) -> dict:
    record = get_target_or_404(session, target_id)
    try:
        return request.app.state.benchmark_cycle(session, record)
    except Exception as exc:
        # Keep internal errors, request details, and any credential-bearing transport text out of responses.
        if isinstance(exc, SQLAlchemyError):
            raise HTTPException(status_code=503, detail="Benchmark persistence unavailable") from exc
        raise HTTPException(status_code=502, detail="Benchmark could not be completed") from exc


@router.get("/api/v1/targets/{target_id}/runs")
def list_target_runs(target_id: int, session: Session = Depends(get_session)) -> list[dict]:
    get_target_or_404(session, target_id)
    runs = list_benchmark_runs(session, target_id)
    return [{"id": run.id, "started_at": run.started_at, "completed_at": run.completed_at,
             "summary": run.summary, "decision_action": run.decision_action,
             "decision_reason": run.decision_reason} for run in runs]


@router.get("/api/v1/runs/{run_id}")
def get_run(run_id: int, session: Session = Depends(get_session)) -> dict:
    run = get_benchmark_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Benchmark run not found")
    return {"id": run.id, "target_id": run.target_id, "started_at": run.started_at,
            "completed_at": run.completed_at, "summary": run.summary,
            "decision_action": run.decision_action, "decision_reason": run.decision_reason,
            "results": [{"ip": result.ip, "valid_runs": result.valid_runs,
                         "requested_runs": result.requested_runs, "healthy": result.healthy,
                         "average_ms": result.average_ms, "median_ms": result.median_ms,
                         "min_ms": result.min_ms, "max_ms": result.max_ms,
                         "jitter_ms": result.jitter_ms,
                         "samples": [{"run_number": sample.run_number, "http_status": sample.http_status,
                                      "connect_ms": sample.connect_ms, "tls_ms": sample.tls_ms,
                                      "total_ms": sample.total_ms, "error": sample.error}
                                     for sample in result.samples]}
                        for result in run.results]}


@router.get("/api/v1/targets/{target_id}/rewrite-history")
def rewrite_history(target_id: int, session: Session = Depends(get_session)) -> list[dict]:
    get_target_or_404(session, target_id)
    records = repo_list_rewrite_history(session, target_id)
    return [{"id": row.id, "old_ip": row.old_ip, "new_ip": row.new_ip,
             "reason": row.reason, "benchmark_run_id": row.benchmark_run_id,
             "created_at": row.created_at} for row in records]


@router.get("/api/v1/targets/{target_id}/rewrite")
def current_rewrite(target_id: int, session: Session = Depends(get_session)) -> dict:
    target = get_target_or_404(session, target_id)
    ip, succeeded = read_adguard_rewrite(target.hostname)
    source = "adguard"
    if not succeeded:
        ip = get_current_rewrite_ip(session, target_id)
        source = "database" if ip else "unavailable"
    return {"target_id": target_id, "hostname": target.hostname, "ip": ip, "source": source}
