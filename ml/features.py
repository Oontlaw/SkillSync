from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np

from database import (
    BehavioralAnomaly,
    BurnoutRisk,
    GuildMember,
    MessageRecord,
    ScoreLog,
    VoiceActivity,
    Worker,
    db,
)


def user_hourly_profile(discord_id, days=30, guild_id=None):
    """Build 24-dim hourly activity vector for a user over last N days.
    If guild_id is provided, only messages from that guild are included."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    q = MessageRecord.query.filter(
        MessageRecord.discord_id == discord_id,
        MessageRecord.created_at >= cutoff,
        MessageRecord.hour_of_day != None,
    )
    if guild_id:
        q = q.filter(MessageRecord.guild_id == guild_id)
    rows = q.with_entities(MessageRecord.hour_of_day).all()
    if not rows:
        return None
    counts = np.zeros(24)
    for (h,) in rows:
        counts[h] += 1
    total = counts.sum()
    if total > 0:
        counts = counts / total
    return counts


def user_message_stats(discord_id, days=30, guild_id=None):
    """Compute message length statistics and daily volume for a user.
    If guild_id is provided, only messages from that guild are included."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    q = MessageRecord.query.filter(
        MessageRecord.discord_id == discord_id, MessageRecord.created_at >= cutoff
    )
    if guild_id:
        q = q.filter(MessageRecord.guild_id == guild_id)
    rows = q.with_entities(MessageRecord.message_length, MessageRecord.created_at).all()
    if len(rows) < 5:
        return None
    lengths = np.array([r.message_length for r in rows])
    # Daily volume
    day_counts = defaultdict(int)
    for _, created in rows:
        day_counts[created.date()] += 1
    daily_vals = np.array(list(day_counts.values()))
    daily_mean = float(daily_vals.mean()) if len(daily_vals) > 0 else 0
    daily_std = float(daily_vals.std()) if len(daily_vals) > 1 else 0
    return {
        "mean_len": float(lengths.mean()),
        "std_len": float(lengths.std()) if len(lengths) > 1 else 0,
        "p95_len": float(np.percentile(lengths, 95)),
        "max_len": float(lengths.max()),
        "daily_mean": daily_mean,
        "daily_std": daily_std,
        "cv": daily_std / daily_mean if daily_mean > 0 else 0,
        "total_msgs": len(rows),
    }


def user_anomaly_feature_vector(discord_id, days=30, guild_id=None):
    """Build combined feature vector for anomaly detection.
    If guild_id is provided, only messages from that guild are included.
    Shape: (28,) = 24 hourly bins + mean_len + std_len + p95_len + cv."""
    profile = user_hourly_profile(discord_id, days, guild_id=guild_id)
    stats = user_message_stats(discord_id, days, guild_id=guild_id)
    if profile is None or stats is None:
        return None
    extra = np.array(
        [stats["mean_len"], stats["std_len"], stats["p95_len"], stats["cv"]]
    )
    return np.concatenate([profile, extra])


def all_user_feature_vectors(days=30, min_msgs=10, guild_id=None):
    """Build feature matrix for all users with sufficient message history.
    If guild_id is provided, only users with messages in that guild are included.
    Returns (X, ids) where X is (n_users, 28) and ids is list of discord_ids."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    q = db.session.query(MessageRecord.discord_id).filter(
        MessageRecord.created_at >= cutoff
    )
    if guild_id:
        q = q.filter(MessageRecord.guild_id == guild_id)
    ids = (
        q.group_by(MessageRecord.discord_id)
        .having(db.func.count(MessageRecord.id) >= min_msgs)
        .all()
    )
    ids = [r[0] for r in ids]
    X_list, valid_ids = [], []
    for did in ids:
        vec = user_anomaly_feature_vector(did, days, guild_id=guild_id)
        if vec is not None:
            X_list.append(vec)
            valid_ids.append(did)
    if not X_list:
        return np.empty((0, 28)), []
    return np.array(X_list), valid_ids


def guild_forecast_features(guild_id, days=30, window=7):
    """Build feature matrix for hourly forecasting with guild-specific baseline.
    Features (10 total):
      hour_sin, hour_cos, dow_sin, dow_cos,
      roll_6, roll_12, roll_24,
      guild_hourly_mean, guild_hourly_std, is_peak_hour
    Returns (X, y, timestamps)."""
    from database import GuildActivityBaseline

    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        MessageRecord.query.filter(
            MessageRecord.guild_id == guild_id,
            MessageRecord.created_at >= cutoff,
            MessageRecord.hour_of_day != None,
        )
        .with_entities(
            MessageRecord.hour_of_day,
            MessageRecord.day_of_week,
            MessageRecord.created_at,
        )
        .all()
    )
    if not rows:
        return None, None, None

    # Load guild baseline (may be None on first run)
    baseline = GuildActivityBaseline.query.filter_by(guild_id=guild_id).first()
    hourly_mean = (
        baseline.hourly_mean if baseline and baseline.hourly_mean else [0.0] * 24
    )
    hourly_std = baseline.hourly_std if baseline and baseline.hourly_std else [1.0] * 24
    peak_hours = set(baseline.peak_hours) if baseline and baseline.peak_hours else set()

    # Aggregate into hourly buckets
    hourly = defaultdict(int)
    timestamps = {}
    for hour, dow, created in rows:
        bucket = created.replace(minute=0, second=0, microsecond=0)
        hourly[bucket] += 1
        timestamps[bucket] = (hour, dow)
    if not hourly:
        return None, None, None

    sorted_buckets = sorted(hourly.keys())
    counts = np.array([hourly[b] for b in sorted_buckets])

    X_list, y_list, ts_list = [], [], []
    for i, bucket in enumerate(sorted_buckets):
        hour, dow = timestamps[bucket]
        hour_sin = np.sin(2 * np.pi * hour / 24)
        hour_cos = np.cos(2 * np.pi * hour / 24)
        dow_sin = np.sin(2 * np.pi * dow / 7)
        dow_cos = np.cos(2 * np.pi * dow / 7)
        roll_6 = (
            counts[max(0, i - 6) : i].mean()
            if i >= 6
            else (counts[: i + 1].mean() if i > 0 else 0)
        )
        roll_12 = (
            counts[max(0, i - 12) : i].mean()
            if i >= 12
            else (counts[: i + 1].mean() if i > 0 else 0)
        )
        roll_24 = (
            counts[max(0, i - 24) : i].mean()
            if i >= 24
            else (counts[: i + 1].mean() if i > 0 else 0)
        )

        # Guild-specific baseline features
        g_mean = hourly_mean[hour]
        g_std = max(hourly_std[hour], 0.1)
        is_peak = 1.0 if hour in peak_hours else 0.0

        X_list.append(
            [
                hour_sin,
                hour_cos,
                dow_sin,
                dow_cos,
                roll_6,
                roll_12,
                roll_24,
                g_mean,
                g_std,
                is_peak,
            ]
        )
        y_list.append(counts[i])
        ts_list.append(bucket.isoformat())

    return np.array(X_list), np.array(y_list), ts_list


def staff_feature_vectors(days=30):
    """Build feature matrix for staff burnout/anomaly detection.
    Features per staff member:
    - anomaly_freq: number of anomalies in last 30d / 10
    - reversal_rate: proportion of ScoreLog changes that are negative+reversal
    - voice_avg_duration: average voice session length in last 30d
    - action_count: total discord actions in last 30d
    - off_hour_ratio: proportion of messages outside 09-17
    - activity_cv: coefficient of variation of daily message count
    - consistency_score: 0-100 based on daily CV
    Returns (X, worker_ids, discord_ids, names).
    """
    from database import ScoreLog, Worker
    from scoring import POINTS

    cutoff_30 = datetime.utcnow() - timedelta(days=days)
    workers = Worker.query.filter(Worker.discord_id != None).all()
    if not workers:
        return None, [], [], []

    X_list, wids, dids, names = [], [], [], []
    for w in workers:
        did = w.discord_id
        # Anomaly frequency
        anomaly_count = BehavioralAnomaly.query.filter(
            BehavioralAnomaly.discord_id == did,
            BehavioralAnomaly.detected_at >= cutoff_30,
        ).count()
        anomaly_freq = min(1.0, anomaly_count / 10)

        # Reversals
        reversals = ScoreLog.query.filter(
            ScoreLog.worker_id == w.id,
            ScoreLog.change < 0,
            ScoreLog.reason.ilike("%reversal%"),
            ScoreLog.created_at >= cutoff_30,
        ).count()
        total_actions = ScoreLog.query.filter(
            ScoreLog.worker_id == w.id,
            ScoreLog.source == "discord",
            ScoreLog.created_at >= cutoff_30,
        ).count()
        reversal_rate = reversals / max(total_actions, 1)

        # Voice
        voice_avg = (
            db.session.query(db.func.avg(VoiceActivity.duration_seconds))
            .filter(
                VoiceActivity.discord_id == did, VoiceActivity.created_at >= cutoff_30
            )
            .scalar()
            or 0
        )

        # Off-hour ratio
        total_msgs = MessageRecord.query.filter(
            MessageRecord.discord_id == did, MessageRecord.created_at >= cutoff_30
        ).count()
        off_hours = 0
        if total_msgs > 0:
            off_hours = MessageRecord.query.filter(
                MessageRecord.discord_id == did,
                MessageRecord.created_at >= cutoff_30,
                MessageRecord.hour_of_day != None,
                ~MessageRecord.hour_of_day.between(9, 16),
            ).count()
        off_hour_ratio = off_hours / max(total_msgs, 1)

        # Activity consistency (CV of daily counts)
        daily_counts = (
            db.session.query(
                db.func.date(MessageRecord.created_at).label("day"),
                db.func.count(MessageRecord.id).label("c"),
            )
            .filter(
                MessageRecord.discord_id == did, MessageRecord.created_at >= cutoff_30
            )
            .group_by(db.func.date(MessageRecord.created_at))
            .all()
        )
        if daily_counts:
            daily_vals = np.array([d.c for d in daily_counts])
            daily_mean = daily_vals.mean()
            daily_std = daily_vals.std() if len(daily_vals) > 1 else 0
            cv = daily_std / max(daily_mean, 0.1)
            consistency = max(0, min(100, (1 - min(cv, 1)) * 100))
        else:
            consistency = 50
            cv = 0.5

        vec = np.array(
            [
                anomaly_freq,
                reversal_rate,
                min(1.0, voice_avg / 3600),
                min(1.0, total_actions / 50),
                off_hour_ratio,
                min(cv, 1.0),
                consistency / 100,
            ]
        )
        X_list.append(vec)
        wids.append(w.id)
        dids.append(did)
        names.append(w.name)

    return np.array(X_list), wids, dids, names


def community_prior_for_worker(worker_id):
    """Build community behavioral priors for a worker based on Discord activity.
    Returns dict with 5 signals, or None if insufficient data."""
    worker = db.session.get(Worker, worker_id)
    if not worker or not worker.discord_id:
        return None
    did = worker.discord_id
    cutoff_30 = datetime.utcnow() - timedelta(days=30)
    cutoff_7 = datetime.utcnow() - timedelta(days=7)

    total_msgs_30 = MessageRecord.query.filter(
        MessageRecord.discord_id == did, MessageRecord.created_at >= cutoff_30
    ).count()
    if total_msgs_30 < 10:
        return None

    # 1. Activity consistency (inverted CV of daily counts)
    daily_counts = (
        db.session.query(
            db.func.date(MessageRecord.created_at).label("day"),
            db.func.count(MessageRecord.id).label("c"),
        )
        .filter(MessageRecord.discord_id == did, MessageRecord.created_at >= cutoff_30)
        .group_by(db.func.date(MessageRecord.created_at))
        .all()
    )
    if daily_counts and len(daily_counts) > 1:
        vals = np.array([d.c for d in daily_counts])
        cv = vals.std() / max(vals.mean(), 0.1)
        activity_consistency = max(0.0, min(1.0, 1.0 - min(cv, 1.0)))
    else:
        activity_consistency = 0.5

    # 2. Off-hours ratio (outside 09:00-17:00)
    off_hours_count = MessageRecord.query.filter(
        MessageRecord.discord_id == did,
        MessageRecord.created_at >= cutoff_30,
        MessageRecord.hour_of_day != None,
        ~MessageRecord.hour_of_day.between(9, 16),
    ).count()
    off_hours_ratio = off_hours_count / max(total_msgs_30, 1)

    # 3. Anomaly rate (anomalies per 30 days, capped at 10)
    anomaly_count = BehavioralAnomaly.query.filter(
        BehavioralAnomaly.discord_id == did, BehavioralAnomaly.detected_at >= cutoff_30
    ).count()
    anomaly_rate = min(10, anomaly_count) / 10.0

    # 4. Score trajectory (second half vs first half of 30-day window)
    recent_scores = (
        ScoreLog.query.filter(
            ScoreLog.worker_id == worker_id, ScoreLog.created_at >= cutoff_30
        )
        .order_by(ScoreLog.created_at)
        .all()
    )
    if len(recent_scores) >= 4:
        mid = len(recent_scores) // 2
        first_half = sum(abs(s.change) for s in recent_scores[:mid])
        second_half = sum(abs(s.change) for s in recent_scores[mid:])
        if first_half + second_half > 0:
            score_trajectory = (
                1.0
                if second_half > first_half
                else (0.0 if second_half < first_half else 0.5)
            )
        else:
            score_trajectory = 0.5
    else:
        score_trajectory = 0.5

    # 5. Recent activity ratio (msgs/day last 7d / msgs/day last 30d)
    total_msgs_7 = MessageRecord.query.filter(
        MessageRecord.discord_id == did, MessageRecord.created_at >= cutoff_7
    ).count()
    daily_30 = total_msgs_30 / 30.0
    daily_7 = total_msgs_7 / 7.0
    recent_activity_ratio = min(2.0, daily_7 / max(daily_30, 0.1))

    return {
        "activity_consistency": round(activity_consistency, 4),
        "off_hours_ratio": round(off_hours_ratio, 4),
        "anomaly_rate": round(anomaly_rate, 4),
        "score_trajectory": round(score_trajectory, 4),
        "recent_activity_ratio": round(recent_activity_ratio, 4),
    }


def update_user_baselines(days_long=90, days_short=7, min_msgs=5):
    """Compute and upsert UserBehaviorBaseline for all users with data.
    Called by engine.train_all() before model training.
    This is the core of long-term memory — data accumulates forever."""
    from scipy.spatial.distance import cosine as cosine_distance

    from database import UserBehaviorBaseline

    now = datetime.utcnow()
    cutoff_long = now - timedelta(days=days_long)
    cutoff_short = now - timedelta(days=days_short)

    # Get all users with enough data
    users = (
        db.session.query(MessageRecord.discord_id, MessageRecord.guild_id)
        .filter(MessageRecord.created_at >= cutoff_long)
        .group_by(MessageRecord.discord_id, MessageRecord.guild_id)
        .having(db.func.count(MessageRecord.id) >= min_msgs)
        .all()
    )

    updated = 0
    for discord_id, guild_id in users:
        # --- Long-term (90d) profile ---
        long_rows = (
            MessageRecord.query.filter(
                MessageRecord.discord_id == discord_id,
                MessageRecord.guild_id == guild_id,
                MessageRecord.created_at >= cutoff_long,
                MessageRecord.hour_of_day != None,
            )
            .with_entities(
                MessageRecord.hour_of_day,
                MessageRecord.message_length,
                MessageRecord.created_at,
            )
            .all()
        )

        if not long_rows:
            continue

        # Hourly profile (normalized)
        hourly_counts = np.zeros(24)
        for hour, _, _ in long_rows:
            hourly_counts[hour] += 1
        total = hourly_counts.sum()
        hourly_profile = (hourly_counts / max(total, 1)).tolist()

        # Daily stats
        day_counts = defaultdict(int)
        for _, _, created in long_rows:
            day_counts[created.date()] += 1
        daily_vals = (
            np.array(list(day_counts.values())) if day_counts else np.array([0])
        )
        mean_daily = float(daily_vals.mean())
        std_daily = float(daily_vals.std()) if len(daily_vals) > 1 else 0.0

        # Message length
        lengths = np.array([r[1] for r in long_rows if r[1]])
        mean_length = float(lengths.mean()) if len(lengths) > 0 else 0.0

        # Off-hours ratio (outside 9-17)
        off_hours = sum(1 for h, _, _ in long_rows if h < 9 or h > 16)
        off_hours_ratio_90d = off_hours / max(len(long_rows), 1)

        # --- Short-term (7d) stats ---
        short_rows = [r for r in long_rows if r[2] >= cutoff_short]
        if short_rows:
            short_day_counts = defaultdict(int)
            for _, _, created in short_rows:
                short_day_counts[created.date()] += 1
            short_vals = np.array(list(short_day_counts.values()))
            mean_daily_7d = float(short_vals.mean()) if len(short_vals) > 0 else 0.0
            off_hours_7d = sum(1 for h, _, _ in short_rows if h < 9 or h > 16)
            off_hours_ratio_7d = off_hours_7d / max(len(short_rows), 1)
        else:
            mean_daily_7d = 0.0
            off_hours_ratio_7d = 0.0

        # --- Drift signals ---
        volume_drift = (mean_daily_7d - mean_daily) / max(std_daily, 1.0)

        # Pattern drift: cosine distance between short-term and long-term hourly profile
        if short_rows:
            short_hourly = np.zeros(24)
            for h, _, _ in short_rows:
                short_hourly[h] += 1
            short_total = short_hourly.sum()
            if short_total > 0 and total > 0:
                short_profile = short_hourly / short_total
                try:
                    pattern_drift = float(
                        cosine_distance(hourly_profile, short_profile)
                    )
                except Exception:
                    pattern_drift = 0.0
            else:
                pattern_drift = 0.0
        else:
            pattern_drift = 0.0

        is_drifting = abs(volume_drift) > 2.0 or pattern_drift > 0.3

        # Confidence: grows logarithmically with total messages seen
        total_msgs = len(long_rows)
        confidence = float(min(1.0, np.log1p(total_msgs) / np.log1p(1000)))

        # Upsert
        existing = UserBehaviorBaseline.query.filter_by(
            discord_id=discord_id, guild_id=guild_id
        ).first()
        if existing:
            existing.hourly_profile_90d = hourly_profile
            existing.mean_daily_msgs_90d = mean_daily
            existing.std_daily_msgs_90d = std_daily
            existing.mean_msg_length_90d = mean_length
            existing.off_hours_ratio_90d = off_hours_ratio_90d
            existing.mean_daily_msgs_7d = mean_daily_7d
            existing.off_hours_ratio_7d = off_hours_ratio_7d
            existing.volume_drift = round(volume_drift, 4)
            existing.pattern_drift = round(pattern_drift, 4)
            existing.is_drifting = is_drifting
            existing.total_msgs_seen = total_msgs
            existing.baseline_confidence = round(confidence, 4)
            existing.updated_at = now
        else:
            db.session.add(
                UserBehaviorBaseline(
                    discord_id=discord_id,
                    guild_id=guild_id,
                    hourly_profile_90d=hourly_profile,
                    mean_daily_msgs_90d=mean_daily,
                    std_daily_msgs_90d=std_daily,
                    mean_msg_length_90d=mean_length,
                    off_hours_ratio_90d=off_hours_ratio_90d,
                    mean_daily_msgs_7d=mean_daily_7d,
                    off_hours_ratio_7d=off_hours_ratio_7d,
                    volume_drift=round(volume_drift, 4),
                    pattern_drift=round(pattern_drift, 4),
                    is_drifting=is_drifting,
                    total_msgs_seen=total_msgs,
                    baseline_confidence=round(confidence, 4),
                )
            )
        updated += 1

    db.session.commit()
    return updated


def update_guild_baselines():
    """Compute and upsert GuildActivityBaseline for all guilds.
    Uses ALL historical data — no cutoff. Called by engine.train_all()."""
    from database import GuildActivityBaseline, GuildInfo

    guilds = GuildInfo.query.all()
    updated = 0

    for g in guilds:
        rows = (
            MessageRecord.query.filter(
                MessageRecord.guild_id == g.guild_id,
                MessageRecord.hour_of_day != None,
            )
            .with_entities(
                MessageRecord.hour_of_day,
                MessageRecord.created_at,
            )
            .all()
        )

        if not rows:
            continue

        # Count occurrences per (hour, day) then average across days
        day_hour_counts = defaultdict(lambda: defaultdict(int))
        for hour, created in rows:
            day_hour_counts[created.date()][hour] += 1

        hourly_mean = []
        hourly_std = []
        for h in range(24):
            vals = [day_hour_counts[d][h] for d in day_hour_counts]
            arr = np.array(vals) if vals else np.array([0])
            hourly_mean.append(round(float(arr.mean()), 4))
            hourly_std.append(round(float(arr.std()), 4) if len(arr) > 1 else 0.0)

        # Peak hours: top 6 by mean
        sorted_hours = sorted(range(24), key=lambda h: hourly_mean[h], reverse=True)
        peak_hours = sorted_hours[:6]

        total_msgs = len(rows)
        days_seen = len(set(created.date() for _, created in rows))

        existing = GuildActivityBaseline.query.filter_by(guild_id=g.guild_id).first()
        if existing:
            existing.hourly_mean = hourly_mean
            existing.hourly_std = hourly_std
            existing.peak_hours = peak_hours
            existing.total_msgs_seen = total_msgs
            existing.days_of_history = days_seen
            existing.updated_at = datetime.utcnow()
        else:
            db.session.add(
                GuildActivityBaseline(
                    guild_id=g.guild_id,
                    hourly_mean=hourly_mean,
                    hourly_std=hourly_std,
                    peak_hours=peak_hours,
                    total_msgs_seen=total_msgs,
                    days_of_history=days_seen,
                )
            )
        updated += 1

    db.session.commit()
    return updated
