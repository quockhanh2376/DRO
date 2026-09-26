"""Benchmark measurements and optimizer decision models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BenchmarkSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ip: str
    run_number: int = Field(ge=1)
    http_status: int | None = None
    connect_ms: float | None = Field(default=None, ge=0)
    tls_ms: float | None = Field(default=None, ge=0)
    total_ms: float | None = Field(default=None, ge=0)
    error: str | None = None

    @property
    def valid(self) -> bool:
        return (self.http_status is not None and 200 <= self.http_status <= 399
                and self.total_ms is not None and self.connect_ms is not None and self.tls_ms is not None)


class BenchmarkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ip: str
    samples: list[BenchmarkSample] = Field(default_factory=list)
    valid_runs: int = 0
    requested_runs: int = 10
    healthy: bool = False
    average_ms: float | None = None
    median_ms: float | None = None
    min_ms: float | None = None
    max_ms: float | None = None
    jitter_ms: float | None = None

    @property
    def avg_ms(self) -> float | None:
        return self.average_ms


class DecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["KEEP", "HOLD", "UPDATE", "FAILOVER", "LOCKED"]
    current_ip: str | None = None
    candidate_ip: str | None = None
    reason: str
    wins: int = 0
    required_wins: int = 2
    improvement_ms: float | None = None
    improvement_percent: float | None = None


class PendingCandidateState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ip: str | None = None
    consecutive_wins: int = 0
