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

from app.api.routes import (LockRequest, RollbackRequest, get_target_or_404,
                            lock_target as api_lock_target, rollback_target as api_rollback_target,
                            unlock_target as api_unlock_target)
from app.api.schemas import TargetPatch
from app.auth import _csrf_token, protect_mutation, require_admin
from app.db.models import BenchmarkRunRecord, OptimizerStateRecord, RewriteHistoryRecord, ScheduleStateRecord
from app.db.dependencies import get_session
from app.db.repositories import (
    add_audit_event, get_benchmark_run, get_setting, list_benchmark_runs, list_rewrite_history,
    list_targets, save_target, set_setting,
)
from app.models.target import Target
from app.core.scheduler import (RunAlreadyActive, default_interval, default_interval_hours,
                                record_target_run, scheduler_enabled, set_scheduler_enabled)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _best_benchmark_result(run):
    if not run:
        return None
    return min((item for item in run.results if item.healthy and item.average_ms is not None),
               key=lambda item: (item.average_ms,
                                 item.median_ms if item.median_ms is not None else float("inf"),
                                 item.jitter_ms if item.jitter_ms is not None else float("inf")),
               default=None)


def _base_context(request: Request, **values):
    return {"request": request, "csrf_token": _csrf_token(request), **values}


def _dashboard_rows(session: Session) -> list[dict]:
    rows = []
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
        schedule = session.get(ScheduleStateRecord, target.id)
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
                     "last_run": schedule.last_run_at if schedule else completed,
                     "next_run": schedule.next_run_at if schedule else next_run,
                     "status": "Disabled" if not target.enabled else
                     "No benchmark" if not run else "Healthy" if best else "Critical"})
    return rows


@router.get("/", response_class=HTMLResponse, name="dashboard", dependencies=[Depends(require_admin)])
def dashboard(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "dashboard.html",
                                      _base_context(request, rows=_dashboard_rows(session)))


@router.get("/targets", response_class=HTMLResponse, name="targets_page", dependencies=[Depends(require_admin)])
def targets_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "targets.html",
                                      _base_context(request, targets=list_targets(session), target=None,
                                                    errors=None,
                                                    default_interval_hours=default_interval_hours(session)))


def _target_form_values(
    hostname: str, enabled: bool, mode: str, interval_hours: float, runs_per_ip: int,
    switch_threshold_ms: float, switch_threshold_percent: float, manual_lock_ip: str | None,
) -> dict:
    return {"hostname": hostname, "enabled": enabled, "mode": mode,
            "interval_hours": interval_hours, "runs_per_ip": runs_per_ip,
            "switch_threshold_ms": switch_threshold_ms,
            "switch_threshold_percent": switch_threshold_percent,
            "manual_lock_ip": manual_lock_ip or None}


@router.post("/targets", response_class=HTMLResponse, name="create_target_page",
             dependencies=[Depends(protect_mutation)])
def create_target_page(
    request: Request, hostname: str = Form(...), enabled: bool = Form(False),
    mode: str = Form("auto"), interval_hours: float | None = Form(None), runs_per_ip: int = Form(10),
    switch_threshold_ms: float = Form(50), switch_threshold_percent: float = Form(5),
    manual_lock_ip: str = Form(""), session: Session = Depends(get_session),
):
    interval_hours = default_interval_hours(session) if interval_hours is None else interval_hours
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
                                            switch_threshold_ms, switch_threshold_percent, manual_lock_ip),
            default_interval_hours=default_interval_hours(session)),
            status_code=422)
    logger.info("Target created hostname=%s", record.hostname)
    return RedirectResponse("/targets", status_code=303)


@router.get("/targets/{target_id}/edit", response_class=HTMLResponse, name="edit_target_page",
            dependencies=[Depends(require_admin)])
def edit_target_page(target_id: int, request: Request, session: Session = Depends(get_session)):
    target = get_target_or_404(session, target_id)
    return templates.TemplateResponse(request, "targets.html",
                                      _base_context(request, targets=list_targets(session), target=target,
                                                    errors=None,
                                                    default_interval_hours=default_interval_hours(session)))


@router.post("/targets/{target_id}/edit", response_class=HTMLResponse, name="update_target_page",
             dependencies=[Depends(protect_mutation)])
def update_target_page(
    target_id: int, request: Request, hostname: str = Form(...), enabled: bool = Form(False),
    mode: str = Form("auto"), interval_hours: float | None = Form(None), runs_per_ip: int = Form(10),
    switch_threshold_ms: float = Form(50), switch_threshold_percent: float = Form(5),
    manual_lock_ip: str = Form(""), session: Session = Depends(get_session),
):
    record = get_target_or_404(session, target_id)
    interval_hours = record.interval_hours if interval_hours is None else interval_hours
    old_lock = record.manual_lock_ip
    try:
        values = _target_form_values(hostname, enabled, mode, interval_hours, runs_per_ip,
                                     switch_threshold_ms, switch_threshold_percent, manual_lock_ip)
        patch = TargetPatch.model_validate(values)
        current = Target.model_validate(record, from_attributes=True)
        updated = Target.model_validate({**current.model_dump(), **patch.model_dump(exclude_unset=True)})
        if any(item.id != target_id and item.hostname == updated.hostname for item in list_targets(session)):
            raise ValueError("A target with this hostname already exists")
        save_target(session, updated, record)
        if old_lock != record.manual_lock_ip:
            add_audit_event(session, "ip_locked" if record.manual_lock_ip else "ip_unlocked",
                            record.id, {"old_ip": old_lock, "ip": record.manual_lock_ip})
        schedule = session.get(ScheduleStateRecord, record.id)
        if schedule and schedule.last_run_at:
            schedule.next_run_at = schedule.last_run_at + timedelta(hours=updated.interval_hours)
        add_audit_event(session, "target_updated", record.id, {"hostname": record.hostname})
    except (ValueError, TypeError) as exc:
        logger.info("Target update rejected target_id=%d", target_id)
        return templates.TemplateResponse(request, "targets.html", _base_context(
            request, targets=list_targets(session), target=record, errors=[str(exc)], form_values=values,
            default_interval_hours=default_interval_hours(session)),
            status_code=422)
    logger.info("Target updated target_id=%d hostname=%s", target_id, record.hostname)
    return RedirectResponse("/targets", status_code=303)


@router.post("/targets/{target_id}/delete", name="delete_target_page",
             dependencies=[Depends(protect_mutation)])
def delete_target_page(target_id: int, confirm: bool = Form(False),
                       session: Session = Depends(get_session)):
    if not confirm:
        raise HTTPException(status_code=400, detail="Explicit delete confirmation is required")
    record = get_target_or_404(session, target_id)
    hostname = record.hostname
    session.delete(record)
    add_audit_event(session, "target_deleted", None, {"hostname": hostname})
    logger.info("Target deleted target_id=%d hostname=%s", target_id, hostname)
    return RedirectResponse("/targets", status_code=303)


@router.get("/targets/{target_id}", response_class=HTMLResponse, name="target_detail",
            dependencies=[Depends(require_admin)])
def target_detail(target_id: int, request: Request, session: Session = Depends(get_session)):
    target = get_target_or_404(session, target_id)
    runs = list_benchmark_runs(session, target_id)
    latest = get_benchmark_run(session, runs[0].id) if runs else None
    state = session.get(OptimizerStateRecord, target_id)
    current = next((item for item in latest.results if state and item.ip == state.current_rewrite_ip), None) if latest else None
    best = _best_benchmark_result(latest)
    return templates.TemplateResponse(request, "target_detail.html", _base_context(
        request, target=target, latest=latest, state=state, current=current, best=best,
        runs=runs, rewrite_history=list_rewrite_history(session, target_id)))


@router.post("/targets/{target_id}/run", name="run_target_now",
             dependencies=[Depends(protect_mutation)])
def run_target_now(target_id: int, request: Request, session: Session = Depends(get_session)):
    target = get_target_or_404(session, target_id)
    try:
        with request.app.state.run_coordinator.run(target_id):
            output = request.app.state.benchmark_cycle(session, target)
            session.commit()
            record_target_run(session, target)
            session.commit()
    except RunAlreadyActive as exc:
        raise HTTPException(status_code=409, detail="A benchmark is already running for this target") from exc
    except Exception as exc:
        logger.warning("Manual benchmark failed target_id=%d error=%s", target_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Benchmark could not be completed") from exc
    logger.info("Manual benchmark completed target_id=%d hostname=%s", target_id, target.hostname)
    if request.headers.get("HX-Request", "").lower() == "true":
        run_id = output.get("benchmark_run_id") if isinstance(output, dict) else None
        latest = get_benchmark_run(session, run_id) if run_id else None
        if latest is None:
            runs = list_benchmark_runs(session, target_id)
            latest = get_benchmark_run(session, runs[0].id) if runs else None
        return templates.TemplateResponse(request, "benchmark_result.html", _base_context(
            request, target=target, latest=latest, best=_best_benchmark_result(latest)))
    return RedirectResponse(f"/targets/{target_id}", status_code=303)


@router.post("/targets/{target_id}/lock", name="lock_target_page",
             dependencies=[Depends(protect_mutation)])
def lock_target_page(target_id: int, ip: str = Form(""), session: Session = Depends(get_session)):
    api_lock_target(target_id, LockRequest(ip=ip or None), session)
    return RedirectResponse(f"/targets/{target_id}", status_code=303)


@router.post("/targets/{target_id}/unlock", name="unlock_target_page",
             dependencies=[Depends(protect_mutation)])
def unlock_target_page(target_id: int, session: Session = Depends(get_session)):
    api_unlock_target(target_id, session)
    return RedirectResponse(f"/targets/{target_id}", status_code=303)


@router.post("/targets/{target_id}/rollback", name="rollback_target_page",
             dependencies=[Depends(protect_mutation)])
def rollback_target_page(target_id: int, confirm: bool = Form(False),
                         session: Session = Depends(get_session)):
    api_rollback_target(target_id, RollbackRequest(confirm=confirm), session)
    return RedirectResponse(f"/targets/{target_id}", status_code=303)


@router.get("/history", response_class=HTMLResponse, name="history_page",
            dependencies=[Depends(require_admin)])
def history_page(request: Request, session: Session = Depends(get_session)):
    targets = {target.id: target for target in list_targets(session)}
    runs = list(session.scalars(select(BenchmarkRunRecord).order_by(
        BenchmarkRunRecord.completed_at.desc()).limit(200)).all())
    rewrites = list(session.scalars(select(RewriteHistoryRecord).order_by(
        RewriteHistoryRecord.created_at.desc()).limit(200)).all())
    return templates.TemplateResponse(request, "history.html", _base_context(
        request, targets=targets, runs=runs, rewrites=rewrites))


@router.get("/settings", response_class=HTMLResponse, name="settings_page",
            dependencies=[Depends(require_admin)])
def settings_page(request: Request, session: Session = Depends(get_session),
                  _auth=Depends(require_admin)):
    return _settings_response(request, session)


def _settings_response(request: Request, session: Session, errors=None,
                       interval_value: str | None = None, interval_unit: str | None = None,
                       retention_value: str | None = None, status_code: int = 200):
    import os
    from app.security import safe_endpoint

    value, unit = default_interval(session)
    try:
        log_retention_days = max(1, int(get_setting(session, "log_retention_days") or "7"))
    except ValueError:
        log_retention_days = 7
    return templates.TemplateResponse(request, "settings.html", _base_context(
        request, adguard_url=safe_endpoint(os.getenv("ADGUARD_URL") or "Not configured"),
        scheduler_enabled=scheduler_enabled(session),
        default_interval_value=interval_value if interval_value is not None else value,
        default_interval_unit=interval_unit if interval_unit is not None else unit,
        threshold_ms=50, threshold_percent=5,
        max_auto_changes=int(get_setting(session, "max_auto_changes_per_day") or "4"),
        log_retention_days=retention_value if retention_value is not None else log_retention_days,
        errors=errors), status_code=status_code)


@router.post("/settings/default-interval", name="default_interval_setting",
             dependencies=[Depends(protect_mutation)])
def default_interval_setting(request: Request, value: str = Form(...), unit: str = Form(...),
                             session: Session = Depends(get_session)):
    import math

    try:
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0 or unit not in {"minutes", "hours"}:
            raise ValueError
    except ValueError:
        return _settings_response(request, session, {"default_interval": "Enter a positive interval and choose minutes or hours."},
                                  value, unit, status_code=422)
    set_setting(session, "default_interval_value", str(parsed))
    set_setting(session, "default_interval_unit", unit)
    add_audit_event(session, "default_interval_updated", details={"value": parsed, "unit": unit})
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/log-retention", name="log_retention_setting",
             dependencies=[Depends(protect_mutation)])
def log_retention_setting(request: Request, days: str = Form(...),
                          session: Session = Depends(get_session)):
    try:
        value = int(days)
        if value < 1:
            raise ValueError
    except ValueError:
        return _settings_response(request, session, {"log_retention": "Log retention must be at least 1 day."},
                                  retention_value=days, status_code=422)
    set_setting(session, "log_retention_days", str(value))
    add_audit_event(session, "log_retention_updated", details={"days": value})
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/scheduler", name="scheduler_setting",
             dependencies=[Depends(protect_mutation)])
def scheduler_setting(enabled: bool = Form(False), session: Session = Depends(get_session)):
    set_scheduler_enabled(session, enabled)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/change-limit", name="change_limit_setting",
             dependencies=[Depends(protect_mutation)])
def change_limit_setting(max_auto_changes_per_day: int = Form(4),
                         session: Session = Depends(get_session)):
    if not 0 <= max_auto_changes_per_day <= 1000:
        raise HTTPException(status_code=422, detail="Change limit must be between 0 and 1000")
    set_setting(session, "max_auto_changes_per_day", str(max_auto_changes_per_day))
    return RedirectResponse("/settings", status_code=303)
