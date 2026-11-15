"""Incremental, consent-filtered lifetime state and durable metric rollups.

`UserBehaviorState.n_days` and `mean_daily_messages` are computed over ACTIVE
days only (a day is folded only when source rows exist for that member). They
are therefore not calendar-day averages; read inactivity from
`last_updated_date` (see `days_since_active`), never from a low mean.
"""

import logging
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func

from database import (
    BehaviorMetricDaily,
    BehaviorMetricsRollup,
    GuildMetricsContributionDaily,
    GuildMetricsDaily,
    MemberJoinLeave,
    MessageRecord,
    PingEvent,
    UserBehaviorState,
    VoiceActivity,
    db,
)
from interactions import opted_out_members

EWMA_HALF_LIFE_DAYS = float(os.getenv("LIFETIME_EWMA_HALF_LIFE_DAYS", "30"))
CATCH_UP_MAX_DAYS = int(os.getenv("LIFETIME_CATCH_UP_MAX_DAYS", "30"))

logger = logging.getLogger(__name__)


def _day_bounds(day):
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


def _dates(values):
    result = set()
    for (value,) in values:
        if isinstance(value, datetime):
            value = value.date()
        elif isinstance(value, str):
            value = date.fromisoformat(value)
        if isinstance(value, date):
            result.add(value)
    return result


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value)
    return value


def _source_dates():
    """Every date still represented by daily metrics or retained raw rows."""
    dates = _dates(db.session.query(BehaviorMetricDaily.date).distinct().all())
    for timestamp in (
        MessageRecord.created_at,
        PingEvent.created_at,
        VoiceActivity.created_at,
    ):
        dates.update(
            _dates(db.session.query(func.date(timestamp)).distinct().all())
        )
    return dates


def _catch_up_plan(through_day, marker, max_days):
    """Ascending source dates in `(marker, through_day]`, plus skipped count.

    `marker` is the last date already processed (None when nothing is). The
    window is bounded by the oldest surviving source date so no day is counted
    as skipped just because it predates all data.
    """
    source = _source_dates()
    if not source:
        return [], 0
    oldest = min(source)
    start = oldest if marker is None else max(_as_date(marker) + timedelta(days=1), oldest)
    if start > through_day:
        return [], 0
    candidates = [day for day in sorted(source) if start <= day <= through_day]
    skipped = (through_day - start).days + 1 - len(candidates)
    return candidates, skipped


def update_user_state(day, half_life_days=EWMA_HALF_LIFE_DAYS):
    """Fold one date into user state; already-folded dates are never counted twice."""
    if half_life_days <= 0:
        raise ValueError("half_life_days must be greater than zero")

    daily = {
        (row.guild_id, row.user_id): row
        for row in BehaviorMetricDaily.query.filter_by(date=day).all()
    }
    start, end = _day_bounds(day)
    message_totals = {}
    for guild_id, user_id, count, mean_length, first_message in (
        db.session.query(
            MessageRecord.guild_id,
            MessageRecord.discord_id,
            func.count(MessageRecord.id),
            func.avg(MessageRecord.message_length),
            func.min(MessageRecord.created_at),
        )
        .filter(MessageRecord.created_at >= start, MessageRecord.created_at < end)
        .group_by(MessageRecord.guild_id, MessageRecord.discord_id)
        .all()
    ):
        message_totals[(guild_id, user_id)] = {
            "count": int(count or 0),
            "mean_length": float(mean_length) if mean_length is not None else None,
            "first_seen": first_message,
        }

    hourly_counts = defaultdict(lambda: [0] * 24)
    off_hours = defaultdict(int)
    for guild_id, user_id, hour, count in (
        db.session.query(
            MessageRecord.guild_id,
            MessageRecord.discord_id,
            MessageRecord.hour_of_day,
            func.count(MessageRecord.id),
        )
        .filter(
            MessageRecord.created_at >= start,
            MessageRecord.created_at < end,
            MessageRecord.hour_of_day.isnot(None),
        )
        .group_by(
            MessageRecord.guild_id,
            MessageRecord.discord_id,
            MessageRecord.hour_of_day,
        )
        .all()
    ):
        key = (guild_id, user_id)
        hourly_counts[key][int(hour)] = int(count)
        if hour < 9 or hour >= 17:
            off_hours[key] += int(count)

    ping_counts = defaultdict(int)
    ping_first = {}
    for guild_id, pinger_id, created_at in PingEvent.query.filter(
        PingEvent.created_at >= start, PingEvent.created_at < end
    ).with_entities(
        PingEvent.guild_id,
        PingEvent.pinger_id,
        PingEvent.created_at,
    ):
        key = (guild_id, pinger_id)
        ping_counts[key] += 1
        ping_first[key] = min(ping_first.get(key, created_at), created_at)

    voice_totals = {}
    for guild_id, user_id, seconds, first_voice in (
        db.session.query(
            VoiceActivity.guild_id,
            VoiceActivity.discord_id,
            func.coalesce(func.sum(VoiceActivity.duration_seconds), 0.0),
            func.min(VoiceActivity.created_at),
        )
        .filter(VoiceActivity.created_at >= start, VoiceActivity.created_at < end)
        .group_by(VoiceActivity.guild_id, VoiceActivity.discord_id)
        .all()
    ):
        voice_totals[(guild_id, user_id)] = (
            float(seconds or 0.0) / 3600.0,
            first_voice,
        )

    keys = set(daily) | set(message_totals) | set(ping_counts) | set(voice_totals)
    if not keys:
        return 0
    opted_out = opted_out_members({guild_id for guild_id, _ in keys})
    updated = 0
    for guild_id, user_id in keys:
        if (guild_id, user_id) in opted_out:
            continue
        state = UserBehaviorState.query.filter_by(
            guild_id=guild_id, discord_id=user_id
        ).first()
        if state and state.last_updated_date and state.last_updated_date >= day:
            continue

        metric = daily.get((guild_id, user_id))
        raw_message = message_totals.get((guild_id, user_id), {})
        messages = max(
            int(metric.messages) if metric is not None else 0,
            int(raw_message.get("count", 0)),
        )
        pings = max(
            int(metric.pings_sent) if metric is not None else 0,
            ping_counts.get((guild_id, user_id), 0),
        )
        voice_hours = max(
            float(metric.voice_hours) if metric is not None else 0.0,
            voice_totals.get((guild_id, user_id), (0.0, None))[0],
        )
        if state is None:
            state = UserBehaviorState(
                guild_id=guild_id,
                discord_id=user_id,
                n_days=0,
                mean_daily_messages=0.0,
                m2_daily_messages=0.0,
                hourly_profile=[0.0] * 24,
                lifetime_messages=0,
                lifetime_pings=0,
                lifetime_voice_hours=0.0,
                schema_version=1,
            )
            db.session.add(state)

        next_n = state.n_days + 1
        delta = messages - state.mean_daily_messages
        state.mean_daily_messages += delta / next_n
        state.m2_daily_messages += delta * (messages - state.mean_daily_messages)
        state.n_days = next_n
        state.lifetime_messages += messages
        state.lifetime_pings += pings
        state.lifetime_voice_hours += voice_hours

        first_seen = raw_message.get("first_seen") or ping_first.get(
            (guild_id, user_id)
        )
        if first_seen is None:
            first_seen = voice_totals.get((guild_id, user_id), (0.0, None))[1]
        if first_seen is None and metric is not None:
            first_seen = datetime.combine(day, datetime.min.time())
        if first_seen is not None and (
            state.first_seen_at is None or first_seen < state.first_seen_at
        ):
            state.first_seen_at = first_seen

        hourly = hourly_counts.get((guild_id, user_id))
        hourly_total = sum(hourly or [])
        if hourly_total:
            day_profile = [count / hourly_total for count in hourly]
            length = raw_message.get("mean_length")
            if state.last_updated_date is None or state.hourly_profile is None:
                alpha = 1.0
                state.hourly_profile = day_profile
                state.ewma_message_length = length
                state.ewma_off_hours_ratio = off_hours[(guild_id, user_id)] / hourly_total
            else:
                gap_days = max(1, (day - state.last_updated_date).days)
                alpha = 1.0 - math.exp(
                    -math.log(2.0) * gap_days / half_life_days
                )
                prior = state.hourly_profile
                state.hourly_profile = [
                    (1.0 - alpha) * old + alpha * new
                    for old, new in zip(prior, day_profile)
                ]
            if length is not None and state.last_updated_date is not None:
                state.ewma_message_length = (
                    length
                    if state.ewma_message_length is None
                    else (1.0 - alpha) * state.ewma_message_length + alpha * length
                )
            off_ratio = off_hours[(guild_id, user_id)] / hourly_total
            if state.last_updated_date is not None:
                state.ewma_off_hours_ratio = (
                    off_ratio
                    if state.ewma_off_hours_ratio is None
                    else (1.0 - alpha) * state.ewma_off_hours_ratio
                    + alpha * off_ratio
                )

        state.last_updated_date = day
        state.schema_version = 1
        updated += 1

    db.session.commit()
    return updated


def backfill_user_state():
    """Idempotently fold every date still represented by daily or retained raw data."""
    return sum(update_user_state(day) for day in sorted(_source_dates()))


def catch_up_user_state(through_day=None, max_days=None):
    """Fold every missed date up to `through_day`, oldest first.

    Starts the day after the oldest `last_updated_date` (bounded by the oldest
    surviving source date) so no member is double-counted, and folds at most
    `max_days` days per call so a single request stays bounded. Dates whose
    source rows are already gone are skipped and reported, never folded as 0.
    """
    through_day = _as_date(through_day or (date.today() - timedelta(days=1)))
    if max_days is None:
        max_days = CATCH_UP_MAX_DAYS
    marker = db.session.query(func.min(UserBehaviorState.last_updated_date)).scalar()
    candidates, skipped = _catch_up_plan(through_day, marker, max_days)
    if skipped:
        logger.warning(
            "lifetime catch-up skipped %d day(s) with no surviving source data up to %s",
            skipped,
            through_day,
        )
    folded = []
    for day in candidates:
        if len(folded) >= max_days:
            break
        if update_user_state(day):
            folded.append(day.isoformat())
    return {"folded": len(folded), "skipped_no_source": skipped, "dates": folded}


def days_since_active(state, today=None):
    """Calendar days since a member's last folded active day.

    `n_days` and `mean_daily_messages` count ACTIVE days only, so inactivity
    must be read from `last_updated_date` (this helper) — never a low mean.
    """
    if state is None or state.last_updated_date is None:
        return None
    today = _as_date(today or date.today())
    return (today - _as_date(state.last_updated_date)).days


def _rebuild_guild_daily(guild_id, day):
    rows = GuildMetricsContributionDaily.query.filter_by(
        guild_id=guild_id, date=day
    ).all()
    aggregate = GuildMetricsDaily.query.filter_by(guild_id=guild_id, date=day).first()
    if not rows:
        if aggregate:
            db.session.delete(aggregate)
        return False
    values = {
        "active_users": sum(1 for row in rows if row.active),
        "messages": sum(row.messages for row in rows),
        "pings": sum(row.pings for row in rows),
        "voice_hours": sum(row.voice_hours for row in rows),
        "joins": sum(row.joins for row in rows),
        "leaves": sum(row.leaves for row in rows),
        "extra": {"contribution_rows": len(rows)},
    }
    if aggregate is None:
        aggregate = GuildMetricsDaily(guild_id=guild_id, date=day)
        db.session.add(aggregate)
    for name, value in values.items():
        setattr(aggregate, name, value)
    return True


def compute_guild_metrics_daily(day):
    """Rebuild member contributions and their guild aggregate for one date."""
    metrics = {
        (row.guild_id, row.user_id): row
        for row in BehaviorMetricDaily.query.filter_by(date=day).all()
    }
    start, end = _day_bounds(day)
    message_counts = {
        (guild_id, user_id): int(count)
        for guild_id, user_id, count in db.session.query(
            MessageRecord.guild_id,
            MessageRecord.discord_id,
            func.count(MessageRecord.id),
        )
        .filter(MessageRecord.created_at >= start, MessageRecord.created_at < end)
        .group_by(MessageRecord.guild_id, MessageRecord.discord_id)
        .all()
    }
    ping_counts = defaultdict(int)
    for guild_id, pinger_id in PingEvent.query.filter(
        PingEvent.created_at >= start, PingEvent.created_at < end
    ).with_entities(PingEvent.guild_id, PingEvent.pinger_id):
        ping_counts[(guild_id, pinger_id)] += 1
    voice_hours = {
        (guild_id, user_id): float(seconds or 0.0) / 3600.0
        for guild_id, user_id, seconds in db.session.query(
            VoiceActivity.guild_id,
            VoiceActivity.discord_id,
            func.coalesce(func.sum(VoiceActivity.duration_seconds), 0.0),
        )
        .filter(VoiceActivity.created_at >= start, VoiceActivity.created_at < end)
        .group_by(VoiceActivity.guild_id, VoiceActivity.discord_id)
        .all()
    }
    joins_leaves = defaultdict(lambda: [0, 0])
    for event in MemberJoinLeave.query.filter(
        MemberJoinLeave.created_at >= start,
        MemberJoinLeave.created_at < end,
        MemberJoinLeave.is_bot.is_(False),
    ).all():
        counts = joins_leaves[(event.guild_id, event.member_id)]
        if event.event_type == "join":
            counts[0] += 1
        elif event.event_type == "leave":
            counts[1] += 1

    keys = (
        set(metrics)
        | set(message_counts)
        | set(ping_counts)
        | set(voice_hours)
        | set(joins_leaves)
    )
    guild_ids = {guild_id for guild_id, _ in keys}
    opted_out = opted_out_members(guild_ids)
    db.session.query(GuildMetricsContributionDaily).filter_by(date=day).delete(
        synchronize_session=False
    )
    contributions = []
    for guild_id, user_id in keys:
        if (guild_id, user_id) in opted_out:
            continue
        metric = metrics.get((guild_id, user_id))
        messages = max(
            int(metric.messages) if metric is not None else 0,
            message_counts.get((guild_id, user_id), 0),
        )
        pings = max(
            int(metric.pings_sent) if metric is not None else 0,
            ping_counts.get((guild_id, user_id), 0),
        )
        voice = max(
            float(metric.voice_hours) if metric is not None else 0.0,
            voice_hours.get((guild_id, user_id), 0.0),
        )
        joins, leaves = joins_leaves.get((guild_id, user_id), (0, 0))
        contributions.append(
            GuildMetricsContributionDaily(
                guild_id=guild_id,
                user_id=user_id,
                date=day,
                messages=messages,
                pings=pings,
                voice_hours=voice,
                joins=joins,
                leaves=leaves,
                active=bool(messages or pings or voice),
            )
        )
    if contributions:
        db.session.add_all(contributions)
        db.session.flush()
    guild_ids.update(
        row.guild_id
        for row in GuildMetricsDaily.query.filter_by(date=day).all()
    )
    for guild_id in guild_ids:
        _rebuild_guild_daily(guild_id, day)
    db.session.commit()
    return {"guilds": len(guild_ids), "contributions": len(contributions)}


def rebuild_guild_metrics_daily(guild_id, day):
    """Reaggregate a date from surviving member contributions after erasure."""
    changed = _rebuild_guild_daily(guild_id, day)
    db.session.commit()
    return changed


def catch_up_guild_metrics_daily(through_day=None, max_days=None):
    """Rebuild guild aggregates for dates missed since the last computed day."""
    through_day = _as_date(through_day or (date.today() - timedelta(days=1)))
    if max_days is None:
        max_days = CATCH_UP_MAX_DAYS
    marker = db.session.query(func.max(GuildMetricsDaily.date)).scalar()
    candidates, skipped = _catch_up_plan(through_day, marker, max_days)
    if skipped:
        logger.warning(
            "guild-metrics catch-up skipped %d day(s) with no surviving source data up to %s",
            skipped,
            through_day,
        )
    folded = []
    for day in candidates:
        if len(folded) >= max_days:
            break
        compute_guild_metrics_daily(day)
        folded.append(day.isoformat())
    return {"folded": len(folded), "skipped_no_source": skipped, "dates": folded}


def _period_bounds(day, period_type):
    if period_type == "weekly":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=7)
    else:
        start = day.replace(day=1)
        end = start.replace(
            year=start.year + (start.month == 12),
            month=1 if start.month == 12 else start.month + 1,
        )
    return start, end


def _rebuild_rollup(day, period_type):
    start, end = _period_bounds(day, period_type)
    rows = BehaviorMetricDaily.query.filter(
        BehaviorMetricDaily.date >= start, BehaviorMetricDaily.date < end
    ).all()
    opted_out = opted_out_members({row.guild_id for row in rows})
    grouped = defaultdict(list)
    for row in rows:
        if (row.guild_id, row.user_id) not in opted_out:
            grouped[(row.guild_id, row.user_id)].append(row)

    db.session.query(BehaviorMetricsRollup).filter_by(
        period_type=period_type, period_start=start
    ).delete(synchronize_session=False)
    values = []
    for (guild_id, user_id), daily_rows in grouped.items():
        def average(field):
            nums = [
                getattr(row, field)
                for row in daily_rows
                if getattr(row, field) is not None
            ]
            return sum(nums) / len(nums) if nums else None

        values.append(
            BehaviorMetricsRollup(
                guild_id=guild_id,
                user_id=user_id,
                period_type=period_type,
                period_start=start,
                active_days=len(daily_rows),
                messages=sum(row.messages for row in daily_rows),
                pings_sent=sum(row.pings_sent for row in daily_rows),
                questions_asked=sum(row.questions_asked for row in daily_rows),
                answers_given=sum(row.answers_given for row in daily_rows),
                voice_hours=sum(row.voice_hours for row in daily_rows),
                extra={
                    "avg_latency_p50_minutes": average("latency_p50_minutes"),
                    "avg_latency_p90_minutes": average("latency_p90_minutes"),
                    "avg_cadence_cv": average("cadence_cv"),
                    "avg_channel_diversity": average("channel_diversity"),
                },
            )
        )
    if values:
        db.session.add_all(values)
    return len(values)


def rebuild_behavior_metrics_rollups(day):
    """Rebuild the weekly and monthly rollups containing `day`."""
    result = {
        period: _rebuild_rollup(day, period) for period in ("weekly", "monthly")
    }
    db.session.commit()
    return result


def backfill_behavior_metrics_rollups():
    """Rebuild all weekly/monthly periods represented by retained daily metrics."""
    dates = _dates(db.session.query(BehaviorMetricDaily.date).distinct().all())
    periods = {
        (period_type, _period_bounds(day, period_type)[0])
        for day in dates
        for period_type in ("weekly", "monthly")
    }
    total = 0
    for period_type, period_start in sorted(periods):
        total += _rebuild_rollup(period_start, period_type)
    db.session.commit()
    return total


def catch_up_rollups(through_day=None, max_days=None):
    """Rebuild the weekly/monthly rollups missed since the last built period.

    The period containing `through_day` is always refreshed (it is still open),
    plus every period holding a surviving source date after the newest rollup
    already on record. At most `max_days` periods are rebuilt per call.
    """
    through_day = _as_date(through_day or (date.today() - timedelta(days=1)))
    if max_days is None:
        max_days = CATCH_UP_MAX_DAYS
    source = _source_dates()
    periods = set()
    for period_type in ("weekly", "monthly"):
        periods.add((period_type, _period_bounds(through_day, period_type)[0]))
        if not source:
            continue
        oldest = min(source)
        marker = (
            db.session.query(func.max(BehaviorMetricsRollup.period_start))
            .filter_by(period_type=period_type)
            .scalar()
        )
        start = oldest if marker is None else max(_as_date(marker), oldest)
        for day in source:
            if start <= day <= through_day:
                periods.add((period_type, _period_bounds(day, period_type)[0]))
    ordered = sorted(periods, key=lambda item: (item[1], item[0]))
    done = []
    for period_type, period_start in ordered:
        if len(done) >= max_days:
            break
        _rebuild_rollup(period_start, period_type)
        done.append(f"{period_type}:{period_start.isoformat()}")
    db.session.commit()
    return {"folded": len(done), "skipped_no_source": 0, "periods": done}
