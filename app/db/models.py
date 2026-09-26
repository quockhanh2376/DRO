"""SQLAlchemy schema for targets, benchmarks, optimizer state, and audit history."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TargetRecord(Base):
    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(253), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    protocol: Mapped[str] = mapped_column(String(10), default="https")
    port: Mapped[int] = mapped_column(Integer, default=443)
    path: Mapped[str] = mapped_column(String(2048), default="/")
    mode: Mapped[str] = mapped_column(String(20), default="auto")
    interval_hours: Mapped[int] = mapped_column(Integer, default=2)
    runs_per_ip: Mapped[int] = mapped_column(Integer, default=10)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=10.0)
    switch_threshold_ms: Mapped[float] = mapped_column(Float, default=50.0)
    switch_threshold_percent: Mapped[float] = mapped_column(Float, default=5.0)
    required_consecutive_wins: Mapped[int] = mapped_column(Integer, default=2)
    immediate_switch_if_current_unhealthy: Mapped[bool] = mapped_column(Boolean, default=True)
    manual_lock_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    benchmark_runs: Mapped[list["BenchmarkRunRecord"]] = relationship(back_populates="target", passive_deletes=True)
    optimizer_state: Mapped["OptimizerStateRecord | None"] = relationship(
        back_populates="target", uselist=False, passive_deletes=True)
    rewrite_history: Mapped[list["RewriteHistoryRecord"]] = relationship(back_populates="target", passive_deletes=True)


class BenchmarkRunRecord(Base):
    __tablename__ = "benchmark_runs"
    __table_args__ = (Index("ix_benchmark_runs_target_completed", "target_id", "completed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    decision_action: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    target: Mapped[TargetRecord] = relationship(back_populates="benchmark_runs")
    results: Mapped[list["BenchmarkResultRecord"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class BenchmarkResultRecord(Base):
    __tablename__ = "benchmark_results"
    __table_args__ = (UniqueConstraint("run_id", "ip", name="uq_benchmark_result_run_ip"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("benchmark_runs.id", ondelete="CASCADE"), index=True)
    ip: Mapped[str] = mapped_column(String(45))
    valid_runs: Mapped[int] = mapped_column(Integer)
    requested_runs: Mapped[int] = mapped_column(Integer)
    healthy: Mapped[bool] = mapped_column(Boolean)
    average_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    jitter_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    run: Mapped[BenchmarkRunRecord] = relationship(back_populates="results")
    samples: Mapped[list["BenchmarkSampleRecord"]] = relationship(back_populates="result", cascade="all, delete-orphan")


class BenchmarkSampleRecord(Base):
    __tablename__ = "benchmark_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    result_id: Mapped[int] = mapped_column(ForeignKey("benchmark_results.id", ondelete="CASCADE"), index=True)
    run_number: Mapped[int] = mapped_column(Integer)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    connect_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    tls_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    result: Mapped[BenchmarkResultRecord] = relationship(back_populates="samples")


class OptimizerStateRecord(Base):
    __tablename__ = "optimizer_state"

    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), primary_key=True)
    current_rewrite_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    pending_candidate_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    consecutive_wins: Mapped[int] = mapped_column(Integer, default=0)
    last_decision_action: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    target: Mapped[TargetRecord] = relationship(back_populates="optimizer_state")


class RewriteHistoryRecord(Base):
    __tablename__ = "rewrite_history"
    __table_args__ = (Index("ix_rewrite_history_target_created", "target_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("targets.id", ondelete="SET NULL"), nullable=True, index=True)
    old_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    new_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    benchmark_run_id: Mapped[int | None] = mapped_column(ForeignKey("benchmark_runs.id", ondelete="SET NULL"), nullable=True)
    automatic: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    target: Mapped[TargetRecord] = relationship(back_populates="rewrite_history")


class SettingRecord(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class AuditLogRecord(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("targets.id", ondelete="SET NULL"), nullable=True, index=True)
    event: Mapped[str] = mapped_column(String(100), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class AdminCredentialRecord(Base):
    __tablename__ = "admin_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    username: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ScheduleStateRecord(Base):
    __tablename__ = "schedule_state"

    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), primary_key=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
