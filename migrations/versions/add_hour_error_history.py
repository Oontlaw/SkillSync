"""
Add hour_error_history column to PredictionLog for feedback loop.
"""

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision = "add_hour_error_history"
down_revision = "work_engine_scoring"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "prediction_logs",
        sa.Column("hour_error_history", sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column("prediction_logs", "hour_error_history")
