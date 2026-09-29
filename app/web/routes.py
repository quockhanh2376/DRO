"""Server-rendered pages backed by the existing read-only services and repositories."""

from __future__ import annotations

import logging
import ipaddress
from pathlib import Path
from datetime import datetime, timedelta, timezone

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
from app.db.models import (BenchmarkRunRecord, OptimizerStateRecord, RewriteHistoryRecord,
                           ScheduleStateRecord, TargetRecord)
from app.db.dependencies import get_session
from app.db.repositories import (
    add_audit_event, get_benchmark_run, get_setting, list_benchmark_runs, list_rewrite_history,
    list_targets, save_target, set_setting,
)
from app.db.retention import clear_benchmark_history, configured_benchmark_history_retention
from app.models.target import Target
from app.core.scheduler import (default_interval, default_interval_hours,
                                record_target_run, scheduler_enabled, set_scheduler_enabled)
from app.core.optimizer import read_adguard_rewrite
from app.core.rewrites import automatic_rewrite_enabled, set_rewrite, _healthy
from app.integrations.adguard import AdGuardError, configured_adguard_client
from app.db.repositories import add_rewrite_history
from app.core.live_ping import PingLimitReached, PingSession, ping_sessions
from app.time_utils import format_vietnam_time, next_run_time
from app.version import VERSION_DISPLAY

logger = logging.getLogger(__name__)
router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
templates.env.filters["vn_time"] = format_vietnam_time
ADD_BEST_MAX_AGE = timedelta(hours=1)


def _best_benchmark_result(run):
    if not run:
        return None
    return min((item for item in run.results if item.healthy and item.average_ms is not None),
               key=lambda item: (item.average_ms,
                                 item.median_ms if item.median_ms is not None else float("inf"),
                                 item.jitter_ms if item.jitter_ms is not None else float("inf")),
               default=None)


def _base_context(request: Request, **values):
    return {"request": request, "csrf_token": _csrf_token(request),
            "version": VERSION_DISPLAY, **values}


def _persist_verified_current_ip(session: Session, target_id: int, current_ip: str | None) -> None:
    state = session.get(OptimizerStateRecord, target_id)
    if state is None:
        state = OptimizerStateRecord(target_id=target_id)
        session.add(state)
    state.current_rewrite_ip = current_ip
    session.commit()


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
        next_run = next_run_time(completed, target.interval_hours) if completed else None
        rows.append({"target": target, "state": state, "run": run, "best": best,
                     "current": current, "improvement": improvement,
                     "wins": state.consecutive_wins if state else 0,
                     "last_run": schedule.last_run_at if schedule else completed,
                     "next_run": schedule.next_run_at if schedule else next_run,
                     "status": "Disabled" if not target.enabled else
                     "No benchmark" if not run else "Healthy" if best else "Critical"})
    return rows


def _add_to_dns_eligibility(session: Session, target: TargetRecord, run=None) -> dict:
    """Resolve eligibility and the current rewrite from the authoritative AdGuard read."""
    runs = list_benchmark_runs(session, target.id)
    latest = get_benchmark_run(session, runs[0].id) if runs else None
    if run is not None and (latest is None or latest.id != run.id):
        run = latest
    latest = latest or run
    best = _best_benchmark_result(latest)
    current_ip, lookup_succeeded = read_adguard_rewrite(target.hostname)
    if lookup_succeeded:
        state = session.get(OptimizerStateRecord, target.id)
        if state is None:
            state = OptimizerStateRecord(target_id=target.id)
            session.add(state)
        state.current_rewrite_ip = current_ip
        session.commit()
    result = {"latest": latest, "best": best, "can_add_to_dns": False,
              "current_ip": current_ip if lookup_succeeded else None,
              "current_rewrite_lookup_succeeded": lookup_succeeded}
    if (not latest or not target.enabled or not best or not best.healthy or not best.ip
            or best.average_ms is None):
        return result
    try:
        best.ip = str(ipaddress.IPv4Address(best.ip))
    except ipaddress.AddressValueError:
        return result
    age = datetime.now(timezone.utc) - latest.completed_at.replace(tzinfo=timezone.utc)
    if age < timedelta(0) or age > ADD_BEST_MAX_AGE:
        return result
    result["can_add_to_dns"] = bool(lookup_succeeded and current_ip is None)
    return result


def _latest_run_map(session: Session, targets: list[TargetRecord]) -> dict[int, tuple]:
    if not targets:
        return {}
    target_ids = [target.id for target in targets]
    latest_runs = {}
    runs = session.scalars(select(BenchmarkRunRecord).where(
        BenchmarkRunRecord.target_id.in_(target_ids)
    ).order_by(BenchmarkRunRecord.target_id, BenchmarkRunRecord.completed_at.desc(),
               BenchmarkRunRecord.id.desc())).all()
    targets_by_id = {target.id: target for target in targets}
    for run in runs:
        if run.target_id not in latest_runs:
            target = targets_by_id[run.target_id]
            eligibility = _add_to_dns_eligibility(session, target, run)
            latest_runs[run.target_id] = (run, eligibility["best"], eligibility["can_add_to_dns"],
                                          eligibility["current_ip"],
                                          eligibility["current_rewrite_lookup_succeeded"])
    return latest_runs


@router.get("/", response_class=HTMLResponse, name="dashboard", dependencies=[Depends(require_admin)])
def dashboard(request: Request, session: Session = Depends(get_session)):
    return _settings_response(request, session)


@router.get("/targets", response_class=HTMLResponse, name="targets_page", dependencies=[Depends(require_admin)])
def targets_page(request: Request, session: Session = Depends(get_session)):
    targets = sorted(list_targets(session), key=lambda target: target.hostname.casefold())
    latest_runs = _latest_run_map(session, targets)
    return templates.TemplateResponse(request, "targets.html",
                                      _base_context(request, targets=targets,
                                                    target_rows=[{"target": item, "sequence": index}
                                                                 for index, item in enumerate(targets, 1)],
                                                    latest_runs=latest_runs, target=None,
                                                    errors=None,
                                                    default_interval_hours=default_interval_hours(session)))


@router.get("/targets/{target_id}/run/state", response_class=HTMLResponse,
            name="run_state_page", dependencies=[Depends(require_admin)])
def run_state_page(target_id: int, request: Request, session: Session = Depends(get_session)):
    target = get_target_or_404(session, target_id)
    run_status, queue_position = request.app.state.run_coordinator.state(target_id)
    runs = list_benchmark_runs(session, target_id)
    latest = get_benchmark_run(session, runs[0].id) if runs else None
    eligibility = _add_to_dns_eligibility(session, target, latest)
    return templates.TemplateResponse(request, "run_state.html", _base_context(
        request, target=target, run_status=run_status, queue_position=queue_position,
        latest=latest, best=eligibility["best"], can_add_to_dns=eligibility["can_add_to_dns"]))


@router.post("/targets/{target_id}/ping", response_class=HTMLResponse, name="ping_target_page",
             dependencies=[Depends(protect_mutation)])
def ping_target_page(target_id: int, request: Request, session: Session = Depends(get_session)):
    target = get_target_or_404(session, target_id)
    runs = list_benchmark_runs(session, target_id)
    latest = get_benchmark_run(session, runs[0].id) if runs else None
    best = _best_benchmark_result(latest)
    state = session.get(OptimizerStateRecord, target_id)
    current_ip = state.current_rewrite_ip if state else (latest.summary.get("current_rewrite_ip") if latest else None)
    ip = best.ip if best else current_ip
    ping_session = None
    error = None
    if ip:
        try:
            ping_session = ping_sessions.start(target_id, ip)
        except PingLimitReached as exc:
            raise HTTPException(status_code=429, detail="Too many active ping sessions") from exc
        except (OSError, ValueError) as exc:
            logger.warning("Live ping could not start target_id=%d error=%s", target_id, type(exc).__name__)
            error = "Ping could not be started"
    else:
        error = "No IP available"
    if ping_session:
        logger.info("Live ping started target_id=%d ip=%s", target_id, ping_session.ip)
    return _ping_response(request, target_id, ping_session, active=bool(ping_session),
                          update_status=True, error=error)


@router.get("/targets/{target_id}/ping/output", response_class=HTMLResponse,
            name="ping_output_page", dependencies=[Depends(require_admin)])
def ping_output_page(target_id: int, request: Request, session: Session = Depends(get_session)):
    get_target_or_404(session, target_id)
    ping_session = ping_sessions.get(target_id)
    active = ping_sessions.is_active(target_id)
    return _ping_response(request, target_id, ping_session, active=active,
                          update_status=True)


@router.post("/targets/{target_id}/ping/stop", response_class=HTMLResponse,
             name="stop_ping_page", dependencies=[Depends(protect_mutation)])
def stop_ping_page(target_id: int, request: Request, session: Session = Depends(get_session)):
    get_target_or_404(session, target_id)
    ping_session = ping_sessions.stop(target_id)
    logger.info("Live ping stopped target_id=%d", target_id)
    return _ping_response(request, target_id, ping_session, active=False, update_status=True)


def _ping_response(request: Request, target_id: int, ping_session: PingSession | None,
                   active: bool, update_status: bool, error: str | None = None):
    lines = []
    if ping_session:
        with ping_session.lock:
            lines = list(ping_session.lines)
    return templates.TemplateResponse(request, "ping_console.html", {
        "request": request, "target_id": target_id, "ping_session": ping_session,
        "ip": ping_session.ip if ping_session else None,
        "lines": lines, "started_at": ping_session.started_at if ping_session else None,
        "stop_reason": getattr(ping_session, "stop_reason", None) if ping_session else None,
        "active": active, "update_status": update_status, "error": error,
        "csrf_token": _csrf_token(request),
    })


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
        targets = list_targets(session)
        return templates.TemplateResponse(request, "targets.html", _base_context(
            request, targets=targets, latest_runs=_latest_run_map(session, targets), target=None, errors=[str(exc)],
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
    targets = list_targets(session)
    return templates.TemplateResponse(request, "targets.html",
                                      _base_context(request, targets=targets,
                                                    latest_runs=_latest_run_map(session, targets), target=target,
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
            schedule.next_run_at = next_run_time(schedule.last_run_at, updated.interval_hours)
        add_audit_event(session, "target_updated", record.id, {"hostname": record.hostname})
    except (ValueError, TypeError) as exc:
        logger.info("Target update rejected target_id=%d", target_id)
        targets = list_targets(session)
        return templates.TemplateResponse(request, "targets.html", _base_context(
            request, targets=targets, latest_runs=_latest_run_map(session, targets),
            target=record, errors=[str(exc)], form_values=values,
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
    eligibility = _add_to_dns_eligibility(session, target, latest)
    return templates.TemplateResponse(request, "target_detail.html", _base_context(
        request, target=target, latest=latest, state=state, current=current, best=best,
        current_ip_override=eligibility["current_ip"],
        current_rewrite_lookup_succeeded_override=eligibility["current_rewrite_lookup_succeeded"],
        authoritative_current_ip=eligibility["current_ip"],
        authoritative_current_ip_succeeded=eligibility["current_rewrite_lookup_succeeded"],
        rewrite_result=request.query_params.get("rewrite"),
        rewrite_old_ip=request.query_params.get("old_ip"),
        rewrite_new_ip=request.query_params.get("new_ip"),
        rewrite_ip=request.query_params.get("ip"),
        rewrite_verified_ip=request.query_params.get("verified_ip"),
        rewrite_reason=request.query_params.get("reason"),
        can_add_to_dns=eligibility["can_add_to_dns"],
        runs=runs, rewrite_history=list_rewrite_history(session, target_id)))


@router.post("/targets/{target_id}/run", name="run_target_now",
             dependencies=[Depends(protect_mutation)])
def run_target_now(target_id: int, request: Request, session: Session = Depends(get_session)):
    target = get_target_or_404(session, target_id)
    hostname = target.hostname
    database = request.app.state.database
    cycle = request.app.state.benchmark_cycle

    def execute() -> None:
        with database.session() as run_session:
            run_target = run_session.get(TargetRecord, target_id)
            if run_target is None:
                return
            cycle(run_session, run_target)
            record_target_run(run_session, run_target)
            run_session.commit()
        logger.info("Manual benchmark completed target_id=%d hostname=%s", target_id, hostname)

    run_status, queue_position = request.app.state.run_coordinator.submit(target_id, execute)
    logger.info("Manual benchmark accepted target_id=%d state=%s queue_position=%s",
                target_id, run_status, queue_position)
    if request.headers.get("HX-Request", "").lower() == "true":
        runs = list_benchmark_runs(session, target_id)
        latest = get_benchmark_run(session, runs[0].id) if runs else None
        eligibility = _add_to_dns_eligibility(session, target, latest)
        return templates.TemplateResponse(request, "run_state.html", _base_context(
            request, target=target, run_status=run_status, queue_position=queue_position,
            latest=latest, best=eligibility["best"], can_add_to_dns=eligibility["can_add_to_dns"],
            current_ip_override=eligibility["current_ip"],
            current_rewrite_lookup_succeeded_override=eligibility["current_rewrite_lookup_succeeded"]))
    return RedirectResponse(f"/targets/{target_id}", status_code=303)


@router.post("/targets/{target_id}/apply-best", name="apply_best_rewrite",
             dependencies=[Depends(protect_mutation)])
def apply_best_rewrite(target_id: int, run_id: int = Form(...), old_ip: str = Form(...),
                       new_ip: str = Form(...), confirm: bool = Form(False),
                       session: Session = Depends(get_session)):
    target = get_target_or_404(session, target_id)
    logger.info("manual_apply_request_received target_id=%d hostname=%s selected_ip=%s",
                target_id, target.hostname, new_ip)
    if not confirm:
        raise HTTPException(status_code=400, detail="Explicit rewrite confirmation is required")
    live_ip, lookup_succeeded = read_adguard_rewrite(target.hostname)
    logger.info("manual_apply_authoritative_read hostname=%s submitted_old_ip=%s authoritative_current_ip=%s lookup_succeeded=%s",
                target.hostname, old_ip, live_ip, lookup_succeeded)
    if lookup_succeeded:
        _persist_verified_current_ip(session, target_id, live_ip)
    if not lookup_succeeded:
        add_audit_event(session, "manual_apply_failed", target_id,
                        {"hostname": target.hostname, "submitted_old_ip": old_ip,
                         "new_ip": new_ip, "reason": "current_rewrite_unavailable"})
        session.commit()
        logger.warning("manual_apply_failed hostname=%s old_ip=%s new_ip=%s reason=current_rewrite_unavailable",
                       target.hostname, old_ip, new_ip)
        return RedirectResponse(f"/targets/{target_id}?rewrite=failed&reason=current", status_code=303)
    run = get_benchmark_run(session, run_id)
    runs = list_benchmark_runs(session, target_id)
    if not run or run.target_id != target_id or not runs or runs[0].id != run_id:
        add_audit_event(session, "manual_apply_failed", target_id,
                        {"hostname": target.hostname, "old_ip": old_ip, "new_ip": new_ip,
                         "reason": "stale_benchmark"})
        session.commit()
        return RedirectResponse(f"/targets/{target_id}?rewrite=failed&reason=stale", status_code=303)
    if run.summary.get("resolution_failed"):
        add_audit_event(session, "manual_apply_failed", target_id,
                        {"hostname": target.hostname, "old_ip": old_ip, "new_ip": new_ip,
                         "reason": "benchmark_unavailable"})
        session.commit()
        return RedirectResponse(f"/targets/{target_id}?rewrite=failed&reason=candidate", status_code=303)
    best = _best_benchmark_result(run)
    benchmark_age = datetime.now(timezone.utc) - run.completed_at.replace(tzinfo=timezone.utc)
    try:
        best_ip = str(ipaddress.IPv4Address(best.ip)) if best and best.healthy else None
        selected_ip = str(ipaddress.IPv4Address(new_ip))
    except ipaddress.AddressValueError:
        best_ip = selected_ip = None
    if (benchmark_age < timedelta(0) or benchmark_age > ADD_BEST_MAX_AGE
            or not best or not best.healthy or best_ip is None or selected_ip != best_ip):
        add_audit_event(session, "manual_apply_failed", target_id,
                        {"hostname": target.hostname, "old_ip": old_ip, "new_ip": new_ip,
                         "reason": "candidate_unhealthy_or_stale"})
        session.commit()
        return RedirectResponse(f"/targets/{target_id}?rewrite=failed&reason=candidate", status_code=303)
    new_ip = best_ip
    if live_ip is None:
        add_audit_event(session, "manual_apply_failed", target_id,
                        {"hostname": target.hostname, "submitted_old_ip": old_ip, "new_ip": new_ip,
                         "reason": "current_rewrite_absent"})
        session.commit()
        return RedirectResponse(f"/targets/{target_id}?rewrite=failed&reason=current", status_code=303)
    # The hidden old_ip is context only. AdGuard's normalized read is the baseline.
    authoritative_old_ip = live_ip
    if authoritative_old_ip == new_ip:
        add_audit_event(session, "manual_apply_already_applied", target_id,
                        {"hostname": target.hostname, "submitted_old_ip": old_ip,
                         "authoritative_current_ip": authoritative_old_ip,
                         "new_ip": new_ip, "benchmark_run_id": run_id})
        session.commit()
        logger.info("manual_apply_success hostname=%s old_ip=%s new_ip=%s idempotent=True",
                    target.hostname, authoritative_old_ip, new_ip)
        return RedirectResponse(f"/targets/{target_id}?rewrite=already-applied&ip={new_ip}", status_code=303)
    logger.info("manual_apply_started hostname=%s old_ip=%s new_ip=%s run_id=%s",
                target.hostname, authoritative_old_ip, new_ip, run_id)
    try:
        outcome = set_rewrite(session, target, new_ip, "Manual Apply Best IP", run_id,
                              expected_old_ip=authoritative_old_ip)
    except AdGuardError as exc:
        logger.warning("manual_apply_failed hostname=%s old_ip=%s new_ip=%s reason=%s",
                       target.hostname, authoritative_old_ip, new_ip, type(exc).__name__)
        current_ip, lookup_succeeded = read_adguard_rewrite(target.hostname)
        _persist_verified_current_ip(session, target_id, current_ip if lookup_succeeded else None)
        if lookup_succeeded and current_ip == new_ip:
            add_audit_event(session, "manual_apply_already_applied", target_id,
                            {"hostname": target.hostname, "old_ip": authoritative_old_ip,
                             "new_ip": new_ip, "benchmark_run_id": run_id,
                             "reason": "concurrent_apply_reached_selected_ip"})
            session.commit()
            logger.info("manual_apply_success hostname=%s old_ip=%s new_ip=%s idempotent=True",
                        target.hostname, authoritative_old_ip, new_ip)
            return RedirectResponse(f"/targets/{target_id}?rewrite=already-applied&ip={new_ip}", status_code=303)
        if ("changed since the benchmark" in str(exc)
                and lookup_succeeded and current_ip != authoritative_old_ip):
            add_audit_event(session, "manual_apply_conflict", target_id,
                            {"hostname": target.hostname, "old_ip": authoritative_old_ip,
                             "new_ip": new_ip, "verified_current_ip": current_ip,
                             "reason": "external_rewrite_change"})
            session.commit()
            logger.warning("manual_apply_conflict hostname=%s old_ip=%s new_ip=%s actual_ip=%s",
                           target.hostname, authoritative_old_ip, new_ip, current_ip)
            return RedirectResponse(
                f"/targets/{target_id}?rewrite=conflict&verified_ip={current_ip or ''}", status_code=303)
        add_audit_event(session, "manual_apply_failed", target_id,
                        {"hostname": target.hostname, "old_ip": authoritative_old_ip, "new_ip": new_ip,
                         "verified_current_ip": current_ip if lookup_succeeded else None,
                         "reason": "adguard_write_failed", "error": type(exc).__name__})
        session.commit()
        return RedirectResponse(f"/targets/{target_id}?rewrite=failed&reason=adguard", status_code=303)
    except Exception as exc:
        logger.warning("manual_apply_failed hostname=%s old_ip=%s new_ip=%s reason=%s",
                       target.hostname, authoritative_old_ip, new_ip, type(exc).__name__)
        current_ip, lookup_succeeded = read_adguard_rewrite(target.hostname)
        _persist_verified_current_ip(session, target_id, current_ip if lookup_succeeded else None)
        add_audit_event(session, "manual_apply_failed", target_id,
                        {"hostname": target.hostname, "old_ip": authoritative_old_ip, "new_ip": new_ip,
                         "verified_current_ip": current_ip if lookup_succeeded else None,
                         "reason": "rewrite_or_persistence_failed", "error": type(exc).__name__})
        session.commit()
        return RedirectResponse(f"/targets/{target_id}?rewrite=failed&reason=other", status_code=303)
    verified_current_ip = outcome.get("verified_current_ip")
    _persist_verified_current_ip(session, target_id, verified_current_ip)
    if not outcome.get("changed") and verified_current_ip == new_ip:
        add_audit_event(session, "manual_apply_already_applied", target_id,
                        {"hostname": target.hostname, "old_ip": authoritative_old_ip,
                         "new_ip": new_ip, "verified_current_ip": verified_current_ip,
                         "benchmark_run_id": run_id, "reason": "concurrent_apply_reached_selected_ip"})
        session.commit()
        logger.info("manual_apply_success hostname=%s old_ip=%s new_ip=%s idempotent=True",
                    target.hostname, authoritative_old_ip, new_ip)
        return RedirectResponse(f"/targets/{target_id}?rewrite=already-applied&ip={new_ip}", status_code=303)
    if outcome.get("rolled_back"):
        return RedirectResponse(f"/targets/{target_id}?rewrite=rolled-back&verified_ip={verified_current_ip or ''}", status_code=303)
    if not outcome.get("healthy") or verified_current_ip != new_ip:
        reason = ("readback" if not outcome.get("readback_verified", verified_current_ip == new_ip)
                  or verified_current_ip != new_ip else "health")
        add_audit_event(session, "manual_apply_failed", target_id,
                        {"hostname": target.hostname, "old_ip": authoritative_old_ip, "new_ip": new_ip,
                         "verified_current_ip": verified_current_ip, "reason": reason})
        session.commit()
        return RedirectResponse(f"/targets/{target_id}?rewrite=failed&reason={reason}", status_code=303)
    return RedirectResponse(
        f"/targets/{target_id}?rewrite=applied&old_ip={authoritative_old_ip}&new_ip={new_ip}", status_code=303)


@router.post("/targets/{target_id}/add-to-dns", name="add_best_rewrite",
             dependencies=[Depends(protect_mutation)])
def add_best_rewrite(target_id: int, request: Request, run_id: int = Form(...),
                     confirm: bool = Form(False), session: Session = Depends(get_session)):
    if not confirm:
        raise HTTPException(status_code=400, detail="Explicit rewrite confirmation is required")
    target = get_target_or_404(session, target_id)
    runs = list_benchmark_runs(session, target_id)
    run = get_benchmark_run(session, run_id)
    if not run or run.target_id != target_id or not runs or runs[0].id != run_id:
        raise HTTPException(status_code=409, detail="Add to DNS requires the latest benchmark result")
    age = datetime.now(timezone.utc) - run.completed_at.replace(tzinfo=timezone.utc)
    best = _best_benchmark_result(run)
    if age < timedelta(0) or age > ADD_BEST_MAX_AGE or not best or not best.healthy:
        raise HTTPException(status_code=409, detail="Best IP is unhealthy or the benchmark is stale")
    try:
        best_ip = str(ipaddress.IPv4Address(best.ip))
    except ipaddress.AddressValueError as exc:
        raise HTTPException(status_code=409, detail="Best IP is not a valid IPv4 address") from exc

    client = None
    try:
        client = configured_adguard_client()
        existing = client.get_rewrite(target.hostname)
        if existing is not None:
            answer = str(ipaddress.IPv4Address(existing["answer"]))
            _persist_verified_current_ip(session, target_id, answer)
            add_audit_event(session, "rewrite_duplicate_synchronized", target_id,
                            {"ip": answer, "benchmark_run_id": run_id})
            session.commit()
            return RedirectResponse(f"/targets/{target_id}?rewrite=already-exists", status_code=303)
        if not _healthy(Target.model_validate(target, from_attributes=True), best_ip):
            add_audit_event(session, "rewrite_add_failed", target_id,
                            {"ip": best_ip, "benchmark_run_id": run_id, "reason": "health_check_failed"})
            session.commit()
            return RedirectResponse(f"/targets/{target_id}?rewrite=unhealthy", status_code=303)
        add_audit_event(session, "rewrite_add_started", target_id,
                        {"ip": best_ip, "benchmark_run_id": run_id})
        session.commit()
        client.add_rewrite(target.hostname, best_ip)
        verified = client.get_rewrite(target.hostname)
        if not verified or verified.get("domain", "").rstrip(".").casefold() != target.hostname.rstrip(".").casefold():
            raise AdGuardError("AdGuard rewrite readback did not match the requested hostname")
        verified_ip = str(ipaddress.IPv4Address(verified["answer"]))
        if verified_ip != best_ip:
            raise AdGuardError("AdGuard rewrite readback did not match the requested IP")
        add_rewrite_history(session, target_id, None, verified_ip, "Manual Add to DNS", run_id)
        _persist_verified_current_ip(session, target_id, verified_ip)
        add_audit_event(session, "rewrite_added", target_id,
                        {"ip": verified_ip, "benchmark_run_id": run_id, "verified": True})
        session.commit()
        return RedirectResponse(f"/targets/{target_id}?rewrite=added", status_code=303)
    except Exception as exc:
        session.rollback()
        try:
            add_audit_event(session, "rewrite_add_failed", target_id,
                            {"ip": best_ip, "benchmark_run_id": run_id, "error": type(exc).__name__})
            session.commit()
        except Exception:
            session.rollback()
        logger.warning("Add to DNS failed target_id=%d error=%s", target_id, type(exc).__name__)
        return RedirectResponse(f"/targets/{target_id}?rewrite=add-failed", status_code=303)
    finally:
        if client is not None:
            client.close()


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


@router.post("/history/clear", name="clear_benchmark_history",
             dependencies=[Depends(protect_mutation)])
def clear_benchmark_history_page(request: Request, confirm: bool = Form(False),
                                 session: Session = Depends(get_session)):
    if not confirm:
        raise HTTPException(status_code=400, detail="Confirmation is required to clear benchmark history")
    counts = clear_benchmark_history(session)
    add_audit_event(session, "benchmark_history_cleared", details=counts)
    logger.info("Benchmark history cleared runs=%d results=%d samples=%d",
                counts["runs_deleted"], counts["results_deleted"], counts["samples_deleted"])
    return RedirectResponse("/history", status_code=303)


@router.get("/settings", response_class=HTMLResponse, name="settings_page",
            dependencies=[Depends(require_admin)])
def settings_page(request: Request, session: Session = Depends(get_session),
                  _auth=Depends(require_admin)):
    return RedirectResponse("/#settings", status_code=303)


def _settings_response(request: Request, session: Session, errors=None,
                       interval_value: str | None = None, interval_unit: str | None = None,
                       retention_value: str | None = None, status_code: int = 200,
                       history_value: str | None = None, history_unit: str | None = None):
    import os
    from app.security import safe_endpoint

    value, unit = default_interval(session)
    try:
        log_retention_days = max(1, int(get_setting(session, "log_retention_days") or "7"))
    except ValueError:
        log_retention_days = 7
    configured_history_value, configured_history_unit = configured_benchmark_history_retention(session)
    return templates.TemplateResponse(request, "dashboard.html", _base_context(
        request, rows=_dashboard_rows(session),
        adguard_url=safe_endpoint(os.getenv("ADGUARD_URL") or "Not configured"),
        scheduler_enabled=scheduler_enabled(session),
        automatic_rewrite_enabled=automatic_rewrite_enabled(session),
        default_interval_value=interval_value if interval_value is not None else value,
        default_interval_unit=interval_unit if interval_unit is not None else unit,
        threshold_ms=50, threshold_percent=5,
        max_auto_changes=int(get_setting(session, "max_auto_changes_per_day") or "4"),
        log_retention_days=retention_value if retention_value is not None else log_retention_days,
        benchmark_history_value=history_value if history_value is not None else configured_history_value,
        benchmark_history_unit=history_unit if history_unit is not None else configured_history_unit,
        settings_errors=errors), status_code=status_code)


@router.post("/settings/benchmark-history-retention", name="benchmark_history_retention_setting",
             dependencies=[Depends(protect_mutation)])
def benchmark_history_retention_setting(request: Request, value: str = Form(...),
                                        unit: str = Form(...), session: Session = Depends(get_session)):
    try:
        parsed = int(value)
        if parsed < 1 or unit not in {"hours", "days"}:
            raise ValueError
    except ValueError:
        return _settings_response(request, session,
                                  {"benchmark_history": "Enter a positive whole number and choose hours or days."},
                                  status_code=422, history_value=value, history_unit=unit)
    set_setting(session, "benchmark_history_retention_value", str(parsed))
    set_setting(session, "benchmark_history_retention_unit", unit)
    add_audit_event(session, "benchmark_history_retention_updated",
                    details={"value": parsed, "unit": unit})
    logger.info("Benchmark history retention updated value=%d unit=%s", parsed, unit)
    return RedirectResponse("/#settings", status_code=303)


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
    return RedirectResponse("/#settings", status_code=303)


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
    return RedirectResponse("/#settings", status_code=303)


@router.post("/settings/scheduler", name="scheduler_setting",
             dependencies=[Depends(protect_mutation)])
def scheduler_setting(enabled: bool = Form(False), session: Session = Depends(get_session)):
    set_scheduler_enabled(session, enabled)
    return RedirectResponse("/#settings", status_code=303)


@router.post("/settings/automatic-rewrite", name="automatic_rewrite_setting",
             dependencies=[Depends(protect_mutation)])
def automatic_rewrite_setting(enabled: bool = Form(False), confirm: bool = Form(False),
                              session: Session = Depends(get_session)):
    if enabled and not confirm:
        raise HTTPException(status_code=400, detail="Confirmation is required to enable automatic DNS rewrites")
    set_setting(session, "global_auto_master", str(enabled).lower())
    add_audit_event(session, "automatic_rewrite_master_enabled" if enabled else
                    "automatic_rewrite_master_disabled", details={"enabled": enabled})
    logger.info("Automatic DNS Rewrite master switch %s", "enabled" if enabled else "disabled")
    return RedirectResponse("/#settings", status_code=303)


@router.post("/targets/{target_id}/auto-apply", name="target_auto_apply_setting",
             dependencies=[Depends(protect_mutation)])
def target_auto_apply_setting(target_id: int, enabled: bool = Form(False),
                              session: Session = Depends(get_session)):
    record = get_target_or_404(session, target_id)
    record.auto_apply = enabled
    add_audit_event(session, "target_auto_apply_enabled" if enabled else "target_auto_apply_disabled",
                    record.id, {"enabled": enabled})
    logger.info("Target Auto Apply %s target_id=%d hostname=%s",
                "enabled" if enabled else "disabled", record.id, record.hostname)
    return RedirectResponse("/targets", status_code=303)


@router.post("/settings/change-limit", name="change_limit_setting",
             dependencies=[Depends(protect_mutation)])
def change_limit_setting(max_auto_changes_per_day: int = Form(4),
                         session: Session = Depends(get_session)):
    if not 0 <= max_auto_changes_per_day <= 1000:
        raise HTTPException(status_code=422, detail="Change limit must be between 0 and 1000")
    set_setting(session, "max_auto_changes_per_day", str(max_auto_changes_per_day))
    return RedirectResponse("/#settings", status_code=303)
