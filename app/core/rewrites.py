"""AdGuard rewrite changes with immediate post-change health checks."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.benchmark import HttpsBenchmarkRunner
from app.db.models import OptimizerStateRecord, TargetRecord
from app.db.repositories import (
    add_audit_event, add_rewrite_history, count_automatic_rewrites_since,
    get_setting, set_setting,
)
from app.integrations.adguard import AdGuardClient, AdGuardError, configured_adguard_client
from app.models.target import Target

logger = logging.getLogger(__name__)


def _adguard_client() -> AdGuardClient:
    return configured_adguard_client()


def _write(client: AdGuardClient, hostname: str, old_ip: str | None, new_ip: str | None) -> None:
    if old_ip == new_ip:
        return
    if new_ip is None:
        if old_ip:
            client.delete_rewrite(hostname, old_ip)
    elif old_ip:
        client.update_rewrite(hostname, old_ip, hostname, new_ip)
    else:
        client.add_rewrite(hostname, new_ip)


def _healthy(target: Target, ip: str) -> bool:
    result = HttpsBenchmarkRunner(3, target.timeout_seconds).benchmark_ip(
        target.hostname, ip, path=target.path, port=target.port)
    return result.healthy


def _rewrite_is(client: AdGuardClient, hostname: str, ip: str | None) -> bool:
    current = client.get_rewrite(hostname)
    return (current.get("answer") if current else None) == ip


def _restore_rewrite(client: AdGuardClient, hostname: str,
                     old_ip: str | None) -> tuple[bool, str | None]:
    current = client.get_rewrite(hostname)
    current_ip = current.get("answer") if current else None
    if current_ip != old_ip:
        _write(client, hostname, current_ip, old_ip)
    restored = client.get_rewrite(hostname)
    verified_ip = restored.get("answer") if restored else None
    return verified_ip == old_ip, verified_ip


def _manual_apply_log(stage: str, hostname: str, old_ip: str | None,
                      new_ip: str | None, **details) -> None:
    """Emit compact, credential-free milestones for an operator initiated apply."""
    if details:
        logger.info("manual_apply_%s hostname=%s old_ip=%s new_ip=%s details=%s",
                    stage, hostname, old_ip, new_ip, details)
    else:
        logger.info("manual_apply_%s hostname=%s old_ip=%s new_ip=%s",
                    stage, hostname, old_ip, new_ip)


def set_rewrite(session: Session, record: TargetRecord, new_ip: str | None,
                reason: str, benchmark_run_id: int | None = None,
                automatic: bool = False, expected_old_ip: str | None = None) -> dict:
    """Write a rewrite, verify it from this host, and immediately restore on failed health."""
    target = Target.model_validate(record, from_attributes=True)
    client = _adguard_client()
    old_ip: str | None = None
    mutation_started = False
    try:
        existing = client.get_rewrite(target.hostname)
        old_ip = existing.get("answer") if existing else None
        if expected_old_ip is not None and old_ip != expected_old_ip:
            raise AdGuardError("AdGuard rewrite changed since the benchmark")
        if old_ip == new_ip:
            return {"changed": False, "old_ip": old_ip, "new_ip": new_ip,
                    "verified_current_ip": old_ip, "healthy": True}

        # Commit an audit intent before the external mutation, so a broken database prevents DNS changes.
        add_audit_event(session, "rewrite_change_started", record.id,
                        {"old_ip": old_ip, "new_ip": new_ip, "reason": reason})
        session.commit()
        mutation_started = True
        if reason == "Manual Apply Best IP":
            _manual_apply_log("adguard_write", target.hostname, old_ip, new_ip,
                              method="PUT", endpoint="/control/rewrite/update",
                              target_domain=target.hostname, target_answer=old_ip,
                              update_domain=target.hostname, update_answer=new_ip)
        _write(client, target.hostname, old_ip, new_ip)
        rewrite_verified = _rewrite_is(client, target.hostname, new_ip)
        if reason == "Manual Apply Best IP":
            readback = client.get_rewrite(target.hostname)
            actual_ip = readback.get("answer") if readback else None
            _manual_apply_log("readback", target.hostname, old_ip, new_ip,
                              verified=rewrite_verified, actual_ip=actual_ip)
        healthy = rewrite_verified and (new_ip is None or _healthy(target, new_ip))
        if reason == "Manual Apply Best IP":
            _manual_apply_log("health", target.hostname, old_ip, new_ip,
                              healthy=healthy if rewrite_verified else None,
                              readback_verified=rewrite_verified)
        if healthy:
            add_rewrite_history(session, record.id, old_ip, new_ip, reason,
                                benchmark_run_id, automatic=automatic)
            state = session.get(OptimizerStateRecord, record.id)
            if state is None:
                state = OptimizerStateRecord(target_id=record.id)
                session.add(state)
            state.current_rewrite_ip = new_ip
            add_audit_event(session, "rewrite_applied", record.id,
                            {"old_ip": old_ip, "new_ip": new_ip, "reason": reason,
                             "automatic": automatic, "healthy": healthy})
            session.commit()
            if reason == "Manual Apply Best IP":
                _manual_apply_log("success", target.hostname, old_ip, new_ip)
            return {"changed": True, "old_ip": old_ip, "new_ip": new_ip,
                    "verified_current_ip": new_ip, "healthy": healthy}

        restored, verified_current_ip = _restore_rewrite(client, target.hostname, old_ip)
        mutation_started = False
        if rewrite_verified and restored:
            # History is a record of verified DNS transitions, never an unconfirmed write.
            add_rewrite_history(session, record.id, new_ip, old_ip,
                                "Rollback after failed post-change health check", benchmark_run_id,
                                automatic=False)
        state = session.get(OptimizerStateRecord, record.id)
        if state is None:
            state = OptimizerStateRecord(target_id=record.id)
            session.add(state)
        state.current_rewrite_ip = verified_current_ip
        event = "rewrite_rollback" if rewrite_verified and restored else "rewrite_apply_failed"
        add_audit_event(session, event, record.id,
                        {"old_ip": old_ip, "new_ip": new_ip, "verified_current_ip": verified_current_ip,
                         "health_check": "failed" if rewrite_verified else "not_run",
                         "readback_verified": rewrite_verified, "restored": restored})
        session.commit()
        if reason == "Manual Apply Best IP":
            _manual_apply_log("rollback" if rewrite_verified and restored else "failed",
                              target.hostname, old_ip, new_ip,
                              restored=restored, verified_current_ip=verified_current_ip)
        logger.warning("Rewrite apply did not complete host=%s requested_ip=%s restored=%s",
                       target.hostname, new_ip, restored)
        return {"changed": rewrite_verified, "rolled_back": rewrite_verified and restored,
                "readback_verified": rewrite_verified, "old_ip": old_ip,
                "verified_current_ip": verified_current_ip,
                "new_ip": new_ip, "healthy": False}
    except Exception as exc:
        if reason == "Manual Apply Best IP":
            _manual_apply_log("failed", target.hostname, old_ip, new_ip,
                              error=type(exc).__name__)
        try:
            session.rollback()
        except Exception as database_rollback_error:
            logger.error("Database rollback failed after rewrite attempt host=%s error=%s",
                         target.hostname, type(database_rollback_error).__name__)
        if mutation_started:
            try:
                _restore_rewrite(client, target.hostname, old_ip)
            except Exception as rollback_error:
                logger.critical("Compensating rewrite failed host=%s error=%s",
                                target.hostname, type(rollback_error).__name__)
        raise
    finally:
        client.close()


def automatic_rewrite_enabled(session: Session) -> bool:
    return (get_setting(session, "global_auto_master") or "false").lower() == "true"


def apply_automatic_decision(session: Session, record: TargetRecord, output: dict) -> dict:
    """Apply UPDATE/FAILOVER only when both global and per-target opt-ins are enabled."""
    decision = output.get("decision", {})
    if record.mode != "auto" or decision.get("action") not in {"UPDATE", "FAILOVER"}:
        return {"rewrite_applied": False}
    if not automatic_rewrite_enabled(session):
        reason = "Automatic DNS Rewrite master switch is OFF"
        add_audit_event(session, "auto_change_blocked", record.id, {"reason": reason})
        logger.info("Automatic rewrite blocked host=%s reason=global_master_off", record.hostname)
        return {"rewrite_applied": False, "change_limited": False, "reason": reason}
    if not record.auto_apply:
        reason = "Auto Apply is OFF for this target"
        add_audit_event(session, "auto_change_blocked", record.id, {"reason": reason})
        logger.info("Automatic rewrite blocked host=%s reason=target_opt_in_off", record.hostname)
        return {"rewrite_applied": False, "change_limited": False, "reason": reason}
    if record.manual_lock_ip:
        reason = "Automatic rewrite blocked by manual IP lock"
        add_audit_event(session, "auto_change_blocked", record.id, {"reason": reason})
        logger.info("Automatic rewrite blocked host=%s reason=manual_lock", record.hostname)
        return {"rewrite_applied": False, "change_limited": False, "reason": reason}
    try:
        maximum = int(get_setting(session, "max_auto_changes_per_day") or "4")
    except ValueError:
        maximum = 4
    changes = count_automatic_rewrites_since(session, datetime.now(timezone.utc) - timedelta(days=1))
    if changes >= maximum:
        reason = f"Daily automatic change limit reached ({changes}/{maximum}); AUTO treated as RECOMMEND"
        set_setting(session, "last_auto_change_limit_reason", reason)
        add_audit_event(session, "auto_change_limited", record.id, {"reason": reason, "count": changes,
                                                                     "limit": maximum})
        logger.warning("Automatic rewrite limited host=%s count=%d limit=%d", record.hostname, changes, maximum)
        return {"rewrite_applied": False, "change_limited": True, "reason": reason}
    candidate_ip = decision.get("candidate_ip")
    if not candidate_ip:
        return {"rewrite_applied": False, "reason": "Decision did not name a candidate IP"}
    try:
        result = set_rewrite(session, record, candidate_ip, decision.get("reason", "Optimizer decision"),
                             output.get("benchmark_run_id"), automatic=True)
    except AdGuardError as exc:
        logger.warning("Automatic rewrite failed host=%s error=%s", record.hostname, type(exc).__name__)
        add_audit_event(session, "auto_change_failed", record.id, {"error": type(exc).__name__})
        return {"rewrite_applied": False, "reason": "AdGuard rewrite failed"}
    return {"rewrite_applied": result["changed"] and result.get("healthy", False), **result}
