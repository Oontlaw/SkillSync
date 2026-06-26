"""Add work engine scoring columns to tasks and score_logs

Revision ID: work_engine_scoring
Revises: workspace_jira_config
Create Date: 2026-06-26
"""

import sqlalchemy as sa
from alembic import op

revision = "work_engine_scoring"
down_revision = "workspace_jira_config"
branch_labels = None
depends_on = None


def upgrade():
    # Tasks — completion quality flags
    op.add_column(
        "tasks",
        sa.Column(
            "is_early", sa.Boolean(), nullable=True, server_default=sa.text("FALSE")
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "is_late", sa.Boolean(), nullable=True, server_default=sa.text("FALSE")
        ),
    )
    op.add_column(
        "tasks", sa.Column("priority_at_completion", sa.String(20), nullable=True)
    )

    # ScoreLogs — work engine context
    op.add_column("score_logs", sa.Column("task_id", sa.Integer(), nullable=True))
    op.create_index("ix_score_logs_task_id", "score_logs", ["task_id"])
    op.add_column(
        "score_logs",
        sa.Column(
            "priority_multiplier",
            sa.Float(),
            nullable=True,
            server_default=sa.text("1.0"),
        ),
    )


def downgrade():
    op.drop_column("score_logs", "priority_multiplier")
    op.drop_index("ix_score_logs_task_id", table_name="score_logs")
    op.drop_column("score_logs", "task_id")
    op.drop_column("tasks", "priority_at_completion")
    op.drop_column("tasks", "is_late")
    op.drop_column("tasks", "is_early")
