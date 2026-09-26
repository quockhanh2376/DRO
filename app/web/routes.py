"""Server-rendered pages backed by the existing read-only services and repositories."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes import get_session, get_target_or_404
from app.api.schemas import TargetPatch
from app.db.models import BenchmarkRunRecord, OptimizerStateRecord, RewriteHistoryRecord
from app.db.repositories import (
    add_audit_event, get_benchmark_run, list_benchmark_runs, list_rewrite_history,
    list_targets, save_target,
)
from app.models.target import Target

logger = logging.getLogger(__name__)
router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _base_context(request: Request, **values):
    return {"request": request, **values}


def _dashboard_rows(session: Session) -> list[dict]:
    rows = []
    now = datetime.now(timezone.utc)
    for target in list_targets(session):
        run = session.scalar(select(BenchmarkRunRecord).where(
            BenchmarkRunRecord.target_id == target.id
        ).order_by(BenchmarkRunRecord.completed_at.desc()).limit(1))
        results = run.results if run else []
        best = min((result for result in results if result.healthy and result.average_ms is not None),
                   key=lambda result: (result.average_ms,
                                       result.median_ms if result.median_ms is not None else float("inf"),
                                       result.jitter_ms if result.jitter_ms is not None else float("inf")),
                   default=None)
        state = session.get(OptimizerStateRecord, target.id)
        current = next((item for item in results if state and item.ip == state.current_rewrite_ip), None)
        improvement = None
        if current and best and current.average_ms and best.average_ms is not None:
            improvement = max(0.0, (current.average_ms - best.average_ms) / current.average_ms * 100)
        completed = run.completed_at if run else None
        next_run = (completed.replace(tzinfo=timezone.utc) + timedelta(hours=target.interval_hours)
                    if completed and completed.tzinfo is None else
                    completed + timedelta(hours=target.interval_hours) if completed else None)
        rows.append({"target": target, "state": state, "run": run, "best": best,
                     "current": current, "improvement": improvement,
                     "wins": state.consecutive_wins if state else 0,
                     "next_run": next_run, "status": "Disabled" if not target.enabled else
                     "No benchmark" if not run else "Healthy" if best else "Critical"})
    return rows


@router.get("/", response_class=HTMLResponse, name="dashboard")
def dashboard(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "dashboard.html",
                                      _base_context(request, rows=_dashboard_rows(session)))


@router.get("/targets", response_class=HTMLResponse, name="targets_page")
def targets_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "targets.html",
                                      _base_context(request, targets=list_targets(session), target=None,
                                                    errors=None))


def _target_form_values(
    hostname: str, enabled: bool, mode: str, interval_hours: int, runs_per_ip: int,
    switch_threshold_ms: float, switch_threshold_percent: float, manual_lock_ip: str | None,
) -> dict:
    return {"hostname": hostname, "enabled": enabled, "mode": mode,
            "interval_hours": interval_hours, "runs_per_ip": runs_per_ip,
            "switch_threshold_ms": switch_threshold_ms,
            "switch_threshold_percent": switch_threshold_percent,
            "manual_lock_ip": manual_lock_ip or None}


@router.post("/targets", response_class=HTMLResponse, name="create_target_page")
def create_target_page(
    request: Request, hostname: str = Form(...), enabled: bool = Form(False),
    mode: str = Form("auto"), interval_hours: int = Form(2), runs_per_ip: int = Form(10),
    switch_threshold_ms: float = Form(50), switch_threshold_percent: float = Form(5),
    manual_lock_ip: str = Form(""), session: Session = Depends(get_session),
):
    try:
        target = Target.model_validate(_target_form_values(
            hostname, enabled, mode, interval_hours, runs_per_ip,
            switch_threshold_ms, switch_threshold_percent, manual_lock_ip))
        if any(item.hostname == target.hostname for item in list_targets(session)):
            raise ValueError("A target with this hostname already exists")
        record = save_target(session, target)
        add_audit_event(session, "target_created", record.id, {"hostname": record.hostname})
    except (ValueError, TypeError) as exc:
        logger.info("Target creation rejected")
        return templates.TemplateResponse(request, "targets.html", _base_context(
            request, targets=list_targets(session), target=None, errors=[str(exc)],
            form_values=_target_form_values(hostname, enabled, mode, interval_hours, runs_per_ip,
                                            switch_threshold_ms, switch_threshold_percent, manual_lock_ip)),
            status_code=422)
    logger.info("Target created hostname=%s", record.hostname)
    return RedirectResponse("/targets", status_code=303)


@router.get("/targets/{target_id}/edit", response_class=HTMLResponse, name="edit_target_page")
def edit_target_page(target_id: int, request: Request, session: Session = Depends(get_session)):
    target = get_target_or_404(session, target_id)
    return templates.TemplateResponse(request, "targets.html",
                                      _base_context(request, targets=list_targets(session), target=target,
                                                    errors=None))


@router.post("/targets/{target_id}/edit", response_class=HTMLResponse, name="update_target_page")
def update_target_page(
    target_id: int, request: Request, hostname: str = Form(...), enabled: bool = Form(False),
    mode: str = Form("auto"), interval_hours: int = Form(2), runs_per_ip: int = Form(10),
    switch_threshold_ms: float = Form(50), switch_threshold_percent: float = Form(5),
    manual_lock_ip: str = Form(""), session: Session = Depends(get_session),
):
    record = get_target_or_404(session, target_id)
    try:
        values = _target_form_values(hostname, enabled, mode, interval_hours, runs_per_ip,
                                     switch_threshold_ms, switch_threshold_percent, manual_lock_ip)
        patch = TargetPatch.model_validate(values)
        current = Target.model_validate(record, from_attributes=True)
        updated = Target.model_validate({**current.model_dump(), **patch.model_dump(exclude_unset=True)})
        if any(item.id != target_id and item.hostname == updated.hostname for item in list_targets(session)):
            raise ValueError("A target with this hostname already exists")
        save_target(session, updated, record)
        add_audit_event(session, "target_updated", record.id, {"hostname": record.hostname})
    except (ValueError, TypeError) as exc:
        logger.info("Target update rejected target_id=%d", target_id)
        return templates.TemplateResponse(request, "targets.html", _base_context(
            request, targets=list_targets(session), target=record, errors=[str(exc)], form_values=values),
            status_code=422)
    logger.info("Target updated target_id=%d hostname=%s", target_id, record.hostname)
    return RedirectResponse("/targets", status_code=303)


@router.post("/targets/{target_id}/delete", name="delete_target_page")
def delete_target_page(target_id: int, session: Session = Depends(get_session)):
    record = get_target_or_404(session, target_id)
    hostname = record.hostname
    session.delete(record)
    add_audit_event(session, "target_deleted", None, {"hostname": hostname})
    logger.info("Target deleted target_id=%d hostname=%s", target_id, hostname)
    return RedirectResponse("/targets", status_code=303)


@router.get("/targets/{target_id}", response_class=HTMLResponse, name="target_detail")
def target_detail(target_id: int, request: Request, session: Session = Depends(get_session)):
    target = get_target_or_404(session, target_id)
    runs = list_benchmark_runs(session, target_id)
    latest = get_benchmark_run(session, runs[0].id) if runs else None
    state = session.get(OptimizerStateRecord, target_id)
    current = next((item for item in latest.results if state and item.ip == state.current_rewrite_ip), None) if latest else None
    return templates.TemplateResponse(request, "target_detail.html", _base_context(
        request, target=target, latest=latest, state=state, current=current,
        runs=runs, rewrite_history=list_rewrite_history(session, target_id)))


@router.post("/targets/{target_id}/run", name="run_target_now")
def run_target_now(target_id: int, request: Request, session: Session = Depends(get_session)):
    target = get_target_or_404(session, target_id)
    try:
        request.app.state.benchmark_cycle(session, target)
    except Exception as exc:
        logger.warning("Manual benchmark failed target_id=%d error=%s", target_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Benchmark could not be completed") from exc
    logger.info("Manual benchmark completed target_id=%d hostname=%s", target_id, target.hostname)
    return RedirectResponse(f"/targets/{target_id}", status_code=303)


@router.get("/history", response_class=HTMLResponse, name="history_page")
def history_page(request: Request, session: Session = Depends(get_session)):
    targets = {target.id: target for target in list_targets(session)}
    runs = list(session.scalars(select(BenchmarkRunRecord).order_by(
        BenchmarkRunRecord.completed_at.desc()).limit(200)).all())
    rewrites = list(session.scalars(select(RewriteHistoryRecord).order_by(
        RewriteHistoryRecord.created_at.desc()).limit(200)).all())
    return templates.TemplateResponse(request, "history.html", _base_context(
        request, targets=targets, runs=runs, rewrites=rewrites))


@router.get("/settings", response_class=HTMLResponse, name="settings_page")
def settings_page(request: Request):
    import os
    from app.security import safe_endpoint

    return templates.TemplateResponse(request, "settings.html", _base_context(
        request, adguard_url=safe_endpoint(os.getenv("ADGUARD_URL") or "Not configured"),
        scheduler_enabled=False, default_interval=2, threshold_ms=50,
        threshold_percent=5, log_retention_days=7))
