"""Shared read-only benchmark and decision cycle used by CLI and API."""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.benchmark import HttpsBenchmarkRunner
from app.core.decision import DecisionEngine
from app.core.discovery import DiscoveryError, PublicDnsDiscovery
from app.db.models import TargetRecord
from app.db.repositories import (
    add_audit_event, get_current_rewrite_ip, get_pending_state, save_benchmark_run,
    save_optimizer_state,
)
from app.integrations.adguard import AdGuardError, configured_adguard_client
from app.models.benchmark import DecisionResult, PendingCandidateState
from app.models.target import Target

logger = logging.getLogger(__name__)


def read_adguard_rewrite(hostname: str) -> tuple[str | None, bool]:
    """Return (IP, lookup_succeeded); never changes AdGuard state."""
    try:
        client = configured_adguard_client()
    except AdGuardError as exc:
        if str(exc) == "No AdGuard endpoint discovered":
            logger.warning("Current AdGuard rewrite unavailable: no endpoint discovered host=%s", hostname)
        elif str(exc) == "Multiple AdGuard endpoints discovered":
            logger.warning("Current AdGuard rewrite unavailable: multiple endpoints discovered host=%s", hostname)
        else:
            logger.warning("Current AdGuard rewrite unavailable host=%s error=%s", hostname, exc)
        return None, False
    try:
        rewrite = client.get_rewrite(hostname)
        if rewrite is None:
            return None, True
        answer = rewrite["answer"]
        try:
            parsed = ipaddress.ip_address(answer)
        except ValueError:
            logger.warning("Current AdGuard rewrite is not an IP address host=%s", hostname)
            return None, False
        if parsed.version != 4:
            logger.warning("Current AdGuard rewrite is not IPv4 host=%s", hostname)
            return None, False
        return str(parsed), True
    except AdGuardError as exc:
        logger.warning("Current AdGuard rewrite unavailable host=%s error=%s", hostname, exc)
        return None, False
    finally:
        client.close()


def run_benchmark_cycle(session: Session, record: TargetRecord) -> dict[str, Any]:
    """Benchmark a target, persist its results/state, and return a secret-free summary."""
    config = Target.model_validate(record, from_attributes=True)
    pending = get_pending_state(session, record.id)
    stored_current_ip = get_current_rewrite_ip(session, record.id)
    discovery = PublicDnsDiscovery()
    discovery_error = None
    try:
        public_ips = discovery.discover(config.hostname)
    except DiscoveryError as exc:
        public_ips = []
        discovery_error = str(exc)
        logger.warning("Public DNS discovery failed host=%s error_type=%s",
                       config.hostname, type(exc).__name__)
    finally:
        discovery.close()

    resolved_current_ip, lookup_succeeded = read_adguard_rewrite(config.hostname)
    if not lookup_succeeded:
        resolved_current_ip = stored_current_ip
    resolution_failed = not public_ips
    if resolution_failed and not discovery_error:
        logger.warning("Public DNS discovery returned no valid IPv4 records host=%s", config.hostname)
    current_ip = None if resolution_failed else resolved_current_ip
    current_rewrite_in_public_dns = bool(current_ip and current_ip in public_ips)
    candidate_ips = [] if resolution_failed else list(dict.fromkeys(
        [*public_ips, *([current_ip] if current_ip else []),
         *([config.manual_lock_ip] if config.manual_lock_ip else [])]
    ))
    results = []
    if resolution_failed:
        reason = "Public DNS discovery failed or returned no valid IP."
        if not discovery_error:
            discovery_error = "No valid public IPv4 A records were returned."
        decision = DecisionResult(action="RESOLUTION_FAILED", reason=reason)
    else:
        results = HttpsBenchmarkRunner(config.runs_per_ip, config.timeout_seconds).benchmark(
            config.hostname, candidate_ips, path=config.path, port=config.port)
        results_by_ip = {result.ip: result for result in results}
        decision = DecisionEngine().decide(
            current_ip, results_by_ip.get(current_ip), results, pending=pending,
            switch_threshold_ms=config.switch_threshold_ms,
            switch_threshold_percent=config.switch_threshold_percent,
            required_consecutive_wins=config.required_consecutive_wins,
            manual_lock_ip=config.manual_lock_ip,
            immediate_switch_if_current_unhealthy=config.immediate_switch_if_current_unhealthy,
        )
    next_pending = (PendingCandidateState(candidate_ip=decision.candidate_ip, consecutive_wins=decision.wins)
                    if decision.action in ("HOLD", "UPDATE") else PendingCandidateState())
    summary = {"current_rewrite_ip": current_ip,
               "public_ips": public_ips,
               "candidate_ips": candidate_ips,
               "resolution_failed": resolution_failed,
               "current_rewrite_included": bool(current_ip and current_ip in candidate_ips),
               "current_rewrite_in_public_dns": current_rewrite_in_public_dns,
               "current_rewrite_lookup_succeeded": bool(lookup_succeeded and not resolution_failed),
               "candidate_count": len(results), "discovery_error": discovery_error}
    with session.begin_nested():
        run = save_benchmark_run(session, record.id, results, summary=summary, decision=decision)
        save_optimizer_state(session, record.id, resolved_current_ip, next_pending, decision)
        add_audit_event(session, "benchmark_completed", record.id,
                        {**summary, "decision_action": decision.action, "decision_reason": decision.reason})
    return {"hostname": config.hostname, "current_ip": current_ip,
            "current_rewrite_included": summary["current_rewrite_included"],
            "current_rewrite_in_public_dns": current_rewrite_in_public_dns,
            "public_ips": public_ips,
            "candidate_ips": candidate_ips,
            "benchmark_run_id": run.id,
            "candidates": [result.model_dump() for result in results],
            "decision": decision.model_dump()}
