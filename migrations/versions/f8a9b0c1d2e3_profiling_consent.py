"""profiling consent: guild_members consent columns

Revision ID: f8a9b0c1d2e3
Revises: e5f1a8b3c7d2
Create Date: 2026-10-02 13:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f8a9b0c1d2e3'
down_revision = 'e5f1a8b3c7d2'
branch_labels = None
depends_on = None


def upgrade():
    # opt-out model: existing members keep their graphs (NULL/absent = opted in)
    op.add_column(
        'guild_members',
        sa.Column('consent_optin', sa.Boolean(), nullable=True, server_default=sa.text('1')),
    )
    op.add_column(
        'guild_members',
        sa.Column('consent_updated_at', sa.DateTime(), nullable=True),
    )
    op.add_column(
        'guild_members',
        sa.Column('consent_source', sa.String(length=50), nullable=True),
    )


def downgrade():
    op.drop_column('guild_members', 'consent_source')
    op.drop_column('guild_members', 'consent_updated_at')
    op.drop_column('guild_members', 'consent_optin')
