"""
Daily-total forecast model with hourly error-profile correction.

Predicts total daily message volume using a RandomForestRegressor, then
distributes across 24 hours using the hourly proportion profile from
recent data. Applies corrections from the per-hour error profile (bias
and magnitude) to improve hourly accuracy over time.

Public interface:
    train(guild_id, days=30)
    predict_next_24h(guild_id, days=30, log_prediction=False)
    resolve_outcomes()                              # no params, returns int
    get_accuracy_metrics(days=7, guild_id=None)
    train_all_guilds(days=30)

    _build_error_profile(guild_id, days=30)         # internal helper

    Use log_prediction=True only for scheduled/bot runs that should
    create PredictionLog history for accuracy tracking.
    Page views should never log predictions.

Accuracy metric return shape:
    {
        "daily": {
            "accuracy_pct": float | None,
            "samples": int,                          # daily runs
            "mean_absolute_error": float | None,
            "mean_signed_error": float | None,
            "tolerance": "max(actual*0.15, 25)",
        },
        "hourly": {
            "accuracy_pct": float | None,
            "samples": int,                          # hourly rows
            "mean_absolute_error": float | None,
            "tolerance": "max(actual*0.25, 10)",
            "worst_hours": [                         # top 3 by MAE
                {"hour": int, "mean_absolute_error": float, "direction": str},
            ],
        },
    }
"""

import json
import os
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

import joblib
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sqlalchemy import func

from database import MessageRecord, PredictionLog, db
from ml import model_path
from ml.features import guild_daily_features

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")

# ── Tolerance constants ──────────────────────────────────────────────────
DAILY_TOLERANCE_FACTOR = 0.15  # 15% of actual daily total
DAILY_TOLERANCE_MIN = 25  # minimum 25 messages
HOURLY_TOLERANCE_FACTOR = 0.25  # 25% of actual hourly count
HOURLY_TOLERANCE_MIN = 10  # minimum 10 messages
CORRECTION_CAP = 0.30  # max 30% adjustment per hour from error profile

DAILY_TOLERANCE_LABEL = "max(actual*0.15, 25)"
HOURLY_TOLERANCE_LABEL = "max(actual*0.25, 10)"


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


def _log_forecast_predictions(guild_id, preds, daily_pred, prediction_time):
    """Log 24 hourly predictions to PredictionLog.
    Each entry's prediction_value is the distributed hourly count;
    metadata stores the daily_total for daily resolution.
    A unique prediction_run_id groups all 24 logs from one run.
    target_start/target_end define the exact 24h window being predicted."""
    now = prediction_time
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Target window: next full 24h period starting at midnight tonight
    target_start = today_midnight
    target_end = target_start + timedelta(hours=24)
    run_id = str(uuid.uuid4())[:8]

    log_entries = []
    for h in range(24):
        metadata = {
            "guild_id": guild_id,
            "predicted_hour": h,
            "predicted_dow": now.weekday(),
            "prediction_date": today_midnight.isoformat(),
            "daily_total": int(daily_pred),
            "prediction_run": run_id,
            "target_start": target_start.isoformat(),
            "target_end": target_end.isoformat(),
        }
        entry = PredictionLog(
            model_name="forecast",
            prediction_value=int(preds[h]),
            features_json=json.dumps({"hour": h, "daily_prediction": int(daily_pred)}),
            metadata_json=json.dumps(metadata),
            confidence=None,
            prediction_time=now,
            hour_error_history=json.dumps({}),
        )
        log_entries.append(entry)

    db.session.add_all(log_entries)
    db.session.commit()


def _build_error_profile(guild_id, days=30):
    """Build error profile from recent resolved predictions for a guild.

    For each hour 0-23, computes bias (mean signed error) and MAE.
    Returns dict of {hour: {"bias": float, "mae": float, "direction": str}}
    where direction is "over", "under", or "neutral".
    Returns empty dict if no resolved data is available.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    logs = PredictionLog.query.filter(
        PredictionLog.model_name == "forecast",
        PredictionLog.actual_value != None,
        PredictionLog.prediction_time >= cutoff,
    ).all()

    # Filter to this guild and group by hour
    by_hour = defaultdict(list)
    for log in logs:
        meta = json.loads(log.metadata_json) if log.metadata_json else {}
        if meta.get("guild_id") != guild_id:
            continue
        if log.actual_value is None or log.prediction_value is None:
            continue

        feature = json.loads(log.features_json) if log.features_json else {}
        hour = feature.get("hour")
        if hour is None:
            continue

        pred = float(log.prediction_value)
        actual = float(log.actual_value)
        by_hour[hour].append(pred - actual)

    if not by_hour:
        return {}

    profile = {}
    for h in range(24):
        if h not in by_hour or not by_hour[h]:
            profile[h] = {"bias": 0.0, "mae": 0.0, "direction": "neutral"}
            continue

        signed_errors = by_hour[h]
        bias = float(np.mean(signed_errors))
        mae = float(np.mean([abs(e) for e in signed_errors]))
        direction = "over" if bias > 0 else ("under" if bias < 0 else "neutral")
        profile[h] = {
            "bias": round(bias, 2),
            "mae": round(mae, 2),
            "direction": direction,
        }

    return profile


def predict_next_24h(guild_id, days=30, log_prediction=False):
    """Predict total messages for the next 24h, then distribute by hourly profile.

    Applies per-hour bias corrections from the error profile to improve
    hourly accuracy. Corrections are capped at 30% of each hour's prediction.

    Returns list of 24 ints (predicted counts per hour), or None if insufficient data.

    When log_prediction=True (scheduled/bot runs), logs 24 PredictionLog rows.
    When log_prediction=False (page views), returns predictions without writing to DB.
    """
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

    # ── Apply error-profile correction ──
    error_profile = _build_error_profile(guild_id, days=30)
    if error_profile:
        for h in range(24):
            info = error_profile.get(h, {})
            bias = info.get("bias", 0.0)
            if abs(bias) < 0.01:
                continue
            orig = float(preds[h])
            if bias > 0:
                # Consistently over-predicting this hour → reduce
                reduction = min(bias, orig * CORRECTION_CAP)
                preds[h] = max(0, int(round(orig - reduction)))
            else:
                # Consistently under-predicting this hour → increase
                increase = min(abs(bias), orig * CORRECTION_CAP)
                preds[h] = max(0, int(round(orig + increase)))

        # Re-normalize after correction
        total_dist = sum(preds)
        if total_dist > 0 and abs(total_dist - daily_pred) > 1:
            scale = daily_pred / total_dist
            preds = [max(0, int(round(p * scale))) for p in preds]
            diff = daily_pred - sum(preds)
            if diff != 0:
                max_idx = max(range(24), key=lambda i: preds[i])
                preds[max_idx] = max(0, preds[max_idx] + diff)

    if log_prediction:
        _log_forecast_predictions(guild_id, preds, daily_pred, now)

    return preds


def resolve_outcomes(days_back=7):
    """Resolve each hourly PredictionLog against its matching hour's actual count.

    Groups pending (unresolved) hourly predictions by (guild_id, prediction_run),
    queries actual hourly message counts from MessageRecord, and resolves each
    hourly row with its specific hour's actual value.

    Daily-level correctness is stored in the first log's metadata_json.
    Hour-level correctness and error metrics are stored per-row using the
    existing PredictionLog columns.

    Returns count of resolved hourly entries.
    """
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
        # Use target_start/target_end when available, fall back to prediction_date
        target_start_str = meta_sample.get("target_start") or meta_sample.get(
            "prediction_date"
        )
        target_end_str = meta_sample.get("target_end")
        try:
            day_start = datetime.fromisoformat(target_start_str)
            if target_end_str:
                day_end = datetime.fromisoformat(target_end_str)
            else:
                day_end = day_start + timedelta(hours=24)
        except (ValueError, TypeError):
            continue
        now = datetime.utcnow()
        if day_end > now:
            continue

        # Query actual hourly counts from MessageRecord
        hourly_actual_rows = (
            db.session.query(
                MessageRecord.hour_of_day,
                func.count(MessageRecord.id).label("cnt"),
            )
            .filter(
                MessageRecord.guild_id == gid,
                MessageRecord.created_at >= day_start,
                MessageRecord.created_at < day_end,
                MessageRecord.hour_of_day != None,
            )
            .group_by(MessageRecord.hour_of_day)
            .all()
        )
        hourly_actuals = {row.hour_of_day: row.cnt for row in hourly_actual_rows}

        # Compute daily totals for daily correctness check
        daily_actual = sum(hourly_actuals.values())
        daily_predicted = sum(float(l.prediction_value or 0) for l in logs)
        daily_error = abs(daily_predicted - daily_actual)
        daily_tolerance = max(
            daily_actual * DAILY_TOLERANCE_FACTOR, DAILY_TOLERANCE_MIN
        )
        daily_correct = daily_error <= daily_tolerance

        # Resolve each hourly log with its matching hour's actual count
        for log in logs:
            feature = json.loads(log.features_json) if log.features_json else {}
            hour = feature.get("hour", 0)
            actual_hourly = hourly_actuals.get(hour, 0)
            pred_hourly = float(log.prediction_value or 0)

            log.actual_value = actual_hourly
            log.outcome_time = now
            log.error_magnitude = abs(pred_hourly - actual_hourly)
            log.error_signed = pred_hourly - actual_hourly

            # Hourly correctness
            hourly_tolerance = max(
                actual_hourly * HOURLY_TOLERANCE_FACTOR, HOURLY_TOLERANCE_MIN
            )
            log.was_correct = log.error_magnitude <= hourly_tolerance

            # Store full error profile in hour_error_history
            err_history = {
                "hour": hour,
                "actual_hourly": actual_hourly,
                "predicted_hourly": int(round(pred_hourly)),
                "error_signed": float(log.error_signed),
                "error_magnitude": float(log.error_magnitude),
                "was_correct_hourly": bool(log.was_correct),
                "hourly_tolerance": round(hourly_tolerance, 2),
                "daily_total_actual": daily_actual,
                "daily_total_predicted": int(round(daily_predicted)),
                "daily_correct": bool(daily_correct),
            }
            log.hour_error_history = json.dumps(err_history)
            resolved += 1

        # Store daily-level correctness in the first log's metadata
        meta = json.loads(logs[0].metadata_json) if logs[0].metadata_json else {}
        meta["daily_correct"] = bool(daily_correct)
        meta["daily_tolerance"] = round(daily_tolerance, 2)
        meta["daily_tolerance_type"] = DAILY_TOLERANCE_LABEL
        logs[0].metadata_json = json.dumps(meta)

    db.session.commit()

    # Cross-model forecast error signal (kept from original)
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


def get_accuracy_metrics(days=7, guild_id=None):
    """Return daily and hourly accuracy metrics for forecast predictions.

    Daily metrics group by (guild_id, prediction_run) and compare the sum
    of hourly predictions against the sum of hourly actuals per run.
    Hourly metrics iterate every resolved PredictionLog and compare each
    individual hourly prediction against its matched hour's actual count.

    Args:
        days: Number of trailing days to consider, or None for all-time.
        guild_id: If set, filter metrics to this specific guild.

    Returns dict with nested "daily" and "hourly" metric dicts.
    """
    q = PredictionLog.query.filter(
        PredictionLog.model_name == "forecast",
        PredictionLog.actual_value != None,
    )
    if days is not None:
        cutoff = datetime.utcnow() - timedelta(days=days)
        q = q.filter(PredictionLog.prediction_time >= cutoff)
    logs = q.all()

    if not logs:
        return {
            "daily": {
                "accuracy_pct": None,
                "samples": 0,
                "mean_absolute_error": None,
                "mean_signed_error": None,
                "tolerance": DAILY_TOLERANCE_LABEL,
            },
            "hourly": {
                "accuracy_pct": None,
                "samples": 0,
                "mean_absolute_error": None,
                "tolerance": HOURLY_TOLERANCE_LABEL,
                "worst_hours": [],
            },
        }

    # ── Group by (guild_id, prediction_run) for daily metrics ──
    by_run = defaultdict(list)
    for log in logs:
        meta = json.loads(log.metadata_json) if log.metadata_json else {}
        gid = meta.get("guild_id")
        run_id = meta.get("prediction_run")
        if gid and run_id:
            if guild_id is not None and gid != guild_id:
                continue
            by_run[(gid, run_id)].append(log)

    daily_correct = 0
    daily_total = 0
    daily_abs_errors = []
    daily_signed_errors = []

    for (gid, run_id), group in by_run.items():
        pred_sum = sum(float(l.prediction_value or 0) for l in group)
        actual_sum = sum(float(l.actual_value or 0) for l in group)
        error = pred_sum - actual_sum
        daily_abs_errors.append(abs(error))
        daily_signed_errors.append(error)
        daily_total += 1
        threshold = max(actual_sum * DAILY_TOLERANCE_FACTOR, DAILY_TOLERANCE_MIN)
        if abs(error) <= threshold:
            daily_correct += 1

    # ── Hourly metrics ──
    hourly_correct = 0
    hourly_total = 0
    hourly_abs_errors = []
    hourly_by_hour = defaultdict(list)  # hour -> list of signed errors

    for log in logs:
        meta = json.loads(log.metadata_json) if log.metadata_json else {}
        gid = meta.get("guild_id")
        if guild_id is not None and gid != guild_id:
            continue
        if log.actual_value is None or log.prediction_value is None:
            continue

        pred = float(log.prediction_value)
        actual = float(log.actual_value)
        hourly_total += 1
        err = abs(pred - actual)
        hourly_abs_errors.append(err)

        # Hourly correctness
        threshold = max(actual * HOURLY_TOLERANCE_FACTOR, HOURLY_TOLERANCE_MIN)
        if err <= threshold:
            hourly_correct += 1

        # Extract hour for worst-hours tracking
        hour = None
        if log.features_json:
            try:
                f = json.loads(log.features_json)
                hour = f.get("hour")
            except (json.JSONDecodeError, TypeError):
                pass
        if hour is not None:
            hourly_by_hour[hour].append(pred - actual)

    # Build worst-hours list (top 3 by MAE)
    worst_hours = []
    for hour, signed_errors in hourly_by_hour.items():
        mae = float(np.mean([abs(e) for e in signed_errors]))
        mean_signed = float(np.mean(signed_errors))
        direction = (
            "over" if mean_signed > 0 else ("under" if mean_signed < 0 else "neutral")
        )
        worst_hours.append(
            {
                "hour": hour,
                "mean_absolute_error": round(mae, 2),
                "direction": direction,
            }
        )
    worst_hours.sort(key=lambda x: x["mean_absolute_error"], reverse=True)
    worst_hours = worst_hours[:3]

    # Compute aggregates
    daily_acc = round(daily_correct / daily_total * 100, 1) if daily_total > 0 else None
    daily_mae = round(float(np.mean(daily_abs_errors)), 2) if daily_abs_errors else None
    daily_mse = (
        round(float(np.mean(daily_signed_errors)), 2) if daily_signed_errors else None
    )

    hourly_acc = (
        round(hourly_correct / hourly_total * 100, 1) if hourly_total > 0 else None
    )
    hourly_mae = (
        round(float(np.mean(hourly_abs_errors)), 2) if hourly_abs_errors else None
    )

    return {
        "daily": {
            "accuracy_pct": daily_acc,
            "samples": daily_total,
            "mean_absolute_error": daily_mae,
            "mean_signed_error": daily_mse,
            "tolerance": DAILY_TOLERANCE_LABEL,
        },
        "hourly": {
            "accuracy_pct": hourly_acc,
            "samples": hourly_total,
            "mean_absolute_error": hourly_mae,
            "tolerance": HOURLY_TOLERANCE_LABEL,
            "worst_hours": worst_hours,
        },
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
