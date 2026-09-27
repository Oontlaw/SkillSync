"""pair drift columns on pair_scores (recent_pings, prior_pings, last_ping_at)

Revision ID: c9d2f4a7b1e8
Revises: b7e4d1a9c3f2
Create Date: 2026-09-27 19:40:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c9d2f4a7b1e8'
down_revision = 'b7e4d1a9c3f2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'pair_scores',
        sa.Column('recent_pings', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'pair_scores',
        sa.Column('prior_pings', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'pair_scores',
        sa.Column('last_ping_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_column('pair_scores', 'last_ping_at')
    op.drop_column('pair_scores', 'prior_pings')
    op.drop_column('pair_scores', 'recent_pings')
