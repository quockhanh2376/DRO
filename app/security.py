"""Small helpers for loading runtime secrets and redacting sensitive text."""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SECRET_ENV_FILE = Path("/etc/dro/dro.env")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:basic|bearer)?\s*[^\s,;]+"),
    re.compile(r"(?i)((?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+"),
)
_SECRET_KEYS = re.compile(
    r"(?i)(?:^|[_.-])(?:password|passwd|pass|secret|token|authorization|api[_-]?key)(?:$|[_.-])"
)

_CACHED_ENV_SNAPSHOT: tuple[tuple[str, str], ...] | None = None
_CACHED_SECRETS: list[str] = []


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip())
        return True
    except ValueError:
        return False


def reset_secret_cache() -> None:
    """Clear the cached environment secret values."""
    global _CACHED_ENV_SNAPSHOT, _CACHED_SECRETS
    _CACHED_ENV_SNAPSHOT = None
    _CACHED_SECRETS = []


def get_secret_values() -> list[str]:
    """Return precomputed list of distinct secret values from environment variables."""
    global _CACHED_ENV_SNAPSHOT, _CACHED_SECRETS
    current = tuple((k, os.environ[k]) for k in sorted(os.environ.keys()) if _SECRET_KEYS.search(k))
    if current == _CACHED_ENV_SNAPSHOT:
        return _CACHED_SECRETS
    secrets_set: set[str] = set()
    for _key, val in current:
        if val and len(val) >= 4 and not _is_ip(val):
            secrets_set.add(val)
    _CACHED_SECRETS = sorted(secrets_set, key=len, reverse=True)
    _CACHED_ENV_SNAPSHOT = current
    return _CACHED_SECRETS


def load_secret_environment(path: Path = SECRET_ENV_FILE) -> None:
    """Load KEY=VALUE entries without overriding process environment settings."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, PermissionError):
        # systemd reads a root-only EnvironmentFile before dropping privileges.
        return
    modified = False
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and key.replace("_", "").isalnum():
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key not in os.environ:
                os.environ[key] = value
                modified = True
    if modified:
        reset_secret_cache()


def redact_secrets(value: object) -> str:
    """Redact common credential fields and authorization values from diagnostic text."""
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text


def safe_endpoint(value: str) -> str:
    """Return an endpoint without URL userinfo, query parameters, or fragment."""
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return redact_secrets(value)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def sanitize_for_storage(value, secrets: list[str] | None = None):
    """Remove credential fields and redact credential-like values before persistence."""
    if secrets is None:
        secrets = get_secret_values()
    if isinstance(value, dict):
        return {key: sanitize_for_storage(item, secrets) for key, item in value.items()
                if not _SECRET_KEYS.search(str(key))}
    if isinstance(value, (list, tuple)):
        return [sanitize_for_storage(item, secrets) for item in value]
    if isinstance(value, str):
        text = redact_secrets(value)
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        return text
    return value


def is_secret_key(value: str) -> bool:
    return bool(_SECRET_KEYS.search(value))
