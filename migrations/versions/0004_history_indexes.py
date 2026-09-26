"""Add composite indexes for target history and latest-run queries."""

from alembic import op

revision = "0004_history_indexes"
down_revision = "0003_nullable_rewrite_ip"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_benchmark_runs_target_completed", "benchmark_runs", ["target_id", "completed_at"])
    op.create_index("ix_rewrite_history_target_created", "rewrite_history", ["target_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_rewrite_history_target_created", table_name="rewrite_history")
    op.drop_index("ix_benchmark_runs_target_completed", table_name="benchmark_runs")
