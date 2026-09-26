"""Shared read-only benchmark and decision cycle used by CLI and API."""

from __future__ import annotations

import logging
import os
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
from app.integrations.adguard import AdGuardClient, AdGuardError, discover_adguard
from app.models.benchmark import PendingCandidateState
from app.models.target import Target
from app.security import load_secret_environment

logger = logging.getLogger(__name__)


def read_adguard_rewrite(hostname: str) -> tuple[str | None, bool]:
    """Return (IP, lookup_succeeded); never changes AdGuard state."""
    load_secret_environment()
    endpoints = discover_adguard(os.getenv("ADGUARD_URL"))
    if not endpoints:
        return None, False
    if len(endpoints) > 1:
        logger.warning("Multiple AdGuard instances found; configure ADGUARD_URL to select one")
        return None, False
    client = AdGuardClient(base_url=endpoints[0])
    try:
        rewrite = client.get_rewrite(hostname)
        return (rewrite.get("answer") if rewrite else None), True
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
    current_ip, lookup_succeeded = read_adguard_rewrite(config.hostname)
    if not lookup_succeeded:
        current_ip = stored_current_ip

    discovery = PublicDnsDiscovery()
    try:
        public_ips = discovery.discover(config.hostname)
    finally:
        discovery.close()
    current_rewrite_in_public_dns = bool(current_ip and current_ip in public_ips)
    candidate_ips = list(dict.fromkeys(
        [*public_ips, *([current_ip] if current_ip else []),
         *([config.manual_lock_ip] if config.manual_lock_ip else [])]
    ))
    if not candidate_ips:
        raise DiscoveryError(f"No public IPv4 A records found for {config.hostname}")

    results = HttpsBenchmarkRunner(config.runs_per_ip, config.timeout_seconds).benchmark(
        config.hostname, candidate_ips, path=config.path, port=config.port)
    results_by_ip = {result.ip: result for result in results}
    current = results_by_ip.get(current_ip)
    decision = DecisionEngine().decide(
        current_ip, current, results, pending=pending,
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
               "current_rewrite_included": bool(current_ip and current_ip in candidate_ips),
               "current_rewrite_in_public_dns": current_rewrite_in_public_dns,
               "candidate_count": len(results)}
    with session.begin_nested():
        run = save_benchmark_run(session, record.id, results, summary=summary, decision=decision)
        save_optimizer_state(session, record.id, current_ip, next_pending, decision)
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
