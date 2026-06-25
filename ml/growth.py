"""
Growth Prediction Module — Phase 3 + 4
Trains a simple model per guild on MemberJoinLeave hourly counts,
predicts next-7-day join/leave totals, and detects anomalous growth.
"""
import os
import json
import numpy as np
import joblib
from datetime import datetime, timedelta
from collections import defaultdict
from sklearn.ensemble import RandomForestRegressor

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')


def _model_path(guild_id, model_type):
    return os.path.join(MODELS_DIR, f'growth_{model_type}_{guild_id}.joblib')


def _hourly_counts(guild_id, days=90):
    """Return dicts of hourly join/leave counts over trailing N days."""
    from database import MemberJoinLeave
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = MemberJoinLeave.query.filter(
        MemberJoinLeave.guild_id == guild_id,
        MemberJoinLeave.created_at >= cutoff,
    ).with_entities(
        MemberJoinLeave.event_type,
        MemberJoinLeave.hour_of_day,
        MemberJoinLeave.day_of_week,
    ).all()
    joins = defaultdict(int)
    leaves = defaultdict(int)
    for event_type, hour, dow in rows:
        key = (hour, dow)
        if event_type == 'join':
            joins[key] += 1
        elif event_type == 'leave':
            leaves[key] += 1
    return joins, leaves


def _build_features(guild_id, days=90):
    """Build hourly feature matrix (hour sin/cos, dow sin/cos, lag_joins_24h, lag_leaves_24h)."""
    from database import MemberJoinLeave
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = MemberJoinLeave.query.filter(
        MemberJoinLeave.guild_id == guild_id,
        MemberJoinLeave.created_at >= cutoff,
    ).order_by(MemberJoinLeave.created_at).with_entities(
        MemberJoinLeave.event_type,
        MemberJoinLeave.hour_of_day,
        MemberJoinLeave.day_of_week,
        MemberJoinLeave.created_at,
    ).all()
    if not rows:
        return None, None, None

    # Build hourly time series
    hourly = {}
    for r in rows:
        ts = r.created_at.replace(minute=0, second=0, microsecond=0)
        if ts not in hourly:
            hourly[ts] = {'join': 0, 'leave': 0}
        if r.event_type == 'join':
            hourly[ts]['join'] += 1
        else:
            hourly[ts]['leave'] += 1

    sorted_ts = sorted(hourly.keys())
    X, y_join, y_leave = [], [], []
    for i, ts in enumerate(sorted_ts):
        h = ts.hour
        dow = ts.weekday()
        hour_sin = np.sin(2 * np.pi * h / 24)
        hour_cos = np.cos(2 * np.pi * h / 24)
        dow_sin = np.sin(2 * np.pi * dow / 7)
        dow_cos = np.cos(2 * np.pi * dow / 7)
        # Lag features: sum of joins/leaves in previous 24h window
        window_start = ts - timedelta(hours=24)
        lag_joins = sum(hourly[t]['join'] for t in sorted_ts if window_start <= t < ts)
        lag_leaves = sum(hourly[t]['leave'] for t in sorted_ts if window_start <= t < ts)
        X.append([hour_sin, hour_cos, dow_sin, dow_cos, lag_joins, lag_leaves])
        y_join.append(hourly[ts]['join'])
        y_leave.append(hourly[ts]['leave'])

    return np.array(X), np.array(y_join), np.array(y_leave)


def train(guild_id, days=90):
    """Train join & leave regressor models for a guild."""
    X, y_join, y_leave = _build_features(guild_id, days=days)
    if X is None or len(y_join) < 48:
        return {'status': 'skipped', 'reason': f'Only {len(y_join) if y_join is not None else 0} hourly data points'}
    os.makedirs(MODELS_DIR, exist_ok=True)
    results = {}
    for target, y, name in [('join', y_join, 'join'), ('leave', y_leave, 'leave')]:
        model = RandomForestRegressor(n_estimators=100, max_depth=8, min_samples_leaf=4, random_state=42, n_jobs=-1)
        model.fit(X, y)
        joblib.dump(model, _model_path(guild_id, name))
        score = float(model.score(X, y))
        results[name] = {'r2_score': round(score, 3), 'samples': len(y)}
    return {'status': 'trained', 'guild_id': guild_id, **results}


def predict_next_7d(guild_id):
    """Predict hourly join/leave counts for the next 7 days (168 hours)."""
    join_path = _model_path(guild_id, 'join')
    leave_path = _model_path(guild_id, 'leave')
    if not os.path.exists(join_path) or not os.path.exists(leave_path):
        return None
    join_model = joblib.load(join_path)
    leave_model = joblib.load(leave_path)

    now = datetime.utcnow()
    preds = []
    for offset in range(168):
        h = (now.hour + offset) % 24
        dow = (now.weekday() + (now.hour + offset) // 24) % 7
        hour_sin = np.sin(2 * np.pi * h / 24)
        hour_cos = np.cos(2 * np.pi * h / 24)
        dow_sin = np.sin(2 * np.pi * dow / 7)
        dow_cos = np.cos(2 * np.pi * dow / 7)
        # Use 0 for lag features (prediction mode — no actual prior)
        vec = np.array([[hour_sin, hour_cos, dow_sin, dow_cos, 0, 0]])
        pred_join = max(0, int(round(join_model.predict(vec)[0])))
        pred_leave = max(0, int(round(leave_model.predict(vec)[0])))
        preds.append({'hour': h, 'dow': dow, 'predicted_joins': pred_join, 'predicted_leaves': pred_leave})

    total_joins = sum(p['predicted_joins'] for p in preds)
    total_leaves = sum(p['predicted_leaves'] for p in preds)
    return {
        'hourly': preds,
        'total_predicted_joins': total_joins,
        'total_predicted_leaves': total_leaves,
        'net_growth': total_joins - total_leaves,
    }


def detect_anomalous_growth(guild_id, days=30, z_threshold=2.5):
    """Detect days with unusually high join or leave counts."""
    from database import MemberJoinLeave
    import statistics
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = MemberJoinLeave.query.filter(
        MemberJoinLeave.guild_id == guild_id,
        MemberJoinLeave.created_at >= cutoff,
    ).with_entities(
        MemberJoinLeave.event_type,
        MemberJoinLeave.created_at,
    ).all()
    daily = {}
    for event_type, ts in rows:
        day = ts.date()
        if day not in daily:
            daily[day] = {'joins': 0, 'leaves': 0}
        daily[day][event_type] += 1

    anomalies = []
    join_vals = [d['joins'] for d in daily.values()]
    leave_vals = [d['leaves'] for d in daily.values()]
    if len(join_vals) >= 7:
        j_mean, j_std = statistics.mean(join_vals), max(statistics.stdev(join_vals), 1)
        l_mean, l_std = statistics.mean(leave_vals), max(statistics.stdev(leave_vals), 1)
        for day, counts in daily.items():
            reasons = []
            if counts['joins'] > j_mean + z_threshold * j_std:
                reasons.append(f'join_spike ({counts["joins"]} vs avg {j_mean:.1f})')
            if counts['leaves'] > l_mean + z_threshold * l_std:
                reasons.append(f'leave_spike ({counts["leaves"]} vs avg {l_mean:.1f})')
            if reasons:
                anomalies.append({'date': day.isoformat(), 'joins': counts['joins'], 'leaves': counts['leaves'], 'reasons': reasons})
    return anomalies


def train_all_guilds(days=90):
    """Train growth models for all guilds."""
    from database import GuildInfo
    guilds = GuildInfo.query.all()
    results = []
    for g in guilds:
        r = train(g.guild_id, days=days)
        results.append({'guild_id': g.guild_id, 'name': g.name, **r})
    return results
