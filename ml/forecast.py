"""
Daily-total forecast model with hourly error-profile correction.

Predicts total daily message volume using a RandomForestRegressor, then
distributes across 24 hours using the hourly proportion profile from
recent data. Applies corrections from the per-hour error profile (bias
and magnitude) to improve hourly accuracy over time.

Public interface:
    train(guild_id, days=30)
    predict_next_24h(guild_id, days=30, log_prediction=False, lead_bucket_hours=24)
    resolve_outcomes()                              # no params, returns int
    get_accuracy_metrics(days=7, guild_id=None)
    train_all_guilds(days=30)

    _build_error_profile(guild_id, days=30)         # hourly error profile
    _build_daily_error_profile(guild_id, days=30, lead_bucket_hours=None)

    Use log_prediction=True only for scheduled/bot runs that should
    create PredictionLog history for accuracy tracking.
    Page views should never log predictions.

Accuracy metric return shape:
    {
        "daily": {
            "accuracy_pct": float | None,
            "samples": int,                          # deduplicated daily runs
            "mean_absolute_error": float | None,
            "mean_signed_error": float | None,
            "tolerance": "max(actual*0.15, 25)",
            "by_lead_bucket": {
                "24": { "accuracy_pct": ..., "samples": ... },
                "18": { "accuracy_pct": ..., "samples": ... },
                "12": { "accuracy_pct": ..., "samples": ... },
                "6":  { "accuracy_pct": ..., "samples": ... },
            },
        },
        "hourly": {
            "accuracy_pct": float | None,
            "samples": int,                          # hourly rows
            "mean_absolute_error": float | None,
            "tolerance": "max(actual*0.25, 10)",
            "worst_hours": [
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
DAILY_CORRECTION_CAP = 0.35  # max 35% daily bias correction

DAILY_TOLERANCE_LABEL = "max(actual*0.15, 25)"
HOURLY_TOLERANCE_LABEL = "max(actual*0.25, 10)"

# ── Inactivity guard ─────────────────────────────────────────────────────
INACTIVITY_HOURS = 72  # guild with no messages in this window = inactive
INACTIVITY_DECAY_HOURS = 48  # if less messages than threshold, decay prediction


def _is_v2_log(log):
    """Check if a PredictionLog has v2 resolution markers.
    v2 logs have resolution_version=2 and actual_granularity="hourly"
    in their metadata, meaning each row's actual_value is the correct
    count for that specific hour (not a legacy daily total)."""
    if not log or not log.metadata_json:
        return False
    try:
        meta = (
            json.loads(log.metadata_json)
            if isinstance(log.metadata_json, str)
            else log.metadata_json
        )
        return (
            meta.get("resolution_version") == 2
            and meta.get("actual_granularity") == "hourly"
        )
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False


def _get_metadata(log):
    """Safely parse metadata_json from a PredictionLog row."""
    if log.metadata_json is None:
        return {}
    if isinstance(log.metadata_json, str):
        try:
            return json.loads(log.metadata_json)
        except (json.JSONDecodeError, TypeError):
            return {}
    if isinstance(log.metadata_json, dict):
        return log.metadata_json
    return {}


def _get_features(log):
    """Safely parse features_json from a PredictionLog row."""
    if log.features_json is None:
        return {}
    if isinstance(log.features_json, str):
        try:
            return json.loads(log.features_json)
        except (json.JSONDecodeError, TypeError):
            return {}
    if isinstance(log.features_json, dict):
        return log.features_json
    return {}


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


def _forecast_exists(guild_id, target_start, lead_bucket_hours):
    """Check if a forecast for (guild_id, target_day, lead_bucket) already exists.
    Filters by recent prediction_time (48h) since forecasts target the next calendar day,
    then checks metadata for exact match. No limit - scans all recent rows."""
    target_day_str = (
        target_start.strftime("%Y-%m-%d")
        if hasattr(target_start, "strftime")
        else str(target_start)[:10]
    )
    # Forecasts for tomorrow are made within the last 48 hours
    cutoff = datetime.utcnow() - timedelta(hours=48)
    rows = PredictionLog.query.filter(
        PredictionLog.model_name == "forecast",
        PredictionLog.prediction_time >= cutoff,
    ).all()
    for log in rows:
        meta = _get_metadata(log)
        if (
            meta.get("guild_id") == guild_id
            and meta.get("target_day") == target_day_str
            and meta.get("lead_bucket_hours") == lead_bucket_hours
        ):
            return True
    return False


def _check_inactivity(guild_id, hours=INACTIVITY_HOURS):
    """Check if a guild has been inactive.
    Returns (is_inactive: bool, recent_count: int, inactivity_factor: float).
    inactivity_factor is 0.0 if fully inactive, 1.0 if fully active,
    and a blend in between."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    recent_count = MessageRecord.query.filter(
        MessageRecord.guild_id == guild_id,
        MessageRecord.created_at >= cutoff,
    ).count()

    if recent_count == 0:
        return True, 0, 0.0

    # Check 48h decay window
    decay_cutoff = datetime.utcnow() - timedelta(hours=INACTIVITY_DECAY_HOURS)
    decay_count = MessageRecord.query.filter(
        MessageRecord.guild_id == guild_id,
        MessageRecord.created_at >= decay_cutoff,
    ).count()

    # Get 7-day average for comparison
    week_ago = datetime.utcnow() - timedelta(days=7)
    week_total = MessageRecord.query.filter(
        MessageRecord.guild_id == guild_id,
        MessageRecord.created_at >= week_ago,
    ).count()
    week_daily_avg = week_total / 7.0 if week_total > 0 else 1.0

    # If decay window has less than 10% of weekly avg, decay
    expected_decay = week_daily_avg * (INACTIVITY_DECAY_HOURS / 24.0)
    if expected_decay > 0 and decay_count < expected_decay * 0.1:
        # Blend: smoothly reduce prediction
        ratio = decay_count / max(expected_decay * 0.1, 1)
        factor = min(1.0, max(0.0, ratio))
        return False, recent_count, factor

    return False, recent_count, 1.0


def _build_daily_error_profile(guild_id, days=30, lead_bucket_hours=None):
    """Build daily bias correction from recently resolved forecasts.

    Deduplicates by case key (guild_id, target_day, lead_bucket_hours).
    If multiple prediction_run values exist for the same case (from old
    duplicate bugs), keeps only the latest completed run for that case.

    Returns a dict:
        {
            "predicted_total": float,   # mean predicted daily total across cases
            "actual_total": float,      # mean actual daily total across cases
            "signed_error": float,      # mean signed error (predicted - actual)
            "error_ratio": float,       # signed_error / max(actual_total, 1)
            "samples": int,             # number of unique cases
            "per_run_signed": [...],    # list of signed errors per case
        }
    Returns empty dict if insufficient data.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    logs = PredictionLog.query.filter(
        PredictionLog.model_name == "forecast",
        PredictionLog.actual_value != None,
        PredictionLog.prediction_time >= cutoff,
    ).all()

    # Step 1: Collect logs grouped by case (target_day, lead_bucket_hours)
    # then by prediction_run within each case.
    # A "case" represents one (guild_id, target_day, lead_bucket) forecast day.
    by_case = {}  # (target_day_str, lead) -> {run_id: {"pred_sum": ..., "actual_sum": ..., "max_pt": ...}}
    for log in logs:
        meta = _get_metadata(log)
        if meta.get("guild_id") != guild_id:
            continue
        if (
            meta.get("resolution_version") != 2
            or meta.get("actual_granularity") != "hourly"
        ):
            continue
        target_day = meta.get("target_day")
        lead = meta.get("lead_bucket_hours")
        run_id = meta.get("prediction_run")

        # Backward compat: derive target_day from target_start if missing
        if not target_day:
            ts = meta.get("target_start")
            if ts:
                target_day = str(ts)[:10]

        # Backward compat: default lead_bucket_hours to 24 if missing
        if lead is None:
            lead = 24

        if not (target_day and run_id):
            continue
        if lead_bucket_hours is not None and lead != lead_bucket_hours:
            continue

        case_key = (target_day, lead)
        if case_key not in by_case:
            by_case[case_key] = {}
        if run_id not in by_case[case_key]:
            by_case[case_key][run_id] = {
                "pred_sum": 0.0,
                "actual_sum": 0.0,
                "max_pt": log.prediction_time,
            }
        by_case[case_key][run_id]["pred_sum"] += float(log.prediction_value or 0)
        by_case[case_key][run_id]["actual_sum"] += float(log.actual_value or 0)
        by_case[case_key][run_id]["max_pt"] = max(
            by_case[case_key][run_id]["max_pt"], log.prediction_time
        )

    if not by_case:
        return {}

    # Step 2: For each case, keep only the latest prediction_run
    run_errors = []
    for case_key, runs in by_case.items():
        # Pick the run with the latest prediction_time
        best_run_id = max(runs.keys(), key=lambda rid: runs[rid]["max_pt"])
        best = runs[best_run_id]
        run_errors.append((best["pred_sum"], best["actual_sum"]))

    if len(run_errors) < 2:
        return {}

    predicted_vals = [p for p, a in run_errors]
    actual_vals = [a for p, a in run_errors]
    mean_pred = float(np.mean(predicted_vals))
    mean_actual = float(np.mean(actual_vals))
    signed_error = mean_pred - mean_actual
    error_ratio = signed_error / max(mean_actual, 1)
    per_run_signed = [p - a for p, a in run_errors]

    return {
        "predicted_total": round(mean_pred, 1),
        "actual_total": round(mean_actual, 1),
        "signed_error": round(signed_error, 1),
        "error_ratio": round(error_ratio, 4),
        "samples": len(run_errors),
        "per_run_signed": [round(e, 1) for e in per_run_signed],
    }


def _log_forecast_predictions(
    guild_id,
    preds,
    daily_pred_raw,
    daily_pred_corrected,
    prediction_time,
    lead_bucket_hours=24,
    daily_correction_factor=1.0,
):
    """Log 24 hourly predictions to PredictionLog.

    Each entry's prediction_value is the distributed hourly count;
    metadata stores the daily_total for daily resolution.
    A unique prediction_run_id groups all 24 logs from one run.
    target_start/target_end define the exact 24h window being predicted.

    Deduplicates: if a forecast for (guild_id, target_day, lead_bucket_hours)
    already exists, skips logging.

    Returns True if logs were written, False if duplicate (skipped).
    """
    now = prediction_time
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Target window: next calendar day (midnight tonight -> midnight tomorrow night)
    target_start = today_midnight + timedelta(days=1)
    target_end = target_start + timedelta(hours=24)
    target_day_str = target_start.strftime("%Y-%m-%d")

    # Dedup check
    if _forecast_exists(guild_id, target_start, lead_bucket_hours):
        return False

    run_id = str(uuid.uuid4())[:8]

    log_entries = []
    for h in range(24):
        metadata = {
            "guild_id": guild_id,
            "predicted_hour": h,
            "predicted_dow": now.weekday(),
            "prediction_date": today_midnight.isoformat(),
            "daily_total": int(daily_pred_corrected),
            "raw_daily_total": int(daily_pred_raw),
            "daily_correction_factor": round(daily_correction_factor, 4),
            "prediction_run": run_id,
            "target_start": target_start.isoformat(),
            "target_end": target_end.isoformat(),
            "target_day": target_day_str,
            "lead_bucket_hours": lead_bucket_hours,
            "resolution_version": 2,
            "actual_granularity": "hourly",
        }
        entry = PredictionLog(
            model_name="forecast",
            prediction_value=int(preds[h]),
            features_json=json.dumps(
                {"hour": h, "daily_prediction": int(daily_pred_corrected)}
            ),
            metadata_json=json.dumps(metadata),
            confidence=None,
            prediction_time=now,
            hour_error_history=json.dumps({}),
        )
        log_entries.append(entry)

    db.session.add_all(log_entries)
    db.session.commit()
    return True


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
        meta = _get_metadata(log)
        if meta.get("guild_id") != guild_id:
            continue
        if (
            meta.get("resolution_version") != 2
            or meta.get("actual_granularity") != "hourly"
        ):
            continue
        if log.actual_value is None or log.prediction_value is None:
            continue

        feature = _get_features(log)
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


def predict_next_24h(guild_id, days=30, log_prediction=False, lead_bucket_hours=24):
    """Predict total messages for the next 24h, then distribute by hourly profile.

    Two-layer correction:
      1. Daily volume correction: adjusts the raw daily total based on recent
         over/under prediction bias.
      2. Hourly shape correction: distributes the corrected daily total across
         24 hours and adjusts per-hour bias.

    Returns list of 24 ints (predicted counts per hour), or None if insufficient data.

    When log_prediction=True (scheduled/bot runs), logs 24 PredictionLog rows.
    When log_prediction=False (page views), returns predictions without writing to DB.
    """
    # ── Inactivity guard ──
    is_inactive, recent_count, inactivity_factor = _check_inactivity(guild_id)
    if is_inactive:
        # Return near-zero forecast: a tiny non-zero to avoid divide-by-zero,
        # but clearly signaling inactivity
        near_zero = [1] * 24  # 1 message per hour = 24/day, essentially inactive
        if log_prediction:
            _log_forecast_predictions(
                guild_id,
                near_zero,
                0,
                24,
                datetime.utcnow(),
                lead_bucket_hours=lead_bucket_hours,
                daily_correction_factor=0.0,
            )
        return near_zero

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

    # ── Apply inactivity blending ──
    if inactivity_factor < 1.0:
        # Blend toward recent actuals when activity is decaying
        recent_avg = (
            float(counts_arr[-3:].mean())
            if len(counts_arr) >= 3
            else float(counts_arr.mean())
        )
        # Weight recent actuals more heavily when activity pattern is changing
        blend_weight = 1.0 - inactivity_factor
        dow_avg = dow_avg * (1 - blend_weight * 0.5) + recent_avg * (blend_weight * 0.5)

    X_pred = np.array([[dow_sin, dow_cos, yesterday_total, avg_3, avg_7, dow_avg]])
    daily_pred_raw = model.predict(X_pred)[0]
    daily_pred_raw = max(0, int(round(daily_pred_raw)))

    # If prediction is 0, fall back to dow_mean
    if daily_pred_raw == 0:
        daily_pred_raw = max(int(round(dow_avg)), 1)

    # ── Layer 1: Daily volume bias correction ──
    daily_error_profile = _build_daily_error_profile(
        guild_id, days=30, lead_bucket_hours=lead_bucket_hours
    )
    daily_correction_factor = 1.0

    if daily_error_profile and daily_error_profile.get("samples", 0) >= 2:
        error_ratio = daily_error_profile["error_ratio"]
        # Apply capped correction
        correction = max(-DAILY_CORRECTION_CAP, min(DAILY_CORRECTION_CAP, -error_ratio))
        daily_correction_factor = 1.0 + correction
        daily_pred_corrected = max(
            1, int(round(daily_pred_raw * daily_correction_factor))
        )
    else:
        daily_pred_corrected = daily_pred_raw

    # ── Hourly distribution ──
    profile = _hourly_profile_from_30d(guild_id)
    preds = [max(0, int(round(daily_pred_corrected * profile[h]))) for h in range(24)]

    # Normalize distributed sum to match daily_pred_corrected
    total_dist = sum(preds)
    if total_dist > 0 and abs(total_dist - daily_pred_corrected) > 1:
        scale = daily_pred_corrected / total_dist
        preds = [max(0, int(round(p * scale))) for p in preds]
        diff = daily_pred_corrected - sum(preds)
        if diff != 0:
            max_idx = max(range(24), key=lambda i: preds[i])
            preds[max_idx] = max(0, preds[max_idx] + diff)

    # ── Layer 2: Hourly shape correction ──
    error_profile = _build_error_profile(guild_id, days=30)
    if error_profile:
        for h in range(24):
            info = error_profile.get(h, {})
            bias = info.get("bias", 0.0)
            if abs(bias) < 0.01:
                continue
            orig = float(preds[h])
            if bias > 0:
                # Consistently over-predicting this hour -> reduce
                reduction = min(bias, orig * CORRECTION_CAP)
                preds[h] = max(0, int(round(orig - reduction)))
            else:
                # Consistently under-predicting this hour -> increase
                increase = min(abs(bias), orig * CORRECTION_CAP)
                preds[h] = max(0, int(round(orig + increase)))

        # Re-normalize after correction (preserving daily total)
        total_dist = sum(preds)
        if total_dist > 0 and abs(total_dist - daily_pred_corrected) > 1:
            scale = daily_pred_corrected / total_dist
            preds = [max(0, int(round(p * scale))) for p in preds]
            diff = daily_pred_corrected - sum(preds)
            if diff != 0:
                max_idx = max(range(24), key=lambda i: preds[i])
                preds[max_idx] = max(0, preds[max_idx] + diff)

    if log_prediction:
        _log_forecast_predictions(
            guild_id,
            preds,
            daily_pred_raw,
            daily_pred_corrected,
            now,
            lead_bucket_hours=lead_bucket_hours,
            daily_correction_factor=daily_correction_factor,
        )

    return preds


def resolve_outcomes(days_back=7):
    """Resolve each hourly PredictionLog against its matching hour's actual count.

    Groups pending (unresolved) hourly predictions by (guild_id, prediction_run),
    queries actual hourly message counts from MessageRecord, and resolves each
    hourly row with its specific hour's actual value.

    Daily-level correctness and error metrics are stored in metadata_json.
    Hour-level correctness and error metrics are stored per-row.

    Returns count of resolved hourly entries.
    """
    cutoff = datetime.utcnow() - timedelta(hours=25)
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

    # Group by (guild_id, prediction_run)
    by_run = defaultdict(list)
    for log in pending:
        meta = _get_metadata(log)
        gid = meta.get("guild_id")
        run_id = meta.get("prediction_run")
        if gid and run_id:
            by_run[(gid, run_id)].append(log)

    resolved = 0
    for (gid, run_id), logs in by_run.items():
        meta_sample = _get_metadata(logs[0])
        target_start_str = meta_sample.get("target_start") or meta_sample.get(
            "prediction_date"
        )
        target_end_str = meta_sample.get("target_end")
        lead_bucket = meta_sample.get("lead_bucket_hours")
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

        # Daily totals for daily correctness
        daily_actual = sum(hourly_actuals.values())
        daily_predicted = sum(float(l.prediction_value or 0) for l in logs)
        daily_error_signed = daily_predicted - daily_actual
        daily_error_magnitude = abs(daily_error_signed)
        daily_tolerance = max(
            daily_actual * DAILY_TOLERANCE_FACTOR, DAILY_TOLERANCE_MIN
        )
        daily_correct = daily_error_magnitude <= daily_tolerance

        # Resolve each hourly log
        for log in logs:
            feature = _get_features(log)
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

            # Store full error profile
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
                "daily_error_signed": round(daily_error_signed, 1),
                "daily_error_magnitude": round(daily_error_magnitude, 1),
                "lead_bucket_hours": lead_bucket,
            }
            log.hour_error_history = json.dumps(err_history)
            # Stamp version marker
            log_meta = _get_metadata(log)
            log_meta["resolution_version"] = 2
            log_meta["actual_granularity"] = "hourly"
            log.metadata_json = json.dumps(log_meta)
            resolved += 1

        # Store daily-level correctness in the first log's metadata
        meta = _get_metadata(logs[0])
        meta["daily_correct"] = bool(daily_correct)
        meta["daily_tolerance"] = round(daily_tolerance, 2)
        meta["daily_tolerance_type"] = DAILY_TOLERANCE_LABEL
        meta["daily_error_signed"] = round(daily_error_signed, 1)
        meta["daily_error_magnitude"] = round(daily_error_magnitude, 1)
        meta["daily_actual_total"] = daily_actual
        meta["daily_predicted_total"] = int(round(daily_predicted))
        meta["lead_bucket_hours"] = lead_bucket
        meta["resolution_version"] = 2
        meta["actual_granularity"] = "hourly"
        logs[0].metadata_json = json.dumps(meta)

    db.session.commit()

    # Cross-model forecast error signal
    try:
        guild_errors = defaultdict(list)
        for log in pending:
            if log.error_signed is not None:
                meta = _get_metadata(log)
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
    Daily metrics also include per-lead-bucket breakdowns.

    Hourly metrics iterate every resolved PredictionLog and compare each
    individual hourly prediction against its matched hour's actual count.

    Args:
        days: Number of trailing days, or None for all-time.
        guild_id: If set, filter to this specific guild.

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

    # Filter to v2+ logs
    logs = [l for l in logs if _is_v2_log(l)]

    if not logs:
        return {
            "daily": {
                "accuracy_pct": None,
                "samples": 0,
                "mean_absolute_error": None,
                "mean_signed_error": None,
                "tolerance": DAILY_TOLERANCE_LABEL,
                "by_lead_bucket": {},
            },
            "hourly": {
                "accuracy_pct": None,
                "samples": 0,
                "mean_absolute_error": None,
                "tolerance": HOURLY_TOLERANCE_LABEL,
                "worst_hours": [],
            },
        }

    # ── Group by case (guild_id, target_day, lead_bucket_hours) for daily metrics ──
    # Each case represents one forecast day. If multiple prediction_run values
    # exist for the same case (from old duplicate bugs), keep only the latest.
    by_case = {}  # (gid, target_day, lead) -> {run_id: {"pred_sum": ..., "actual_sum": ..., "max_pt": ...}}
    for log in logs:
        meta = _get_metadata(log)
        gid = meta.get("guild_id")
        target_day = meta.get("target_day")
        lead = meta.get("lead_bucket_hours")
        run_id = meta.get("prediction_run")

        # Backward compat: derive target_day from target_start if missing
        if not target_day:
            ts = meta.get("target_start")
            if ts:
                target_day = str(ts)[:10]

        # Backward compat: default lead_bucket_hours to 24 if missing
        if lead is None:
            lead = 24

        if not (gid and target_day and run_id):
            continue
        if guild_id is not None and gid != guild_id:
            continue

        case_key = (gid, target_day, lead)
        if case_key not in by_case:
            by_case[case_key] = {}
        if run_id not in by_case[case_key]:
            by_case[case_key][run_id] = {
                "pred_sum": 0.0,
                "actual_sum": 0.0,
                "max_pt": log.prediction_time,
            }
        by_case[case_key][run_id]["pred_sum"] += float(log.prediction_value or 0)
        by_case[case_key][run_id]["actual_sum"] += float(log.actual_value or 0)
        by_case[case_key][run_id]["max_pt"] = max(
            by_case[case_key][run_id]["max_pt"], log.prediction_time
        )

    daily_correct = 0
    daily_total = 0
    daily_abs_errors = []
    daily_signed_errors = []
    by_lead = defaultdict(
        lambda: {"correct": 0, "total": 0, "abs_errors": [], "signed_errors": []}
    )

    # For each case, keep only the latest prediction_run
    for case_key, runs in by_case.items():
        gid, target_day, lead = case_key
        best_run_id = max(runs.keys(), key=lambda rid: runs[rid]["max_pt"])
        best = runs[best_run_id]
        pred_sum = best["pred_sum"]
        actual_sum = best["actual_sum"]
        error = pred_sum - actual_sum
        daily_abs_errors.append(abs(error))
        daily_signed_errors.append(error)
        daily_total += 1
        threshold = max(actual_sum * DAILY_TOLERANCE_FACTOR, DAILY_TOLERANCE_MIN)
        if abs(error) <= threshold:
            daily_correct += 1

        # Per-lead-bucket
        bucket_key = str(lead) if lead is not None else "unknown"
        by_lead[bucket_key]["total"] += 1
        by_lead[bucket_key]["abs_errors"].append(abs(error))
        by_lead[bucket_key]["signed_errors"].append(error)
        if abs(error) <= threshold:
            by_lead[bucket_key]["correct"] += 1

    # ── Hourly metrics ──
    hourly_correct = 0
    hourly_total = 0
    hourly_abs_errors = []
    hourly_by_hour = defaultdict(list)

    for log in logs:
        meta = _get_metadata(log)
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

        threshold = max(actual * HOURLY_TOLERANCE_FACTOR, HOURLY_TOLERANCE_MIN)
        if err <= threshold:
            hourly_correct += 1

        hour = None
        if log.features_json:
            try:
                f = json.loads(log.features_json)
                hour = f.get("hour")
            except (json.JSONDecodeError, TypeError):
                pass
        if hour is not None:
            hourly_by_hour[hour].append(pred - actual)

    # Build worst-hours list
    worst_hours = []
    for hour, signed_errors in hourly_by_hour.items():
        mae = float(np.mean([abs(e) for e in signed_errors]))
        mean_signed = float(np.mean(signed_errors))
        direction = (
            "over" if mean_signed > 0 else ("under" if mean_signed < 0 else "neutral")
        )
        worst_hours.append(
            {"hour": hour, "mean_absolute_error": round(mae, 2), "direction": direction}
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

    # Build by_lead_bucket metrics
    by_lead_bucket = {}
    for bucket_key, data in by_lead.items():
        b_total = data["total"]
        b_correct = data["correct"]
        b_acc = round(b_correct / b_total * 100, 1) if b_total > 0 else None
        b_mae = (
            round(float(np.mean(data["abs_errors"])), 2) if data["abs_errors"] else None
        )
        b_mse = (
            round(float(np.mean(data["signed_errors"])), 2)
            if data["signed_errors"]
            else None
        )
        by_lead_bucket[bucket_key] = {
            "accuracy_pct": b_acc,
            "samples": b_total,
            "mean_absolute_error": b_mae,
            "mean_signed_error": b_mse,
        }

    return {
        "daily": {
            "accuracy_pct": daily_acc,
            "samples": daily_total,
            "mean_absolute_error": daily_mae,
            "mean_signed_error": daily_mse,
            "tolerance": DAILY_TOLERANCE_LABEL,
            "by_lead_bucket": by_lead_bucket,
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
