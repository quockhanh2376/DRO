"""Read-only network diagnostics for selected target addresses."""

from __future__ import annotations

import ipaddress
import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class PingResult:
    ip: str
    packet_loss_percent: float | None
    min_ms: float | None
    avg_ms: float | None
    max_ms: float | None
    mdev_ms: float | None
    status: str


def ping_ipv4(ip: str) -> PingResult:
    """Ping one validated IPv4 four times and parse standard Linux iputils output."""
    address = ipaddress.ip_address(ip)
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError("Ping requires an IPv4 address")

    try:
        completed = subprocess.run(
            ["ping", "-c", "4", "-W", "1", str(address)],
            capture_output=True, text=True, check=False, timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return PingResult(str(address), None, None, None, None, None, "Failed")

    output = completed.stdout + "\n" + completed.stderr
    loss_match = re.search(r"([\d.]+)%\s*packet loss", output)
    rtt_match = re.search(
        r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*"
        r"([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", output,
    )
    loss = float(loss_match.group(1)) if loss_match else None
    metrics = tuple(float(rtt_match.group(index)) for index in range(1, 5)) if rtt_match else (None,) * 4
    status = "Failed" if loss is None or loss >= 100 or completed.returncode != 0 and loss == 0 else (
        "Warning" if loss > 0 else "OK")
    return PingResult(str(address), loss, *metrics, status)
