"""
Daily-total forecast model.
Predicts total daily message volume, then distributes across 24 hours
using the hourly proportion profile from recent data.

Public interface (unchanged):
    train(guild_id, days=30)
    predict_next_24h(guild_id)
    resolve_outcomes(days_back=7)
    get_accuracy_metrics(days=7)
    train_all_guilds(days=30)
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta

import joblib
import numpy as np
from sklearn.ensemble import RandomForestRegressor

from database import MessageRecord, PredictionLog, db
from ml import model_path
from ml.features import guild_daily_features

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")


def train(guild_id, days=30):
    """Train a daily total message count forecast model for a guild."""
    X, y, _ = guild_daily_features(guild_id, days=days)
    if X is None or len(y) < 7:
        return {
            "status": "skipped",
            "reason": f"Only {len(y) if y is not None else 0} daily data points (need >= 7)",
        }
    model = RandomForestRegressor(
        n_estimators=100,
        max_depth=10,
        min_samples_leaf=2,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X, y)
    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(model, model_path("forecast", guild_id))
    score = model.score(X, y)
    return {
        "status": "trained",
        "guild_id": guild_id,
        "r2_score": round(float(score), 3),
        "samples": len(y),
    }


def _hourly_profile_from_30d(guild_id):
    """Compute hourly proportion from last 30 days of actual data.
    Returns list of 24 floats summing to 1.0 (fractions of daily total).
    Falls back to GuildActivityBaseline or uniform if no data."""
    cutoff = datetime.utcnow() - timedelta(days=30)
    rows = (
        MessageRecord.query.filter(
            MessageRecord.guild_id == guild_id,
            MessageRecord.created_at >= cutoff,
            MessageRecord.hour_of_day != None,
        )
        .with_entities(MessageRecord.hour_of_day)
        .all()
    )
    if rows:
        hourly = defaultdict(int)
        for (hour,) in rows:
            hourly[hour] += 1
        total = sum(hourly.values())
        if total > 0:
            return [hourly.get(h, 0) / total for h in range(24)]

    # Fallback: GuildActivityBaseline all-time hourly_mean
    from database import GuildActivityBaseline

    baseline = GuildActivityBaseline.query.filter_by(guild_id=guild_id).first()
    if baseline and baseline.hourly_mean:
        total = sum(baseline.hourly_mean)
        if total > 0:
            return [count / total for count in baseline.hourly_mean]

    # Uniform fallback
    return [1.0 / 24] * 24


def predict_next_24h(guild_id, days=30):
    """Predict total messages for the next 24h, then distribute by hourly profile.
    Returns list of 24 ints (predicted counts per hour), or None if insufficient data.
    Logs 24 PredictionLog rows for chart compatibility."""
    path = model_path("forecast", guild_id)
    if not os.path.exists(path):
        return None

    model = joblib.load(path)
    now = datetime.utcnow()
    current_dow = now.weekday()

    # Build feature vector for the next full day
    dow_sin = np.sin(2 * np.pi * current_dow / 7)
    dow_cos = np.cos(2 * np.pi * current_dow / 7)

    # Get recent daily totals
    cutoff = now - timedelta(days=days)
    rows = (
        MessageRecord.query.filter(
            MessageRecord.guild_id == guild_id,
            MessageRecord.created_at >= cutoff,
        )
        .with_entities(MessageRecord.created_at)
        .all()
    )
    if not rows:
        return None

    daily_totals = defaultdict(int)
    day_dows = {}
    for (created,) in rows:
        daily_totals[created.date()] += 1
        day_dows[created.date()] = created.weekday()

    sorted_days = sorted(daily_totals.keys())
    counts_arr = np.array([daily_totals[d] for d in sorted_days])

    yesterday_total = float(counts_arr[-1]) if len(counts_arr) >= 1 else 0.0
    avg_3 = (
        float(counts_arr[-3:].mean())
        if len(counts_arr) >= 3
        else float(counts_arr.mean())
    )
    avg_7 = (
        float(counts_arr[-7:].mean())
        if len(counts_arr) >= 7
        else float(counts_arr.mean())
    )

    # Compute dow mean
    dow_sums = defaultdict(list)
    for d, count in zip(sorted_days, counts_arr):
        dow_sums[day_dows[d]].append(count)
    dow_means = {dw: float(np.mean(vals)) for dw, vals in dow_sums.items()}
    dow_avg = float(dow_means.get(current_dow, counts_arr.mean()))

    X_pred = np.array([[dow_sin, dow_cos, yesterday_total, avg_3, avg_7, dow_avg]])
    daily_pred = model.predict(X_pred)[0]
    daily_pred = max(0, int(round(daily_pred)))

    # If prediction is 0, fall back to dow_mean
    if daily_pred == 0:
        daily_pred = max(int(round(dow_avg)), 1)

    # Get hourly profile and distribute
    profile = _hourly_profile_from_30d(guild_id)
    preds = [max(0, int(round(daily_pred * profile[h]))) for h in range(24)]

    # Normalize distributed sum to match daily_pred
    total_dist = sum(preds)
    if total_dist > 0 and abs(total_dist - daily_pred) > 1:
        scale = daily_pred / total_dist
        preds = [max(0, int(round(p * scale))) for p in preds]
        # Fix rounding drift on the largest bucket
        diff = daily_pred - sum(preds)
        if diff != 0:
            max_idx = max(range(24), key=lambda i: preds[i])
            preds[max_idx] = max(0, preds[max_idx] + diff)

    # Log predictions for chart compatibility and outcome tracking
    _log_forecast_predictions(guild_id, preds, daily_pred, now)

    return preds


def _log_forecast_predictions(guild_id, preds, daily_pred, prediction_time):
    """Log 24 hourly predictions to PredictionLog.
    Each entry's prediction_value is the distributed hourly count;
    metadata stores the daily_total for daily resolution.
    A unique prediction_run_id groups all 24 logs from one run."""
    import uuid

    today_midnight = prediction_time.replace(hour=0, minute=0, second=0, microsecond=0)
    run_id = str(uuid.uuid4())[:8]

    log_entries = []
    for h in range(24):
        metadata = {
            "guild_id": guild_id,
            "predicted_hour": h,
            "predicted_dow": prediction_time.weekday(),
            "prediction_date": today_midnight.isoformat(),
            "daily_total": int(daily_pred),
            "prediction_run": run_id,
        }
        entry = PredictionLog(
            model_name="forecast",
            prediction_value=int(preds[h]),
            features_json=json.dumps({"hour": h, "daily_prediction": int(daily_pred)}),
            metadata_json=json.dumps(metadata),
            confidence=None,
            prediction_time=prediction_time,
            hour_error_history=json.dumps({}),
        )
        log_entries.append(entry)

    db.session.add_all(log_entries)
    db.session.commit()


def resolve_outcomes(days_back=7):
    """Resolve daily predictions vs actual daily totals.
    Groups pending hourly PredictionLog entries by (guild_id, prediction_date),
    compares the predicted daily total against the actual daily message count.
    Returns count of resolved entries."""
    cutoff = datetime.utcnow() - timedelta(hours=25)  # Must be >= 25h old
    pending = (
        PredictionLog.query.filter(
            PredictionLog.model_name == "forecast",
            PredictionLog.actual_value == None,
            PredictionLog.prediction_time <= cutoff,
        )
        .limit(500)
        .all()
    )
    print(f"[forecast] Found {len(pending)} pending predictions to resolve.")

    # Group by (guild_id, prediction_run) — each run is one prediction set
    by_run = defaultdict(list)
    for log in pending:
        meta = json.loads(log.metadata_json) if log.metadata_json else {}
        gid = meta.get("guild_id")
        run_id = meta.get("prediction_run")
        if gid and run_id:
            by_run[(gid, run_id)].append(log)

    resolved = 0
    for (gid, run_id), logs in by_run.items():
        meta_sample = json.loads(logs[0].metadata_json) if logs[0].metadata_json else {}
        pdate = meta_sample.get("prediction_date")
        try:
            day_start = datetime.fromisoformat(pdate)
        except (ValueError, TypeError):
            continue
        day_end = day_start + timedelta(hours=24)
        now = datetime.utcnow()
        if day_end > now:
            continue

        # Actual total messages for that day
        actual = MessageRecord.query.filter(
            MessageRecord.guild_id == gid,
            MessageRecord.created_at >= day_start,
            MessageRecord.created_at < day_end,
        ).count()

        # Predicted daily total = sum of all hourly predictions for the group
        predicted_total = sum(float(l.prediction_value or 0) for l in logs)

        # Resolve all hourly logs with the daily outcome
        for log in logs:
            log.actual_value = actual
            log.outcome_time = now
            if log.prediction_value is not None:
                pred_val = float(log.prediction_value)
                log.error_magnitude = abs(pred_val - actual)
                log.error_signed = pred_val - actual
                # Daily accuracy threshold: within 25% or 50 messages
                threshold = max(actual * 0.25, 50)
                log.was_correct = abs(predicted_total - actual) <= threshold
            resolved += 1

    db.session.commit()

    # Cross-model forecast error signal
    try:
        guild_errors = defaultdict(list)
        for log in pending:
            if log.error_signed is not None:
                meta = json.loads(log.metadata_json) if log.metadata_json else {}
                gid = meta.get("guild_id")
                if gid:
                    guild_errors[gid].append(log.error_signed)
        for gid, errors in guild_errors.items():
            mean_err = float(np.mean(errors))
            from database import UserBehaviorBaseline, Worker

            workers = Worker.query.filter(Worker.discord_id != None).all()
            for w in workers:
                baseline = UserBehaviorBaseline.query.filter_by(
                    discord_id=w.discord_id, guild_id=gid
                ).first()
                if baseline:
                    baseline.forecast_error_mean = round(mean_err, 4)
        db.session.commit()
    except Exception as e:
        print(f"[forecast] Cross-model signal update failed: {e}")

    return resolved


def get_accuracy_metrics(days=7):
    """Return daily accuracy metrics for forecast predictions over last N days.
    Groups by (guild_id, prediction_date) and reports per-day accuracy."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    logs = PredictionLog.query.filter(
        PredictionLog.model_name == "forecast",
        PredictionLog.actual_value != None,
        PredictionLog.prediction_time >= cutoff,
    ).all()

    if not logs:
        return {
            "total_predictions": 0,
            "resolved": 0,
            "mean_absolute_error": None,
            "mean_signed_error": None,
            "accuracy_pct": None,
            "samples": 0,
        }

    # Group by (guild_id, prediction_run) for daily accuracy
    by_run = defaultdict(list)
    for log in logs:
        meta = json.loads(log.metadata_json) if log.metadata_json else {}
        gid = meta.get("guild_id")
        run_id = meta.get("prediction_run")
        if gid and run_id:
            by_run[(gid, run_id)].append(log)

    daily_correct = 0
    daily_total = 0
    daily_errors = []

    for (gid, run_id), group in by_run.items():
        actual = float(group[0].actual_value or 0)
        predicted_total = sum(float(l.prediction_value or 0) for l in group)
        error = abs(predicted_total - actual)
        daily_errors.append(error)
        daily_total += 1
        threshold = max(actual * 0.25, 50)
        if error <= threshold:
            daily_correct += 1

    accuracy = round(daily_correct / daily_total * 100, 1) if daily_total > 0 else None
    mae = round(float(np.mean(daily_errors)), 2) if daily_errors else None

    return {
        "total_predictions": len(logs),
        "resolved": daily_total,
        "mean_absolute_error": mae,
        "mean_signed_error": None,
        "accuracy_pct": accuracy,
        "samples": daily_total,
    }


def train_all_guilds(days=30):
    """Train forecast models for all guilds with sufficient data."""
    from database import GuildInfo

    guilds = GuildInfo.query.all()
    results = []
    for g in guilds:
        result = train(g.guild_id, days=days)
        results.append({"guild_id": g.guild_id, "name": g.name, **result})
    return results
