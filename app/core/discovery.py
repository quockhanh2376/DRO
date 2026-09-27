"""Public DNS-over-HTTPS candidate discovery."""

from __future__ import annotations

import ipaddress
import logging

import httpx

logger = logging.getLogger(__name__)


class DiscoveryError(RuntimeError):
    """Public DNS lookup failed or returned an unusable response."""


class PublicDnsDiscovery:
    def __init__(self, client: httpx.Client | None = None, timeout_seconds: float = 5.0):
        self._client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def discover(self, hostname: str) -> list[str]:
        """Resolve A records via dns.google, following CNAME chains and bypassing local DNS."""
        logger.info("Public DNS discovery started host=%s", hostname)
        pending = [(hostname.rstrip("."), 0)]
        seen_names: set[str] = set()
        addresses: list[str] = []
        seen_ips: set[str] = set()
        while pending:
            name, cname_hops = pending.pop(0)
            name = name.lower()
            if name in seen_names:
                continue
            seen_names.add(name)
            try:
                response = self._client.get("https://dns.google/resolve", params={"name": name, "type": "A"},
                                            headers={"Accept": "application/dns-json"})
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                if addresses:
                    logger.warning("Public DNS lookup failed after finding an IP host=%s name=%s error_type=%s",
                                   hostname, name, type(exc).__name__)
                    continue
                raise DiscoveryError(f"Public DNS lookup failed for {name}; no IP records found") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("Status"), int):
                if addresses:
                    logger.warning("Malformed DNS-over-HTTPS response after finding an IP host=%s name=%s",
                                   hostname, name)
                    continue
                raise DiscoveryError(f"Malformed DNS-over-HTTPS response for {name}: missing integer Status")
            if payload["Status"] != 0:
                if addresses:
                    logger.warning("Public DNS lookup returned an error after finding an IP host=%s name=%s status=%s",
                                   hostname, name, payload["Status"])
                    continue
                raise DiscoveryError(f"Public DNS lookup for {name} returned DNS status {payload['Status']}; no IP records found")
            answers = payload.get("Answer", [])
            if not isinstance(answers, list):
                if addresses:
                    logger.warning("Malformed DNS-over-HTTPS Answer after finding an IP host=%s name=%s",
                                   hostname, name)
                    continue
                raise DiscoveryError(f"Malformed DNS-over-HTTPS response for {name}: Answer must be a list")
            for answer in answers:
                if not isinstance(answer, dict) or not isinstance(answer.get("type"), int):
                    continue
                data = answer.get("data")
                if answer["type"] == 1 and isinstance(data, str):
                    try:
                        ip = ipaddress.ip_address(data)
                    except ValueError:
                        continue
                    if ip.version == 4 and str(ip) not in seen_ips:
                        seen_ips.add(str(ip))
                        addresses.append(str(ip))
                elif answer["type"] == 5 and isinstance(data, str):
                    target = data.rstrip(".").lower()
                    if target and cname_hops < 8 and target not in seen_names and not any(
                            pending_name == target for pending_name, _ in pending):
                        pending.append((target, cname_hops + 1))
                    elif target and cname_hops >= 8:
                        logger.warning("Public DNS CNAME chain exceeded maximum hops host=%s name=%s", hostname, name)
        if not addresses:
            raise DiscoveryError(f"Public DNS discovery for {hostname} returned no IP records")
        logger.info("Public DNS discovery finished host=%s addresses=%d", hostname, len(addresses))
        return addresses
