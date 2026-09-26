"""Safe read-only command-line entry point for DRO."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from app.core.benchmark import HttpsBenchmarkRunner
from app.core.discovery import DiscoveryError, PublicDnsDiscovery
from app.core.decision import DecisionEngine
from app.db.database import Database
from app.db.repositories import (add_audit_event, get_pending_state, get_target,
                                 get_current_rewrite_ip, save_benchmark_run,
                                 save_optimizer_state, save_target)
from app.integrations.adguard import AdGuardClient, AdGuardError, discover_adguard
from app.models.benchmark import PendingCandidateState
from app.models.target import Target
from app.security import load_secret_environment, safe_endpoint
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger("dro")


def benchmark_command(hostname: str) -> int:
    load_secret_environment()
    default_target = Target(hostname=hostname)
    hostname = default_target.hostname
    database = Database()
    try:
        with database.session() as session:
            record = get_target(session, hostname) or save_target(session, default_target)
            target_id = record.id
            target = Target.model_validate({key: getattr(record, key) for key in Target.model_fields})
            pending = get_pending_state(session, target_id)
            stored_current_ip = get_current_rewrite_ip(session, target_id)
    except SQLAlchemyError as exc:
        logger.error("Database unavailable during benchmark setup host=%s error=%s", hostname, type(exc).__name__)
        print("Database unavailable or not migrated; run 'alembic upgrade head'.", file=sys.stderr)
        database.close()
        return 1

    configured_url = os.getenv("ADGUARD_URL")
    endpoints = discover_adguard(configured_url)
    adguard = AdGuardClient(base_url=endpoints[0]) if len(endpoints) == 1 else None
    current_ip = None
    adguard_read_succeeded = False
    if adguard:
        try:
            rewrite = adguard.get_rewrite(hostname)
            adguard_read_succeeded = True
            if rewrite:
                current_ip = rewrite.get("answer")
        except AdGuardError as exc:
            logger.warning("Current AdGuard rewrite unavailable host=%s error=%s", hostname, exc)
        finally:
            adguard.close()
    elif len(endpoints) > 1:
        logger.warning("Multiple AdGuard instances found; choose one with 'dro adguard discover'")
    else:
        logger.info("No local AdGuard instance found")
    if not adguard_read_succeeded:
        current_ip = stored_current_ip

    discovery = PublicDnsDiscovery()
    try:
        ips = discovery.discover(hostname)
    except DiscoveryError as exc:
        print(f"Discovery failed: {exc}", file=sys.stderr)
        return 1
    finally:
        discovery.close()
    if current_ip and current_ip not in ips:
        ips.append(current_ip)
    if target.manual_lock_ip and target.manual_lock_ip not in ips:
        ips.append(target.manual_lock_ip)
    if not ips:
        print(f"No public IPv4 A records found for {hostname}", file=sys.stderr)
        return 1
    results = HttpsBenchmarkRunner(target.runs_per_ip, target.timeout_seconds).benchmark(
        hostname, ips, path=target.path, port=target.port)
    current = next((result for result in results if result.ip == current_ip), None)
    decision = DecisionEngine().decide(
        current_ip, current, results, pending=pending,
        switch_threshold_ms=target.switch_threshold_ms,
        switch_threshold_percent=target.switch_threshold_percent,
        required_consecutive_wins=target.required_consecutive_wins,
        manual_lock_ip=target.manual_lock_ip,
        immediate_switch_if_current_unhealthy=target.immediate_switch_if_current_unhealthy,
    )
    next_pending = (PendingCandidateState(candidate_ip=decision.candidate_ip, consecutive_wins=decision.wins)
                    if decision.action in ("HOLD", "UPDATE") else PendingCandidateState())
    summary = {"current_rewrite_ip": current_ip, "current_rewrite_included": bool(current_ip and current_ip in ips),
               "candidate_count": len(results)}
    try:
        with database.session() as session:
            run = save_benchmark_run(session, target_id, results, summary=summary, decision=decision)
            save_optimizer_state(session, target_id, current_ip, next_pending, decision)
            add_audit_event(session, "benchmark_completed", target_id,
                            {**summary, "decision_action": decision.action, "decision_reason": decision.reason})
            run_id = run.id
    except SQLAlchemyError as exc:
        logger.error("Failed to persist benchmark host=%s error=%s", hostname, type(exc).__name__)
        print("Could not persist benchmark results.", file=sys.stderr)
        database.close()
        return 1
    database.close()
    logger.info("Decision host=%s action=%s current_ip=%s candidate_ip=%s reason=%s",
                hostname, decision.action, decision.current_ip, decision.candidate_ip, decision.reason)
    print(json.dumps({"hostname": hostname, "current_ip": current_ip,
                      "current_rewrite_included": current_ip in ips if current_ip else False,
                      "benchmark_run_id": run_id,
                      "candidates": [result.model_dump() for result in results],
                      "decision": decision.model_dump()}, indent=2))
    return 0


def adguard_check_command() -> int:
    """Read-only connection check; reports status and rewrite count without returning credentials."""
    load_secret_environment()
    endpoints = discover_adguard(os.getenv("ADGUARD_URL"))
    if len(endpoints) != 1:
        print(json.dumps({"connected": False, "choices": [safe_endpoint(url) for url in endpoints]}, indent=2))
        return 1
    client = AdGuardClient(base_url=endpoints[0])
    try:
        rewrites = client.list_rewrites()
        print(json.dumps({"connected": True, "endpoint": safe_endpoint(endpoints[0]),
                          "rewrite_count": len(rewrites)}, indent=2))
        return 0
    except AdGuardError as exc:
        logger.warning("AdGuard connection check failed endpoint=%s error=%s", safe_endpoint(endpoints[0]), exc)
        print(json.dumps({"connected": False, "endpoint": safe_endpoint(endpoints[0]), "error": str(exc)}, indent=2))
        return 1
    finally:
        client.close()


def main() -> int:
    logging.basicConfig(level=logging.DEBUG if os.getenv("DRO_DEBUG") else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not os.getenv("DRO_DEBUG"):
        logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="dro", description="DNS Route Optimizer core tools")
    commands = parser.add_subparsers(dest="command", required=True)
    benchmark = commands.add_parser("benchmark", help="read-only HTTPS benchmark")
    benchmark.add_argument("hostname")
    adguard = commands.add_parser("adguard", help="AdGuard endpoint discovery")
    adguard_commands = adguard.add_subparsers(dest="adguard_command", required=True)
    discover = adguard_commands.add_parser("discover", help="discover local AdGuard endpoints on demand")
    discover.add_argument("--subnet", help="manually scan a local IPv4/IPv6 subnet, e.g. 192.168.1.0/24")
    adguard_commands.add_parser("check", help="test configured/detected AdGuard connection (read-only)")
    args = parser.parse_args()
    if args.command == "benchmark":
        return benchmark_command(args.hostname)
    if args.command == "adguard" and args.adguard_command == "discover":
        load_secret_environment()
        endpoints = discover_adguard(os.getenv("ADGUARD_URL"), subnet=args.subnet)
        print(json.dumps({"choices": endpoints}, indent=2))
        return 0 if endpoints else 1
    if args.command == "adguard" and args.adguard_command == "check":
        return adguard_check_command()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
