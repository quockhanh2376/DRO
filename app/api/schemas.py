"""Request and response schemas for the v1 API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.models.target import Target


class TargetCreate(Target):
    pass


class TargetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str | None = None
    enabled: bool | None = None
    protocol: str | None = None
    port: int | None = None
    path: str | None = None
    mode: str | None = None
    interval_hours: int | None = None
    runs_per_ip: int | None = None
    timeout_seconds: float | None = None
    switch_threshold_ms: float | None = None
    switch_threshold_percent: float | None = None
    required_consecutive_wins: int | None = None
    immediate_switch_if_current_unhealthy: bool | None = None
    manual_lock_ip: str | None = None


class TargetRead(Target):
    id: int
