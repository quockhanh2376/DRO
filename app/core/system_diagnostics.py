"""Read-only operational diagnostics for the authenticated Dashboard."""

from __future__ import annotations

import logging
import ssl
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
DNS_PROBE_HOST = "example.com"
INTERNET_PROBE_IP = "1.1.1.1"
PROBE_TIMEOUT_SECONDS = 1.25
MAX_TARGET_WORKERS = 8
MAX_TARGET_ENDPOINTS = 3


def _resolve(hostname: str, port: int = 443) -> list[str]:
    return list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(
        hostname, port, type=socket.SOCK_STREAM)))


def _connect(address: str, port: int = 443) -> bool:
    try:
        with socket.create_connection((address, port), timeout=PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _connect_https(address: str, hostname: str = "cloudflare-dns.com") -> bool:
    """Verify an outbound TLS handshake without disabling certificate/SNI checks."""
    try:
        context = ssl.create_default_context()
        with socket.create_connection((address, 443), timeout=PROBE_TIMEOUT_SECONDS) as connection:
            with context.wrap_socket(connection, server_hostname=hostname):
                return True
    except (OSError, ssl.SSLError):
        return False


def _status_rows(targets, rewrite_map, rewrite_read_ok: bool) -> list[dict]:
    enabled = [target for target in targets if target.enabled]
    if not enabled:
        return []

    def probe(target):
        hostname = target.hostname.rstrip(".").casefold()
        try:
            addresses = _resolve(target.hostname, target.port)
        except (OSError, ValueError):
            addresses = []
        rewrite_ip = rewrite_map.get(hostname)
        endpoints = list(dict.fromkeys(([rewrite_ip] if rewrite_ip else []) + addresses))[:MAX_TARGET_ENDPOINTS]
        reachable_ip = next((ip for ip in endpoints if _connect(ip, target.port)), None)
        rewrite_text = (f"rewrite {rewrite_ip} readable" if rewrite_ip else
                        "no AdGuard rewrite" if rewrite_read_ok else "rewrite read unavailable")
        details = []
        if addresses:
            details.append(f"DNS resolved ({len(addresses)} address{'es' if len(addresses) != 1 else ''})")
        else:
            details.append("DNS resolution failed")
        details.append(rewrite_text)
        if reachable_ip:
            details.append(f"endpoint reachable at {reachable_ip}:{target.port}")
        else:
            details.append("no endpoint reachable")
        if not addresses:
            status = "failed"
        elif not reachable_ip or not rewrite_read_ok:
            status = "warning"
        else:
            status = "healthy"
        return {"hostname": target.hostname, "status": status, "detail": "; ".join(details)}

    rows = []
    with ThreadPoolExecutor(max_workers=min(MAX_TARGET_WORKERS, len(enabled))) as pool:
        futures = {pool.submit(probe, target): target for target in enabled}
        for future in as_completed(futures):
            target = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:  # each target failure is isolated
                logger.info("diagnostics_target_failed hostname=%s error=%s",
                            target.hostname, type(exc).__name__)
                rows.append({"hostname": target.hostname, "status": "failed",
                             "detail": "Target diagnostic could not complete"})
    rows.sort(key=lambda row: row["hostname"].casefold())
    return rows


def run_diagnostics(*, targets, adguard_client_factory, scheduler_enabled: bool,
                    scheduler_running: bool, queue_states: list[tuple[str | None, int | None]],
                    https_reached: bool) -> dict:
    """Collect operational checks without changing DRO, scheduler, benchmark, or DNS state."""
    checks = [{"name": "DRO Backend", "status": "healthy", "detail": "Dashboard backend responding"}]

    rewrite_map: dict[str, str] = {}
    adguard_ok = False
    rewrite_ok = False
    rewrite_count = 0
    client = None
    try:
        client = adguard_client_factory()
        rewrites = client.list_rewrites()
        adguard_ok = True
        rewrite_ok = True
        rewrite_count = len(rewrites)
        rewrite_map = {str(item["domain"]).rstrip(".").casefold(): str(item["answer"])
                       for item in rewrites
                       if isinstance(item, dict) and isinstance(item.get("domain"), str)
                       and isinstance(item.get("answer"), str)}
        checks.append({"name": "AdGuard API", "status": "healthy", "detail": "API reachable"})
        checks.append({"name": "AdGuard Rewrite Read", "status": "healthy",
                       "detail": f"{rewrite_count} rewrite{'s' if rewrite_count != 1 else ''} readable"})
    except Exception as exc:
        logger.info("diagnostics_adguard_failed error=%s", type(exc).__name__)
        checks.extend([
            {"name": "AdGuard API", "status": "failed", "detail": "AdGuard API unavailable"},
            {"name": "AdGuard Rewrite Read", "status": "failed", "detail": "Rewrite list could not be read"},
        ])
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    checks.append({"name": "Nginx / HTTPS", "status": "healthy" if https_reached else "warning",
                   "detail": "Dashboard request reached DRO over HTTPS" if https_reached else
                   "Dashboard request is not marked HTTPS"})
    try:
        dns_addresses = _resolve(DNS_PROBE_HOST)
        checks.append({"name": "DNS Resolution", "status": "healthy" if dns_addresses else "failed",
                       "detail": "System DNS resolution working" if dns_addresses else "No DNS answers"})
    except Exception as exc:
        logger.info("diagnostics_dns_failed error=%s", type(exc).__name__)
        checks.append({"name": "DNS Resolution", "status": "failed", "detail": "System DNS resolution failed"})

    internet_ok = _connect_https(INTERNET_PROBE_IP)
    checks.append({"name": "Internet Connectivity", "status": "healthy" if internet_ok else "failed",
                   "detail": "External HTTPS endpoint reachable" if internet_ok else
                   "External HTTPS endpoint unreachable"})
    scheduler_ok = scheduler_enabled and scheduler_running
    checks.append({"name": "Scheduler", "status": "healthy" if scheduler_ok else "warning",
                   "detail": "Enabled and running" if scheduler_ok else
                   "Disabled" if not scheduler_enabled else "Enabled but worker not running"})
    active_count = sum(1 for state, _position in queue_states if state == "running")
    queued_count = sum(1 for state, _position in queue_states if state == "queued")
    checks.append({"name": "Queue Coordinator", "status": "healthy",
                   "detail": f"{active_count} active / {queued_count} queued"})

    target_rows = _status_rows(targets, rewrite_map, rewrite_ok)
    healthy_targets = sum(row["status"] == "healthy" for row in target_rows)
    total_targets = len(target_rows)
    if total_targets == 0:
        targets_status, targets_detail = "warning", "No enabled targets"
    elif healthy_targets == total_targets:
        targets_status, targets_detail = "healthy", f"{healthy_targets}/{total_targets} reachable"
    elif healthy_targets == 0:
        targets_status, targets_detail = "failed", f"0/{total_targets} reachable"
    else:
        targets_status, targets_detail = "warning", f"{healthy_targets}/{total_targets} reachable"
    checks.append({"name": "Enabled Targets", "status": targets_status, "detail": targets_detail})

    statuses = {row["status"] for row in checks}
    overall = "failed" if "failed" in statuses else "warning" if "warning" in statuses else "healthy"
    return {"overall": overall, "last_run": datetime.now(timezone.utc), "checks": checks,
            "targets": target_rows, "adguard_api_ok": adguard_ok,
            "adguard_rewrite_read_ok": rewrite_ok}
