"""behavior patterns: pair conversations/return_rate + user_behavior_metrics

Revision ID: e5f1a8b3c7d2
Revises: d4e8b2c6a9f1
Create Date: 2026-09-28 09:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f1a8b3c7d2'
down_revision = 'd4e8b2c6a9f1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'pair_scores',
        sa.Column('conversations', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'pair_scores',
        sa.Column('return_rate', sa.Float(), nullable=True),
    )
    op.create_table(
        'user_behavior_metrics',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('guild_id', sa.String(length=50), nullable=False),
        sa.Column('discord_id', sa.String(length=50), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=True),
        sa.Column('recent_messages', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('prior_messages', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('trend', sa.String(length=10), nullable=True),
        sa.Column('rhythm_shift', sa.Float(), nullable=True),
        sa.Column('voice_hours_30d', sa.Float(), nullable=False, server_default='0'),
        sa.Column('active_days_30d', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('week1_pings_received', sa.Integer(), nullable=True),
        sa.Column('absorbed_by', sa.String(length=100), nullable=True),
        sa.Column('broadcast_count_30d', sa.Integer(), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(), nullable=True),
        sa.Column('computed_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('guild_id', 'discord_id', name='uq_user_behavior_guild_member'),
    )
    op.create_index('ix_user_behavior_metrics_guild_id', 'user_behavior_metrics', ['guild_id'])
    op.create_index('ix_user_behavior_metrics_discord_id', 'user_behavior_metrics', ['discord_id'])


def downgrade():
    op.drop_table('user_behavior_metrics')
    op.drop_column('pair_scores', 'return_rate')
    op.drop_column('pair_scores', 'conversations')
