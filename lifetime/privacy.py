"""Erasure helpers for consent withdrawal."""

from sqlalchemy import or_

from database import (
    BehavioralAnomaly,
    BehaviorMetricDaily,
    BehaviorMetricsRollup,
    GuildMetricsContributionDaily,
    PairLifetime,
    PairScore,
    PairScoresHistory,
    UserBehaviorBaseline,
    UserBehaviorMetric,
    UserBehaviorState,
    db,
)
from lifetime.state import _rebuild_guild_daily


def erase_member_derived_state(guild_id, discord_id):
    """Erase member-keyed derived rows and subtract their guild aggregates."""
    affected_dates = [
        row.date
        for row in GuildMetricsContributionDaily.query.filter_by(
            guild_id=guild_id, user_id=discord_id
        ).with_entities(GuildMetricsContributionDaily.date)
    ]
    erased = {
        "user_behavior_state": UserBehaviorState.query.filter_by(
            guild_id=guild_id, discord_id=discord_id
        ).delete(synchronize_session=False),
        "user_behavior_baselines": UserBehaviorBaseline.query.filter_by(
            guild_id=guild_id, discord_id=discord_id
        ).delete(synchronize_session=False),
        "pair_scores": PairScore.query.filter(
            PairScore.guild_id == guild_id,
            or_(PairScore.pinger_id == discord_id, PairScore.pingee_id == discord_id),
        ).delete(synchronize_session=False),
        "pair_scores_history": PairScoresHistory.query.filter(
            PairScoresHistory.guild_id == guild_id,
            or_(
                PairScoresHistory.pinger_id == discord_id,
                PairScoresHistory.pingee_id == discord_id,
            ),
        ).delete(synchronize_session=False),
        "pair_lifetime": PairLifetime.query.filter(
            PairLifetime.guild_id == guild_id,
            or_(PairLifetime.pinger_id == discord_id, PairLifetime.pingee_id == discord_id),
        ).delete(synchronize_session=False),
        "behavior_metrics_rollup": BehaviorMetricsRollup.query.filter_by(
            guild_id=guild_id, user_id=discord_id
        ).delete(synchronize_session=False),
        "guild_metrics_contributions_daily": GuildMetricsContributionDaily.query.filter_by(
            guild_id=guild_id, user_id=discord_id
        ).delete(synchronize_session=False),
        "behavior_metrics_daily": BehaviorMetricDaily.query.filter_by(
            guild_id=guild_id, user_id=discord_id
        ).delete(synchronize_session=False),
        "user_behavior_metrics": UserBehaviorMetric.query.filter_by(
            guild_id=guild_id, discord_id=discord_id
        ).delete(synchronize_session=False),
        "behavioral_anomalies": BehavioralAnomaly.query.filter_by(
            guild_id=guild_id, discord_id=discord_id
        ).delete(synchronize_session=False),
    }
    db.session.flush()
    rebuilt = 0
    for day in affected_dates:
        rebuilt += int(_rebuild_guild_daily(guild_id, day))
    erased["guild_metrics_daily_rebuilt"] = rebuilt
    db.session.commit()
    return erased
