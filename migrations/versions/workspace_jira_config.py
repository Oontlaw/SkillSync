"""Add Jira config columns to organisations

Revision ID: workspace_jira_config
Revises: 6271b49286ae
Create Date: 2026-06-25
"""

import sqlalchemy as sa
from alembic import op

revision = "workspace_jira_config"
down_revision = "member_join_leave_migration"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("organisations", sa.Column("jira_url", sa.String(256), nullable=True))
    op.add_column(
        "organisations", sa.Column("jira_email", sa.String(150), nullable=True)
    )
    op.add_column(
        "organisations", sa.Column("jira_api_token", sa.Text(), nullable=True)
    )
    op.add_column(
        "organisations", sa.Column("jira_project", sa.String(50), nullable=True)
    )


def downgrade():
    op.drop_column("organisations", "jira_project")
    op.drop_column("organisations", "jira_api_token")
    op.drop_column("organisations", "jira_email")
    op.drop_column("organisations", "jira_url")
