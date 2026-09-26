"""Target configuration and validation."""

from __future__ import annotations

import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str
    enabled: bool = True
    protocol: Literal["https"] = "https"
    port: int = Field(default=443, ge=1, le=65535)
    path: str = "/"
    mode: Literal["monitor", "recommend", "auto"] = "auto"
    interval_hours: float = Field(default=2, gt=0)
    runs_per_ip: int = Field(default=10, ge=1)
    timeout_seconds: float = Field(default=10.0, gt=0)
    switch_threshold_ms: float = Field(default=50, ge=0)
    switch_threshold_percent: float = Field(default=5, ge=0, le=100)
    required_consecutive_wins: int = Field(default=2, ge=1)
    immediate_switch_if_current_unhealthy: bool = True
    manual_lock_ip: str | None = None

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, value: str) -> str:
        value = value.strip().rstrip(".").lower()
        if len(value) > 253 or not value:
            raise ValueError("hostname must contain 1 to 253 characters")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            raise ValueError("hostname must be a DNS hostname, not an IP address")
        labels = value.split(".")
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
            raise ValueError("hostname must contain valid DNS labels")
        if len(labels) < 2:
            raise ValueError("hostname must be a fully qualified DNS hostname")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("path must start with '/'")
        return value

    @field_validator("manual_lock_ip")
    @classmethod
    def validate_manual_lock_ip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("manual_lock_ip must be a valid IPv4 address") from exc
        if address.version != 4:
            raise ValueError("manual_lock_ip must be a valid IPv4 address")
        return str(address)
