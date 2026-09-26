"""Small helpers for loading runtime secrets and redacting sensitive text."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SECRET_ENV_FILE = Path("/etc/dro/dro.env")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:basic|bearer)?\s*[^\s,;]+"),
    re.compile(r"(?i)((?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+"),
)


def load_secret_environment(path: Path = SECRET_ENV_FILE) -> None:
    """Load KEY=VALUE entries without overriding process environment settings."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and key.replace("_", "").isalnum():
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


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
