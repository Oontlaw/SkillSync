"""pairwise interaction tables (ping_events, pair_scores, message_refs)

Revision ID: b7e4d1a9c3f2
Revises: ffa4e3e42f92
Create Date: 2026-09-27 16:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e4d1a9c3f2'
down_revision = 'ffa4e3e42f92'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ping_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('guild_id', sa.String(length=50), nullable=False),
        sa.Column('pinger_id', sa.String(length=50), nullable=False),
        sa.Column('pinger_name', sa.String(length=100), nullable=True),
        sa.Column('pingee_id', sa.String(length=50), nullable=False),
        sa.Column('pingee_name', sa.String(length=100), nullable=True),
        sa.Column('channel_id', sa.String(length=50), nullable=False),
        sa.Column('channel_name', sa.String(length=100), nullable=True),
        sa.Column('message_id', sa.String(length=50), nullable=False),
        sa.Column('ping_type', sa.String(length=20), nullable=False, server_default='mention'),
        sa.Column('requires_response', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('addressed', sa.Boolean(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('return_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('message_id', 'pingee_id', name='uq_ping_events_message_pingee'),
    )
    op.create_index('ix_ping_events_guild_id', 'ping_events', ['guild_id'])
    op.create_index('ix_ping_events_pinger_id', 'ping_events', ['pinger_id'])
    op.create_index('ix_ping_events_pingee_id', 'ping_events', ['pingee_id'])
    op.create_index('ix_ping_events_created_at', 'ping_events', ['created_at'])

    op.create_table(
        'pair_scores',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('guild_id', sa.String(length=50), nullable=False),
        sa.Column('pinger_id', sa.String(length=50), nullable=False),
        sa.Column('pinger_name', sa.String(length=100), nullable=True),
        sa.Column('pingee_id', sa.String(length=50), nullable=False),
        sa.Column('pingee_name', sa.String(length=100), nullable=True),
        sa.Column('affinity_score', sa.Float(), nullable=True),
        sa.Column('unaddressed_rate', sa.Float(), nullable=True),
        sa.Column('baseline_unaddressed', sa.Float(), nullable=True),
        sa.Column('sample_size', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_computed_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('guild_id', 'pinger_id', 'pingee_id', name='uq_pair_scores_guild_pair'),
    )
    op.create_index('ix_pair_scores_guild_id', 'pair_scores', ['guild_id'])
    op.create_index('ix_pair_scores_pinger_id', 'pair_scores', ['pinger_id'])
    op.create_index('ix_pair_scores_pingee_id', 'pair_scores', ['pingee_id'])

    op.create_table(
        'message_refs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('guild_id', sa.String(length=50), nullable=False),
        sa.Column('channel_id', sa.String(length=50), nullable=False),
        sa.Column('message_id', sa.String(length=50), nullable=False),
        sa.Column('author_id', sa.String(length=50), nullable=False),
        sa.Column('reply_to_message_id', sa.String(length=50), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_message_refs_guild_id', 'message_refs', ['guild_id'])
    op.create_index('ix_message_refs_channel_id', 'message_refs', ['channel_id'])
    op.create_index('ix_message_refs_message_id', 'message_refs', ['message_id'])
    op.create_index('ix_message_refs_author_id', 'message_refs', ['author_id'])
    op.create_index('ix_message_refs_created_at', 'message_refs', ['created_at'])


def downgrade():
    op.drop_table('message_refs')
    op.drop_table('pair_scores')
    op.drop_table('ping_events')
