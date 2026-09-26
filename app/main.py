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
from app.integrations.adguard import AdGuardClient, AdGuardError, discover_adguard
from app.models.benchmark import BenchmarkResult
from app.security import load_secret_environment, safe_endpoint

logger = logging.getLogger("dro")


def benchmark_command(hostname: str) -> int:
    load_secret_environment()
    configured_url = os.getenv("ADGUARD_URL")
    endpoints = discover_adguard(configured_url)
    adguard = AdGuardClient(base_url=endpoints[0]) if len(endpoints) == 1 else None
    current_ip = None
    if adguard:
        try:
            rewrite = adguard.get_rewrite(hostname)
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
    if not ips:
        print(f"No public IPv4 A records found for {hostname}", file=sys.stderr)
        return 1
    results = HttpsBenchmarkRunner().benchmark(hostname, ips)
    current = next((result for result in results if result.ip == current_ip), None)
    decision = DecisionEngine().decide(current_ip, current, results)
    logger.info("Decision host=%s action=%s current_ip=%s candidate_ip=%s reason=%s",
                hostname, decision.action, decision.current_ip, decision.candidate_ip, decision.reason)
    print(json.dumps({"hostname": hostname, "current_ip": current_ip,
                      "current_rewrite_included": current_ip in ips if current_ip else False,
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
