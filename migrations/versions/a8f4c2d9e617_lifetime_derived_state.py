"""add durable lifetime derived state

Revision ID: a8f4c2d9e617
Revises: 2e4f6a8b0c1d
Create Date: 2026-11-15 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "a8f4c2d9e617"
down_revision = "2e4f6a8b0c1d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_behavior_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=50), nullable=False),
        sa.Column("discord_id", sa.String(length=50), nullable=False),
        sa.Column("n_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "mean_daily_messages", sa.Float(), nullable=False, server_default="0"
        ),
        sa.Column("m2_daily_messages", sa.Float(), nullable=False, server_default="0"),
        sa.Column("hourly_profile", sa.JSON(), nullable=True),
        sa.Column("ewma_message_length", sa.Float(), nullable=True),
        sa.Column("ewma_off_hours_ratio", sa.Float(), nullable=True),
        sa.Column("lifetime_messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lifetime_pings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "lifetime_voice_hours", sa.Float(), nullable=False, server_default="0"
        ),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_updated_date", sa.Date(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guild_id", "discord_id", name="uq_user_behavior_state_guild_member"
        ),
    )
    op.create_index(
        "ix_user_behavior_state_guild_id", "user_behavior_state", ["guild_id"]
    )
    op.create_index(
        "ix_user_behavior_state_discord_id", "user_behavior_state", ["discord_id"]
    )

    op.create_table(
        "guild_metrics_daily",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=50), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("active_users", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("voice_hours", sa.Float(), nullable=False, server_default="0"),
        sa.Column("joins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("leaves", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guild_id", "date", name="uq_guild_metrics_daily_date"),
    )
    op.create_index("ix_guild_metrics_daily_guild_id", "guild_metrics_daily", ["guild_id"])
    op.create_index("ix_guild_metrics_daily_date", "guild_metrics_daily", ["date"])

    op.create_table(
        "guild_metrics_contributions_daily",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=50), nullable=False),
        sa.Column("user_id", sa.String(length=50), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("voice_hours", sa.Float(), nullable=False, server_default="0"),
        sa.Column("joins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("leaves", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guild_id", "user_id", "date", name="uq_guild_metrics_contrib_day"
        ),
    )
    op.create_index(
        "ix_guild_metrics_contributions_daily_guild_id",
        "guild_metrics_contributions_daily",
        ["guild_id"],
    )
    op.create_index(
        "ix_guild_metrics_contributions_daily_user_id",
        "guild_metrics_contributions_daily",
        ["user_id"],
    )
    op.create_index(
        "ix_guild_metrics_contributions_daily_date",
        "guild_metrics_contributions_daily",
        ["date"],
    )

    op.create_table(
        "pair_scores_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=50), nullable=False),
        sa.Column("pinger_id", sa.String(length=50), nullable=False),
        sa.Column("pingee_id", sa.String(length=50), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("affinity_score", sa.Float(), nullable=True),
        sa.Column("unaddressed_rate", sa.Float(), nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("return_rate", sa.Float(), nullable=True),
        sa.Column("median_response_minutes", sa.Float(), nullable=True),
        sa.Column("conversations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guild_id",
            "pinger_id",
            "pingee_id",
            "month",
            name="uq_pair_scores_history_month",
        ),
    )
    op.create_index("ix_pair_scores_history_guild_id", "pair_scores_history", ["guild_id"])
    op.create_index("ix_pair_scores_history_pinger_id", "pair_scores_history", ["pinger_id"])
    op.create_index("ix_pair_scores_history_pingee_id", "pair_scores_history", ["pingee_id"])
    op.create_index("ix_pair_scores_history_month", "pair_scores_history", ["month"])

    op.create_table(
        "pair_lifetime",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=50), nullable=False),
        sa.Column("pinger_id", sa.String(length=50), nullable=False),
        sa.Column("pingee_id", sa.String(length=50), nullable=False),
        sa.Column(
            "decayed_edge_weight", sa.Float(), nullable=False, server_default="0"
        ),
        sa.Column("lifetime_ping_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_interaction_at", sa.DateTime(), nullable=True),
        sa.Column("last_interaction_at", sa.DateTime(), nullable=True),
        sa.Column("peak_affinity", sa.Float(), nullable=True),
        sa.Column("peak_month", sa.Date(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guild_id", "pinger_id", "pingee_id", name="uq_pair_lifetime_direction"
        ),
    )
    op.create_index("ix_pair_lifetime_guild_id", "pair_lifetime", ["guild_id"])
    op.create_index("ix_pair_lifetime_pinger_id", "pair_lifetime", ["pinger_id"])
    op.create_index("ix_pair_lifetime_pingee_id", "pair_lifetime", ["pingee_id"])

    op.create_table(
        "behavior_metrics_rollup",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=50), nullable=False),
        sa.Column("user_id", sa.String(length=50), nullable=False),
        sa.Column("period_type", sa.String(length=10), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("active_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pings_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("questions_asked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("answers_given", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("voice_hours", sa.Float(), nullable=False, server_default="0"),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guild_id",
            "user_id",
            "period_type",
            "period_start",
            name="uq_behavior_metrics_rollup_period",
        ),
    )
    op.create_index(
        "ix_behavior_metrics_rollup_guild_id", "behavior_metrics_rollup", ["guild_id"]
    )
    op.create_index(
        "ix_behavior_metrics_rollup_user_id", "behavior_metrics_rollup", ["user_id"]
    )
    op.create_index(
        "ix_behavior_metrics_rollup_period_start",
        "behavior_metrics_rollup",
        ["period_start"],
    )

    op.create_table(
        "model_registry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=50), nullable=True),
        sa.Column("trained_at", sa.DateTime(), nullable=False),
        sa.Column("train_start", sa.DateTime(), nullable=True),
        sa.Column("train_end", sa.DateTime(), nullable=True),
        sa.Column("n_samples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "model_name", "version", "guild_id", name="uq_model_registry_version"
        ),
    )
    op.create_index("ix_model_registry_model_name", "model_registry", ["model_name"])
    op.create_index("ix_model_registry_guild_id", "model_registry", ["guild_id"])
    op.create_index("ix_model_registry_is_active", "model_registry", ["is_active"])


def downgrade():
    op.drop_index("ix_model_registry_is_active", table_name="model_registry")
    op.drop_index("ix_model_registry_guild_id", table_name="model_registry")
    op.drop_index("ix_model_registry_model_name", table_name="model_registry")
    op.drop_table("model_registry")

    op.drop_index(
        "ix_behavior_metrics_rollup_period_start", table_name="behavior_metrics_rollup"
    )
    op.drop_index(
        "ix_behavior_metrics_rollup_user_id", table_name="behavior_metrics_rollup"
    )
    op.drop_index(
        "ix_behavior_metrics_rollup_guild_id", table_name="behavior_metrics_rollup"
    )
    op.drop_table("behavior_metrics_rollup")

    op.drop_index("ix_pair_lifetime_pingee_id", table_name="pair_lifetime")
    op.drop_index("ix_pair_lifetime_pinger_id", table_name="pair_lifetime")
    op.drop_index("ix_pair_lifetime_guild_id", table_name="pair_lifetime")
    op.drop_table("pair_lifetime")

    op.drop_index("ix_pair_scores_history_month", table_name="pair_scores_history")
    op.drop_index("ix_pair_scores_history_pingee_id", table_name="pair_scores_history")
    op.drop_index("ix_pair_scores_history_pinger_id", table_name="pair_scores_history")
    op.drop_index("ix_pair_scores_history_guild_id", table_name="pair_scores_history")
    op.drop_table("pair_scores_history")

    op.drop_index(
        "ix_guild_metrics_contributions_daily_date",
        table_name="guild_metrics_contributions_daily",
    )
    op.drop_index(
        "ix_guild_metrics_contributions_daily_user_id",
        table_name="guild_metrics_contributions_daily",
    )
    op.drop_index(
        "ix_guild_metrics_contributions_daily_guild_id",
        table_name="guild_metrics_contributions_daily",
    )
    op.drop_table("guild_metrics_contributions_daily")

    op.drop_index("ix_guild_metrics_daily_date", table_name="guild_metrics_daily")
    op.drop_index("ix_guild_metrics_daily_guild_id", table_name="guild_metrics_daily")
    op.drop_table("guild_metrics_daily")

    op.drop_index("ix_user_behavior_state_discord_id", table_name="user_behavior_state")
    op.drop_index("ix_user_behavior_state_guild_id", table_name="user_behavior_state")
    op.drop_table("user_behavior_state")
