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
        pending = [hostname.rstrip(".")]
        seen_names: set[str] = set()
        addresses: list[str] = []
        seen_ips: set[str] = set()
        while pending:
            name = pending.pop(0).lower()
            if name in seen_names:
                continue
            seen_names.add(name)
            try:
                response = self._client.get("https://dns.google/resolve", params={"name": name, "type": "A"},
                                            headers={"Accept": "application/dns-json"})
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise DiscoveryError(f"Public DNS lookup failed for {name}: {exc}") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("Status"), int):
                raise DiscoveryError(f"Malformed DNS-over-HTTPS response for {name}: missing integer Status")
            if payload["Status"] != 0:
                raise DiscoveryError(f"Public DNS lookup for {name} returned DNS status {payload['Status']}")
            answers = payload.get("Answer", [])
            if not isinstance(answers, list):
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
                    if target and target not in seen_names and target not in pending:
                        pending.append(target)
        logger.info("Public DNS discovery finished host=%s addresses=%d", hostname, len(addresses))
        return addresses
