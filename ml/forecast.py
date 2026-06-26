import json
import os
from collections import defaultdict
from datetime import datetime, timedelta

import joblib
import numpy as np
from sklearn.ensemble import RandomForestRegressor

from database import MessageRecord, PredictionLog, db
from ml.features import guild_forecast_features

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")


def _model_path(guild_id):
    return os.path.join(MODELS_DIR, f"forecast_{guild_id}.joblib")


def _recent_hourly_counts(guild_id, hours_back=24):
    """Get actual message counts per hour for the last N hours."""
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    rows = (
        MessageRecord.query.filter(
            MessageRecord.guild_id == guild_id,
            MessageRecord.created_at >= cutoff,
            MessageRecord.hour_of_day != None,
        )
        .with_entities(MessageRecord.hour_of_day, MessageRecord.created_at)
        .all()
    )
    hourly = defaultdict(int)
    for hour, created in rows:
        hourly[created.hour] += 1
    return hourly


def _live_rolling_averages(guild_id):
    """Compute rolling avg message counts from actual recent data (6h, 12h, 24h)."""
    now = datetime.utcnow()
    buckets = {"6h": 6, "12h": 12, "24h": 24}
    # Get message counts for last 24h
    cutoff = now - timedelta(hours=24)
    rows = MessageRecord.query.filter(
        MessageRecord.guild_id == guild_id,
        MessageRecord.created_at >= cutoff,
        MessageRecord.hour_of_day != None,
    ).all()
    if not rows:
        return 0, 0, 0
    # Bucket by hour using hour_of_day field for consistency
    hourly = defaultdict(int)
    for r in rows:
        hourly[r.hour_of_day] += 1
    # Compute averages over each window
    now_hour = now.hour
    avg_6 = sum(hourly[(now_hour - i) % 24] for i in range(6)) / 6.0
    avg_12 = sum(hourly[(now_hour - i) % 24] for i in range(12)) / 12.0
    avg_24 = sum(hourly[(now_hour - i) % 24] for i in range(24)) / 24.0
    return avg_6, avg_12, avg_24


def train(guild_id, days=30):
    """Train an hourly activity forecast model for a guild."""
    X, y, _ = guild_forecast_features(guild_id, days=days)
    if X is None or len(y) < 48:
        return {
            "status": "skipped",
            "reason": f"Only {len(y) if y is not None else 0} hourly data points",
        }
    model = RandomForestRegressor(
        n_estimators=100,
        max_depth=10,
        min_samples_leaf=4,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X, y)
    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(model, _model_path(guild_id))
    score = model.score(X, y)
    return {
        "status": "trained",
        "guild_id": guild_id,
        "r2_score": round(float(score), 3),
        "samples": len(y),
    }


def predict_next_24h(guild_id, days=30):
    """Predict message counts for the next 24 hours.
    Returns array of shape (24,) with predicted counts per hour.
    Also logs each hourly prediction to PredictionLog for later outcome resolution."""
    path = _model_path(guild_id)
    if not os.path.exists(path):
        return None
    model = joblib.load(path)

    now = datetime.utcnow()
    current_dow = now.weekday()

    # Use LIVE rolling averages from actual recent data
    avg_6, avg_12, avg_24 = _live_rolling_averages(guild_id)
    last_counts = np.array([avg_6, avg_12, avg_24])

    # Build 24 hourly feature vectors predicting today's hours (0-23)
    X_pred = []
    for h in range(24):
        hour_sin = np.sin(2 * np.pi * h / 24)
        hour_cos = np.cos(2 * np.pi * h / 24)
        dow_sin = np.sin(2 * np.pi * current_dow / 7)
        dow_cos = np.cos(2 * np.pi * current_dow / 7)
        X_pred.append(
            [
                hour_sin,
                hour_cos,
                dow_sin,
                dow_cos,
                last_counts[0],
                last_counts[1],
                last_counts[2],
            ]
        )
    X_pred = np.array(X_pred)
    preds = model.predict(X_pred)
    preds = np.maximum(0, np.round(preds)).astype(int)

    # Load error history and adjust predictions
    error_history = _load_hour_error_history(guild_id)
    adjusted_preds = []
    for h, pred in enumerate(preds):
        avg_error = error_history.get(str(h), 0)
        adjusted_pred = max(0, int(pred + avg_error))
        adjusted_preds.append(adjusted_pred)

    # Log predictions for outcome tracking
    _log_forecast_predictions(guild_id, adjusted_preds, X_pred, now)

    return adjusted_preds


def _load_hour_error_history(guild_id):
    """Load recent error history for a guild."""
    logs = (
        PredictionLog.query.filter(
            PredictionLog.model_name == "forecast",
            PredictionLog.metadata_json.contains(f'"guild_id": {guild_id}'),
            PredictionLog.error_signed != None,
        )
        .order_by(PredictionLog.prediction_time.desc())
        .limit(100)
        .all()
    )

    history = defaultdict(list)
    for log in logs:
        meta = json.loads(log.metadata_json)
        hour = meta.get("predicted_hour")
        if hour is not None and log.error_signed is not None:
            history[str(hour)].append(log.error_signed)

    # Compute average error per hour
    avg_errors = {}
    for hour, errors in history.items():
        if errors:
            avg_errors[hour] = sum(errors) / len(errors)
    return avg_errors


def _update_hour_error_history(guild_id, hour, error):
    """Update error history for a guild and hour."""
    # Load existing history
    logs = (
        PredictionLog.query.filter(
            PredictionLog.model_name == "forecast",
            PredictionLog.metadata_json.contains(f'"guild_id": {guild_id}'),
            PredictionLog.hour_error_history != None,
        )
        .order_by(PredictionLog.prediction_time.desc())
        .limit(1)
        .all()
    )

    history = defaultdict(list)
    if logs:
        history = json.loads(logs[0].hour_error_history) or defaultdict(list)

    # Update history (keep last 7 errors)
    history[str(hour)].append(error)
    if len(history[str(hour)]) > 7:
        history[str(hour)] = history[str(hour)][-7:]

    # Save back to DB
    for log in logs:
        log.hour_error_history = json.dumps(history)
    db.session.commit()


def _log_forecast_predictions(guild_id, preds, X_pred, prediction_time):
    """Log 24 hourly predictions to PredictionLog."""
    today = prediction_time.replace(hour=0, minute=0, second=0, microsecond=0)
    features_list = X_pred.tolist() if hasattr(X_pred, "tolist") else X_pred

    log_entries = []
    for h in range(24):
        metadata = {
            "guild_id": guild_id,
            "predicted_hour": h,
            "predicted_dow": prediction_time.weekday(),
            "prediction_date": today.isoformat(),
        }

        entry = PredictionLog(
            model_name="forecast",
            prediction_value=int(preds[h]),
            features_json=json.dumps(features_list[h]),
            metadata_json=json.dumps(metadata),
            confidence=None,
            prediction_time=prediction_time,
            hour_error_history=json.dumps({}),  # Initialize as empty JSON
        )
        log_entries.append(entry)

    db.session.add_all(log_entries)
    db.session.commit()


def resolve_outcomes(days_back=7):
    """Compare past predictions to actual message counts.
    Scans unresolved PredictionLog entries where outcome_time > 1h ago,
    looks up actual counts from MessageRecord, and fills in results.
    Returns count of resolved entries."""
    cutoff = datetime.utcnow() - timedelta(
        hours=1
    )  # Resolve predictions older than 1 hour
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

    resolved = 0
    for log in pending:
        meta = json.loads(log.metadata_json) if log.metadata_json else {}
        guild_id = meta.get("guild_id")
        pred_hour = meta.get("predicted_hour")
        pred_date = meta.get("prediction_date")
        if not guild_id or pred_hour is None or not pred_date:
            print(
                f"[forecast] Skipping prediction {log.id}: missing metadata (guild_id={guild_id}, pred_hour={pred_hour}, pred_date={pred_date})"
            )
            continue

        # Query actual messages in that hour
        try:
            day_start = datetime.fromisoformat(pred_date)
        except (ValueError, TypeError):
            continue
        hour_start = day_start.replace(hour=pred_hour)
        hour_end = hour_start + timedelta(hours=1)
        now = datetime.utcnow()
        if hour_end > now:
            continue  # Not yet resolvable

        actual = MessageRecord.query.filter(
            MessageRecord.guild_id == guild_id,
            MessageRecord.created_at >= hour_start,
            MessageRecord.created_at < hour_end,
        ).count()

        log.actual_value = actual
        log.outcome_time = now
        if log.prediction_value is not None:
            pred_val = float(log.prediction_value)
            log.error_magnitude = abs(pred_val - actual)
            log.error_signed = pred_val - actual
            # Correct if within 50% of actual (or within 2 messages for very low counts)
            threshold = max(actual * 0.5, 2)
            log.was_correct = abs(pred_val - actual) <= threshold

            # Update error history for feedback loop
            print(
                f"[forecast] Updating error history for guild={guild_id}, hour={pred_hour}, error={log.error_signed}"
            )
            _update_hour_error_history(guild_id, pred_hour, log.error_signed)
        resolved += 1

    db.session.commit()
    return resolved


def get_accuracy_metrics(days=7):
    """Return accuracy metrics for forecast predictions over last N days."""
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

    errors = []
    correct = 0
    for log in logs:
        if log.error_magnitude is not None:
            errors.append(log.error_magnitude)
            if log.was_correct:
                correct += 1

    return {
        "total_predictions": len(logs),
        "resolved": len(logs),
        "mean_absolute_error": round(float(np.mean(errors)), 2) if errors else None,
        "mean_signed_error": round(
            float(
                np.mean([l.error_signed for l in logs if l.error_signed is not None])
            ),
            2,
        ),
        "accuracy_pct": round(correct / len(logs) * 100, 1) if logs else None,
        "samples": len(logs),
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


def log_anomaly_prediction(discord_id, score, is_anomaly, severity):
    """Log an anomaly prediction to PredictionLog."""
    try:
        import json
        from datetime import datetime

        from database import PredictionLog, db

        entry = PredictionLog(
            model_name="anomaly",
            prediction_value=float(score),
            confidence=float(min(1.0, max(0.0, abs(score) / max(abs(-0.15), 0.01)))),
            metadata_json=json.dumps(
                {
                    "discord_id": discord_id,
                    "is_anomaly": is_anomaly,
                    "severity": severity,
                    "threshold": -0.15,
                }
            ),
            prediction_time=datetime.utcnow(),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        print(f"[forecast] Anomaly prediction log failed: {e}")


def log_burnout_prediction(
    discord_id, worker_id, raw_score, burnout_score, is_flagged, signals
):
    """Log a burnout prediction to PredictionLog."""
    try:
        import json
        from datetime import datetime

        from database import PredictionLog, db

        entry = PredictionLog(
            model_name="burnout",
            prediction_value=float(burnout_score),
            confidence=float(min(1.0, max(0.0, abs(raw_score) / max(abs(0.0), 0.01)))),
            metadata_json=json.dumps(
                {
                    "discord_id": discord_id,
                    "worker_id": worker_id,
                    "is_flagged": is_flagged,
                    "raw_score": round(raw_score, 4),
                    "signals": signals,
                    "threshold": 0.0,
                }
            ),
            prediction_time=datetime.utcnow(),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        print(f"[forecast] Burnout prediction log failed: {e}")


def log_corrector_prediction(worker_id, original_score, corrected_score, reason):
    """Log a corrector prediction to PredictionLog."""
    try:
        import json
        from datetime import datetime

        from database import PredictionLog, db

        entry = PredictionLog(
            model_name="corrector",
            prediction_value=float(corrected_score - original_score),
            features_json=json.dumps(
                {
                    "worker_id": worker_id,
                    "original_score": original_score,
                    "corrected_score": corrected_score,
                    "reason": reason,
                }
            ),
            metadata_json=json.dumps(
                {
                    "worker_id": worker_id,
                    "original_score": original_score,
                    "corrected_score": corrected_score,
                    "reason": reason,
                }
            ),
            prediction_time=datetime.utcnow(),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        print(f"[forecast] Corrector prediction log failed: {e}")
