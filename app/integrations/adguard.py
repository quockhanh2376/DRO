"""AdGuard Home DNS rewrite API client."""

from __future__ import annotations

import os
import logging
import ipaddress
from typing import Any

import httpx
from app.security import load_secret_environment, redact_secrets, safe_endpoint

logger = logging.getLogger(__name__)


class AdGuardError(RuntimeError):
    """AdGuard API request failed."""


def discover_adguard(configured_url: str | None = None, subnet: str | None = None,
                     probe: Any = None) -> list[str]:
    """Find AdGuard endpoints without credentials; subnet probing runs only when explicitly requested."""
    probe = probe or _probe_adguard
    if configured_url:
        return [configured_url.rstrip("/")]

    candidates = ["http://127.0.0.1", "http://localhost"]
    found = [url for url in candidates if probe(url)]
    if subnet:
        network = ipaddress.ip_network(subnet, strict=False)
        for address in network.hosts():
            url = f"http://{address}"
            if url not in found and probe(url):
                found.append(url)
    return found


def _probe_adguard(base_url: str) -> bool:
    """Unauthenticated status probe; 401/403 also indicate a protected AdGuard endpoint."""
    try:
        response = httpx.get(f"{base_url}/control/status", timeout=0.5, follow_redirects=False)
    except httpx.HTTPError:
        return False
    return response.status_code in (200, 401, 403)


class AdGuardClient:
    def __init__(self, base_url: str | None = None, user: str | None = None,
                 password: str | None = None, client: httpx.Client | None = None,
                 timeout_seconds: float = 10.0):
        load_secret_environment()
        self.base_url = (base_url or os.getenv("ADGUARD_URL", "")).rstrip("/")
        self.user = user if user is not None else os.getenv("ADGUARD_USER", "")
        self.password = password if password is not None else os.getenv("ADGUARD_PASS", "")
        self._client = client or httpx.Client(timeout=timeout_seconds, auth=(self.user, self.password))
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __repr__(self) -> str:
        return f"AdGuardClient(base_url={safe_endpoint(self.base_url)!r}, user=<redacted>, password=<redacted>)"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.base_url:
            raise AdGuardError("ADGUARD_URL is not configured")
        try:
            response = self._client.request(method, f"{self.base_url}/control/{path.lstrip('/')}", **kwargs)
            response.raise_for_status()
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                logger.error("AdGuard API returned malformed JSON method=%s path=%s", method, path)
                raise AdGuardError(f"AdGuard API {method} {path} returned malformed JSON") from exc
        except httpx.HTTPError as exc:
            # Do not include request headers, client repr, or credentials in error text.
            logger.error("AdGuard API request failed method=%s path=%s error=%s", method,
                         redact_secrets(path), type(exc).__name__)
            raise AdGuardError(f"AdGuard API {method} {path} failed: {type(exc).__name__}") from exc

    def list_rewrites(self) -> list[dict[str, Any]]:
        result = self._request("GET", "rewrite/list")
        if (not isinstance(result, list) or
                any(not isinstance(item, dict) or not isinstance(item.get("domain"), str)
                    or not isinstance(item.get("answer"), str) for item in result)):
            raise AdGuardError("AdGuard API returned an invalid rewrite list")
        return result

    def get_rewrite(self, domain: str) -> dict[str, Any] | None:
        normalized_domain = domain.rstrip(".").casefold()
        matches = [item for item in self.list_rewrites()
                   if item["domain"].rstrip(".").casefold() == normalized_domain]
        if len(matches) > 1:
            raise AdGuardError("AdGuard API returned multiple rewrites for the same domain")
        return matches[0] if matches else None

    def add_rewrite(self, domain: str, answer: str) -> Any:
        return self._request("POST", "rewrite/add", json={"domain": domain, "answer": answer})

    def update_rewrite(self, old_domain: str, old_answer: str, domain: str, answer: str) -> Any:
        result = self._request("PUT", "rewrite/update", json={
            "target": {"domain": old_domain, "answer": old_answer},
            "update": {"domain": domain, "answer": answer, "enabled": True},
        })
        return result

    def delete_rewrite(self, domain: str, answer: str) -> Any:
        return self._request("POST", "rewrite/delete", json={"domain": domain, "answer": answer})


def configured_adguard_client() -> AdGuardClient:
    """Create a client only when runtime configuration identifies one endpoint."""
    load_secret_environment()
    endpoints = discover_adguard(os.getenv("ADGUARD_URL"))
    if len(endpoints) != 1:
        raise AdGuardError("AdGuard endpoint is unavailable or ambiguous")
    return AdGuardClient(base_url=endpoints[0])
