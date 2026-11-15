"""prune model versions and finalize closed pair months

Revision ID: f3b8c1a9d2e4
Revises: a8f4c2d9e617
Create Date: 2026-11-15 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "f3b8c1a9d2e4"
down_revision = "a8f4c2d9e617"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "model_registry",
        sa.Column("pruned_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "pair_scores_history",
        sa.Column(
            "finalized",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade():
    op.drop_column("pair_scores_history", "finalized")
    op.drop_column("model_registry", "pruned_at")
