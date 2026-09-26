"""Add admin credentials, schedule state, and automatic rewrite tracking."""

from alembic import op
import sqlalchemy as sa

revision = "0002_hardening"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rewrite_history", sa.Column("automatic", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("rewrite_history") as batch:
        batch.alter_column("automatic", existing_type=sa.Boolean(), server_default=None)
    op.create_table(
        "admin_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "schedule_state",
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("targets.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("schedule_state")
    op.drop_table("admin_credentials")
    op.drop_column("rewrite_history", "automatic")
