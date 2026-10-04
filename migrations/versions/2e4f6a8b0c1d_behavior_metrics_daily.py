"""behavior metrics daily rollup

Revision ID: 2e4f6a8b0c1d
Revises: f8a9b0c1d2e3
Create Date: 2026-10-04 15:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2e4f6a8b0c1d'
down_revision = 'f8a9b0c1d2e3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'behavior_metrics_daily',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('guild_id', sa.String(length=50), nullable=False),
        sa.Column('user_id', sa.String(length=50), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('messages', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('pings_sent', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('questions_asked', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('answers_given', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('latency_p50_minutes', sa.Float(), nullable=True),
        sa.Column('latency_p90_minutes', sa.Float(), nullable=True),
        sa.Column('streak_days', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cadence_cv', sa.Float(), nullable=True),
        sa.Column('channel_diversity', sa.Float(), nullable=True),
        sa.Column('voice_hours', sa.Float(), nullable=False, server_default='0'),
        sa.Column('extra', sa.Text(), nullable=True),
        sa.Column('computed_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('guild_id', 'user_id', 'date', name='uq_behavior_daily_guild_user_date'),
    )
    op.create_index('ix_behavior_metrics_daily_guild_id', 'behavior_metrics_daily', ['guild_id'])
    op.create_index('ix_behavior_metrics_daily_user_id', 'behavior_metrics_daily', ['user_id'])
    op.create_index('ix_behavior_metrics_daily_date', 'behavior_metrics_daily', ['date'])


def downgrade():
    op.drop_index('ix_behavior_metrics_daily_date', table_name='behavior_metrics_daily')
    op.drop_index('ix_behavior_metrics_daily_user_id', table_name='behavior_metrics_daily')
    op.drop_index('ix_behavior_metrics_daily_guild_id', table_name='behavior_metrics_daily')
    op.drop_table('behavior_metrics_daily')
