"""Add per-target automatic rewrite opt-in, disabled by default."""

from alembic import op
import sqlalchemy as sa

revision = "0006_auto_apply"
down_revision = "0005_fractional_intervals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("targets") as batch_op:
        batch_op.add_column(sa.Column(
            "auto_apply", sa.Boolean(), server_default=sa.false(), nullable=False,
        ))


def downgrade() -> None:
    with op.batch_alter_table("targets") as batch_op:
        batch_op.drop_column("auto_apply")
