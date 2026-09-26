"""Allow rewrite history to represent deleting an initial rewrite."""

from alembic import op
import sqlalchemy as sa

revision = "0003_nullable_rewrite_ip"
down_revision = "0002_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("rewrite_history") as batch:
        batch.alter_column("new_ip", existing_type=sa.String(45), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("rewrite_history") as batch:
        batch.alter_column("new_ip", existing_type=sa.String(45), nullable=False)
