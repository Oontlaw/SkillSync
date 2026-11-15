"""Erasure helpers for consent withdrawal and member departure."""

import os
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from database import (
    BehavioralAnomaly,
    BehaviorMetricDaily,
    BehaviorMetricsRollup,
    GuildMetricsContributionDaily,
    MemberJoinLeave,
    PairLifetime,
    PairScore,
    PairScoresHistory,
    UserBehaviorBaseline,
    UserBehaviorMetric,
    UserBehaviorState,
    db,
)
from lifetime.state import _rebuild_guild_daily

LEAVE_ERASURE_GRACE_DAYS = int(os.getenv("LEAVE_ERASURE_GRACE_DAYS", "30"))


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


def erase_departed_members(now=None, grace_days=None):
    """Erase derived state for members whose leave grace period has expired.

    Reads each member's latest join/leave event, so a rejoin inside the grace
    window cancels the pending erasure. `grace_days` < 0 disables the policy.
    """
    if grace_days is None:
        grace_days = LEAVE_ERASURE_GRACE_DAYS
    if grace_days < 0:
        return {"erased": 0, "members": [], "policy": "disabled"}
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=grace_days)
    ranked = (
        db.session.query(
            MemberJoinLeave.guild_id,
            MemberJoinLeave.member_id,
            MemberJoinLeave.event_type,
            MemberJoinLeave.created_at,
            func.row_number()
            .over(
                partition_by=(MemberJoinLeave.guild_id, MemberJoinLeave.member_id),
                order_by=(
                    MemberJoinLeave.created_at.desc(),
                    MemberJoinLeave.id.desc(),
                ),
            )
            .label("rn"),
        )
        .filter(MemberJoinLeave.is_bot.is_(False))
        .subquery()
    )
    erased = []
    for guild_id, member_id, event_type, created_at in db.session.query(
        ranked.c.guild_id, ranked.c.member_id, ranked.c.event_type, ranked.c.created_at
    ).filter(ranked.c.rn == 1):
        if event_type != "leave" or created_at is None or created_at > cutoff:
            continue
        # shortcut: erased members are re-scanned each run (deletes are no-ops);
        # add a marker if the departed set grows large
        erase_member_derived_state(guild_id, member_id)
        erased.append({"guild_id": guild_id, "discord_id": member_id})
    return {"erased": len(erased), "members": erased}
