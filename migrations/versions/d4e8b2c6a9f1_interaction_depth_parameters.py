"""interaction depth parameters (pair_scores x5, ping_events.first_response_at)

Revision ID: d4e8b2c6a9f1
Revises: c9d2f4a7b1e8
Create Date: 2026-09-27 20:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4e8b2c6a9f1'
down_revision = 'c9d2f4a7b1e8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'ping_events',
        sa.Column('first_response_at', sa.DateTime(), nullable=True),
    )
    op.add_column(
        'pair_scores',
        sa.Column('initiation_share', sa.Float(), nullable=True),
    )
    op.add_column(
        'pair_scores',
        sa.Column('median_response_minutes', sa.Float(), nullable=True),
    )
    op.add_column(
        'pair_scores',
        sa.Column('max_unaddressed_streak', sa.Integer(), nullable=True),
    )
    op.add_column(
        'pair_scores',
        sa.Column('channels', sa.Integer(), nullable=True),
    )
    op.add_column(
        'pair_scores',
        sa.Column('voice_sessions', sa.Integer(), nullable=True),
    )


def downgrade():
    op.drop_column('pair_scores', 'voice_sessions')
    op.drop_column('pair_scores', 'channels')
    op.drop_column('pair_scores', 'max_unaddressed_streak')
    op.drop_column('pair_scores', 'median_response_minutes')
    op.drop_column('pair_scores', 'initiation_share')
    op.drop_column('ping_events', 'first_response_at')
