"""Safe read-only command-line entry point for DRO."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from app.core.discovery import DiscoveryError
from app.core.optimizer import run_benchmark_cycle
from app.db.database import Database, database_url, sqlite_file_path
from app.db.retention import cleanup_retention
from app.maintenance import backup_database, restore_database
from app.db.repositories import get_target, save_target
from app.integrations.adguard import AdGuardClient, AdGuardError, discover_adguard
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
            output = run_benchmark_cycle(session, record)
    except DiscoveryError as exc:
        logger.error("Benchmark discovery failed host=%s error=%s", hostname, exc)
        print(f"Discovery failed: {exc}", file=sys.stderr)
        database.close()
        return 1
    except SQLAlchemyError as exc:
        logger.error("Database unavailable during benchmark setup host=%s error=%s", hostname, type(exc).__name__)
        print("Database unavailable or not migrated; run 'alembic upgrade head'.", file=sys.stderr)
        database.close()
        return 1
    database.close()
    logger.info("Decision host=%s action=%s current_ip=%s candidate_ip=%s reason=%s",
                hostname, output["decision"]["action"], output["current_ip"],
                output["decision"]["candidate_ip"], output["decision"]["reason"])
    print(json.dumps(output, indent=2))
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


def database_backup_command(destination: str) -> int:
    path = sqlite_file_path(database_url())
    if path is None:
        print("Backup supports SQLite databases only.", file=sys.stderr)
        return 1
    try:
        print(backup_database(path, Path(destination)))
        return 0
    except (OSError, ValueError) as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1


def database_restore_command(source: str, confirmed: bool = False) -> int:
    path = sqlite_file_path(database_url())
    if path is None:
        print("Restore supports SQLite databases only.", file=sys.stderr)
        return 1
    if not confirmed and input("This replaces the live DRO database. Type RESTORE to continue: ") != "RESTORE":
        print("Restore cancelled.", file=sys.stderr)
        return 1
    try:
        restore_database(Path(source), path, confirmed=True)
        print("Restore completed. Restart DRO to reopen the database.")
        return 0
    except (OSError, ValueError, PermissionError) as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 1


def retention_cleanup_command() -> int:
    database = Database()
    try:
        with database.session() as session:
            result = cleanup_retention(session)
        print(json.dumps(result))
        return 0
    finally:
        database.close()


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
    db = commands.add_parser("db", help="SQLite maintenance")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    backup = db_commands.add_parser("backup", help="create a consistent SQLite backup")
    backup.add_argument("destination")
    restore = db_commands.add_parser("restore", help="validate and restore a SQLite backup")
    restore.add_argument("source")
    restore.add_argument("--yes", action="store_true", help="confirm replacement of the live database")
    commands.add_parser("cleanup", help="apply benchmark retention policy")
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
    if args.command == "db" and args.db_command == "backup":
        return database_backup_command(args.destination)
    if args.command == "db" and args.db_command == "restore":
        return database_restore_command(args.source, args.yes)
    if args.command == "cleanup":
        return retention_cleanup_command()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
