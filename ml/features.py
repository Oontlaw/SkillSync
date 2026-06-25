import numpy as np
from collections import defaultdict
from datetime import datetime, timedelta
from database import db, MessageRecord, ScoreLog, VoiceActivity, BehavioralAnomaly, BurnoutRisk, GuildMember, Worker


def user_hourly_profile(discord_id, days=30, guild_id=None):
    """Build 24-dim hourly activity vector for a user over last N days.
    If guild_id is provided, only messages from that guild are included."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    q = MessageRecord.query.filter(
        MessageRecord.discord_id == discord_id,
        MessageRecord.created_at >= cutoff,
        MessageRecord.hour_of_day != None
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
        MessageRecord.discord_id == discord_id,
        MessageRecord.created_at >= cutoff
    )
    if guild_id:
        q = q.filter(MessageRecord.guild_id == guild_id)
    rows = q.with_entities(
        MessageRecord.message_length,
        MessageRecord.created_at
    ).all()
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
        'mean_len': float(lengths.mean()),
        'std_len': float(lengths.std()) if len(lengths) > 1 else 0,
        'p95_len': float(np.percentile(lengths, 95)),
        'max_len': float(lengths.max()),
        'daily_mean': daily_mean,
        'daily_std': daily_std,
        'cv': daily_std / daily_mean if daily_mean > 0 else 0,
        'total_msgs': len(rows),
    }


def user_anomaly_feature_vector(discord_id, days=30, guild_id=None):
    """Build combined feature vector for anomaly detection.
    If guild_id is provided, only messages from that guild are included.
    Shape: (28,) = 24 hourly bins + mean_len + std_len + p95_len + cv."""
    profile = user_hourly_profile(discord_id, days, guild_id=guild_id)
    stats = user_message_stats(discord_id, days, guild_id=guild_id)
    if profile is None or stats is None:
        return None
    extra = np.array([stats['mean_len'], stats['std_len'], stats['p95_len'], stats['cv']])
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
    ids = q.group_by(MessageRecord.discord_id).having(
        db.func.count(MessageRecord.id) >= min_msgs
    ).all()
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


def guild_hourly_matrix(guild_id, days=30):
    """Build hourly activity matrix for a guild over last N days.
    Returns (n_days*24, 3) with columns: hour, day_of_week, message_count."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = MessageRecord.query.filter(
        MessageRecord.guild_id == guild_id,
        MessageRecord.created_at >= cutoff,
        MessageRecord.hour_of_day != None
    ).with_entities(
        MessageRecord.hour_of_day,
        MessageRecord.day_of_week,
        MessageRecord.created_at
    ).all()
    if not rows:
        return None
    # Aggregate by (date, hour)
    agg = defaultdict(int)
    for hour, dow, created in rows:
        key = (created.date(), hour, dow)
        agg[key] += 1
    if not agg:
        return None
    X = np.array([[k[1], k[2], v] for k, v in agg.items()])
    return X


def guild_forecast_features(guild_id, days=30, window=7):
    """Build feature matrix for hourly forecasting.
    Returns (X, y, timestamps) for regression.
    Features: hour (sin/cos), day_of_week (sin/cos), rolling_avg_{6,12,24}h.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = MessageRecord.query.filter(
        MessageRecord.guild_id == guild_id,
        MessageRecord.created_at >= cutoff,
        MessageRecord.hour_of_day != None
    ).with_entities(
        MessageRecord.hour_of_day,
        MessageRecord.day_of_week,
        MessageRecord.created_at
    ).all()
    if not rows:
        return None, None, None
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
    # Build features
    X_list, y_list, ts_list = [], [], []
    for i, bucket in enumerate(sorted_buckets):
        hour, dow = timestamps[bucket]
        hour_sin = np.sin(2 * np.pi * hour / 24)
        hour_cos = np.cos(2 * np.pi * hour / 24)
        dow_sin = np.sin(2 * np.pi * dow / 7)
        dow_cos = np.cos(2 * np.pi * dow / 7)
        # Rolling averages
        roll_6 = counts[max(0, i - 6):i].mean() if i >= 6 else counts[:i + 1].mean() if i > 0 else 0
        roll_12 = counts[max(0, i - 12):i].mean() if i >= 12 else counts[:i + 1].mean() if i > 0 else 0
        roll_24 = counts[max(0, i - 24):i].mean() if i >= 24 else counts[:i + 1].mean() if i > 0 else 0
        X_list.append([hour_sin, hour_cos, dow_sin, dow_cos, roll_6, roll_12, roll_24])
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
    from database import Worker, ScoreLog
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
            BehavioralAnomaly.detected_at >= cutoff_30
        ).count()
        anomaly_freq = min(1.0, anomaly_count / 10)

        # Reversals
        reversals = ScoreLog.query.filter(
            ScoreLog.worker_id == w.id,
            ScoreLog.change < 0,
            ScoreLog.reason.ilike('%reversal%'),
            ScoreLog.created_at >= cutoff_30
        ).count()
        total_actions = ScoreLog.query.filter(
            ScoreLog.worker_id == w.id,
            ScoreLog.source == 'discord',
            ScoreLog.created_at >= cutoff_30
        ).count()
        reversal_rate = reversals / max(total_actions, 1)

        # Voice
        voice_avg = db.session.query(db.func.avg(VoiceActivity.duration_seconds)).filter(
            VoiceActivity.discord_id == did,
            VoiceActivity.created_at >= cutoff_30
        ).scalar() or 0

        # Off-hour ratio
        total_msgs = MessageRecord.query.filter(
            MessageRecord.discord_id == did,
            MessageRecord.created_at >= cutoff_30
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
        daily_counts = db.session.query(
            db.func.date(MessageRecord.created_at).label('day'),
            db.func.count(MessageRecord.id).label('c')
        ).filter(
            MessageRecord.discord_id == did,
            MessageRecord.created_at >= cutoff_30
        ).group_by(db.func.date(MessageRecord.created_at)).all()
        if daily_counts:
            daily_vals = np.array([d.c for d in daily_counts])
            daily_mean = daily_vals.mean()
            daily_std = daily_vals.std() if len(daily_vals) > 1 else 0
            cv = daily_std / max(daily_mean, 0.1)
            consistency = max(0, min(100, (1 - min(cv, 1)) * 100))
        else:
            consistency = 50
            cv = 0.5

        vec = np.array([
            anomaly_freq,
            reversal_rate,
            min(1.0, voice_avg / 3600),
            min(1.0, total_actions / 50),
            off_hour_ratio,
            min(cv, 1.0),
            consistency / 100,
        ])
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
        MessageRecord.discord_id == did,
        MessageRecord.created_at >= cutoff_30
    ).count()
    if total_msgs_30 < 10:
        return None

    # 1. Activity consistency (inverted CV of daily counts)
    daily_counts = db.session.query(
        db.func.date(MessageRecord.created_at).label('day'),
        db.func.count(MessageRecord.id).label('c')
    ).filter(
        MessageRecord.discord_id == did,
        MessageRecord.created_at >= cutoff_30
    ).group_by(db.func.date(MessageRecord.created_at)).all()
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
        BehavioralAnomaly.discord_id == did,
        BehavioralAnomaly.detected_at >= cutoff_30
    ).count()
    anomaly_rate = min(10, anomaly_count) / 10.0

    # 4. Score trajectory (second half vs first half of 30-day window)
    recent_scores = ScoreLog.query.filter(
        ScoreLog.worker_id == worker_id,
        ScoreLog.created_at >= cutoff_30
    ).order_by(ScoreLog.created_at).all()
    if len(recent_scores) >= 4:
        mid = len(recent_scores) // 2
        first_half = sum(abs(s.change) for s in recent_scores[:mid])
        second_half = sum(abs(s.change) for s in recent_scores[mid:])
        if first_half + second_half > 0:
            score_trajectory = 1.0 if second_half > first_half else (0.0 if second_half < first_half else 0.5)
        else:
            score_trajectory = 0.5
    else:
        score_trajectory = 0.5

    # 5. Recent activity ratio (msgs/day last 7d / msgs/day last 30d)
    total_msgs_7 = MessageRecord.query.filter(
        MessageRecord.discord_id == did,
        MessageRecord.created_at >= cutoff_7
    ).count()
    daily_30 = total_msgs_30 / 30.0
    daily_7 = total_msgs_7 / 7.0
    recent_activity_ratio = min(2.0, daily_7 / max(daily_30, 0.1))

    return {
        'activity_consistency': round(activity_consistency, 4),
        'off_hours_ratio': round(off_hours_ratio, 4),
        'anomaly_rate': round(anomaly_rate, 4),
        'score_trajectory': round(score_trajectory, 4),
        'recent_activity_ratio': round(recent_activity_ratio, 4),
    }
