"""add database indexes for common queries

Revision ID: a1b2c3d4e5f6
Revises: 79ae191b2523
Create Date: 2026-06-20 10:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '79ae191b2523'
branch_labels = None
depends_on = None


def upgrade():
    # Workers
    op.create_index('ix_workers_discord_id', 'workers', ['discord_id'])
    # Tasks
    op.create_index('ix_tasks_worker_id', 'tasks', ['worker_id'])
    # ScoreLogs
    op.create_index('ix_score_logs_worker_id', 'score_logs', ['worker_id'])
    op.create_index('ix_score_logs_created_at', 'score_logs', ['created_at'])
    # CommunityEvents
    op.create_index('ix_community_events_discord_id', 'community_events', ['discord_id'])
    # MessageRecords
    op.create_index('ix_message_records_discord_id', 'message_records', ['discord_id'])
    op.create_index('ix_message_records_guild_id', 'message_records', ['guild_id'])
    op.create_index('ix_message_records_created_at', 'message_records', ['created_at'])
    # GuildInfo
    op.create_index('ix_guild_info_guild_id', 'guild_info', ['guild_id'])
    # GuildRoles
    op.create_index('ix_guild_roles_guild_id', 'guild_roles', ['guild_id'])
    # GuildMembers
    op.create_index('ix_guild_members_guild_id', 'guild_members', ['guild_id'])
    # GuildChannels
    op.create_index('ix_guild_channels_guild_id', 'guild_channels', ['guild_id'])
    # MentionRecords
    op.create_index('ix_mention_records_mentioner_id', 'mention_records', ['mentioner_id'])
    op.create_index('ix_mention_records_mentioned_id', 'mention_records', ['mentioned_id'])
    op.create_index('ix_mention_records_guild_id', 'mention_records', ['guild_id'])
    # VoiceActivity
    op.create_index('ix_voice_activity_guild_id', 'voice_activity', ['guild_id'])
    # PingJoinEvents
    op.create_index('ix_ping_join_events_guild_id', 'ping_join_events', ['guild_id'])
    # BurnoutRisks
    op.create_index('ix_burnout_risks_score', 'burnout_risks', ['score'])


def downgrade():
    op.drop_index('ix_workers_discord_id')
    op.drop_index('ix_tasks_worker_id')
    op.drop_index('ix_score_logs_worker_id')
    op.drop_index('ix_score_logs_created_at')
    op.drop_index('ix_community_events_discord_id')
    op.drop_index('ix_message_records_discord_id')
    op.drop_index('ix_message_records_guild_id')
    op.drop_index('ix_message_records_created_at')
    op.drop_index('ix_guild_info_guild_id')
    op.drop_index('ix_guild_roles_guild_id')
    op.drop_index('ix_guild_members_guild_id')
    op.drop_index('ix_guild_channels_guild_id')
    op.drop_index('ix_mention_records_mentioner_id')
    op.drop_index('ix_mention_records_mentioned_id')
    op.drop_index('ix_mention_records_guild_id')
    op.drop_index('ix_voice_activity_guild_id')
    op.drop_index('ix_ping_join_events_guild_id')
    op.drop_index('ix_burnout_risks_score')
