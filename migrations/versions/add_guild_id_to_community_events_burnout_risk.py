"""Add guild_id column to community_events and burnout_risks

Revision ID: add_guild_id_community_burnout
Revises: work_engine_scoring
Create Date: 2026-06-28
"""

import sqlalchemy as sa
from alembic import op

revision = "add_guild_id_community_burnout"
down_revision = "work_engine_scoring"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("community_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("guild_id", sa.String(50), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_community_events_guild_id"), ["guild_id"], unique=False
        )

    with op.batch_alter_table("burnout_risks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("guild_id", sa.String(50), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_burnout_risks_guild_id"), ["guild_id"], unique=False
        )


def downgrade():
    with op.batch_alter_table("burnout_risks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_burnout_risks_guild_id"))
        batch_op.drop_column("guild_id")

    with op.batch_alter_table("community_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_community_events_guild_id"))
        batch_op.drop_column("guild_id")
