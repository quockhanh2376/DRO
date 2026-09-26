"""Allow positive fractional target intervals, such as thirty minutes."""

from alembic import op
import sqlalchemy as sa

revision = "0005_fractional_intervals"
down_revision = "0004_history_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("targets") as batch_op:
        batch_op.alter_column("interval_hours", existing_type=sa.Integer(), type_=sa.Float())


def downgrade() -> None:
    with op.batch_alter_table("targets") as batch_op:
        batch_op.alter_column("interval_hours", existing_type=sa.Float(), type_=sa.Integer())
