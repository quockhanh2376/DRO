"""Initial DRO persistence schema."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hostname", sa.String(253), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("protocol", sa.String(10), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(2048), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("interval_hours", sa.Integer(), nullable=False),
        sa.Column("runs_per_ip", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("switch_threshold_ms", sa.Float(), nullable=False),
        sa.Column("switch_threshold_percent", sa.Float(), nullable=False),
        sa.Column("required_consecutive_wins", sa.Integer(), nullable=False),
        sa.Column("immediate_switch_if_current_unhealthy", sa.Boolean(), nullable=False),
        sa.Column("manual_lock_ip", sa.String(45), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("hostname", name="uq_targets_hostname"),
    )
    op.create_table(
        "benchmark_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("decision_action", sa.String(20), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_benchmark_runs_target_id", "benchmark_runs", ["target_id"])
    op.create_index("ix_benchmark_runs_completed_at", "benchmark_runs", ["completed_at"])
    op.create_table(
        "benchmark_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("benchmark_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("valid_runs", sa.Integer(), nullable=False),
        sa.Column("requested_runs", sa.Integer(), nullable=False),
        sa.Column("healthy", sa.Boolean(), nullable=False),
        sa.Column("average_ms", sa.Float(), nullable=True),
        sa.Column("median_ms", sa.Float(), nullable=True),
        sa.Column("min_ms", sa.Float(), nullable=True),
        sa.Column("max_ms", sa.Float(), nullable=True),
        sa.Column("jitter_ms", sa.Float(), nullable=True),
        sa.UniqueConstraint("run_id", "ip", name="uq_benchmark_result_run_ip"),
    )
    op.create_index("ix_benchmark_results_run_id", "benchmark_results", ["run_id"])
    op.create_table(
        "benchmark_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("result_id", sa.Integer(), sa.ForeignKey("benchmark_results.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("connect_ms", sa.Float(), nullable=True),
        sa.Column("tls_ms", sa.Float(), nullable=True),
        sa.Column("total_ms", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_benchmark_samples_result_id", "benchmark_samples", ["result_id"])
    op.create_index("ix_benchmark_samples_created_at", "benchmark_samples", ["created_at"])
    op.create_table(
        "optimizer_state",
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("current_rewrite_ip", sa.String(45), nullable=True),
        sa.Column("pending_candidate_ip", sa.String(45), nullable=True),
        sa.Column("consecutive_wins", sa.Integer(), nullable=False),
        sa.Column("last_decision_action", sa.String(20), nullable=True),
        sa.Column("last_decision_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "rewrite_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("old_ip", sa.String(45), nullable=True),
        sa.Column("new_ip", sa.String(45), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("benchmark_run_id", sa.Integer(), sa.ForeignKey("benchmark_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_rewrite_history_target_id", "rewrite_history", ["target_id"])
    op.create_index("ix_rewrite_history_created_at", "rewrite_history", ["created_at"])
    op.create_table(
        "settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event", sa.String(100), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_log_target_id", "audit_log", ["target_id"])
    op.create_index("ix_audit_log_event", "audit_log", ["event"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_event", table_name="audit_log")
    op.drop_index("ix_audit_log_target_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("settings")
    op.drop_index("ix_rewrite_history_created_at", table_name="rewrite_history")
    op.drop_index("ix_rewrite_history_target_id", table_name="rewrite_history")
    op.drop_table("rewrite_history")
    op.drop_table("optimizer_state")
    op.drop_index("ix_benchmark_samples_created_at", table_name="benchmark_samples")
    op.drop_index("ix_benchmark_samples_result_id", table_name="benchmark_samples")
    op.drop_table("benchmark_samples")
    op.drop_index("ix_benchmark_results_run_id", table_name="benchmark_results")
    op.drop_table("benchmark_results")
    op.drop_index("ix_benchmark_runs_completed_at", table_name="benchmark_runs")
    op.drop_index("ix_benchmark_runs_target_id", table_name="benchmark_runs")
    op.drop_table("benchmark_runs")
    op.drop_table("targets")
