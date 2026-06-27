"""
Burnout Detection — Weighted Signal Scoring
Replaces IsolationForest (invalid for small staff counts <50).
Uses deterministic weighted signals that are explainable and stable.
Signals grow in accuracy as UserBehaviorBaseline accumulates data.
"""

import json
import os
from datetime import datetime, timedelta

import numpy as np

from database import PredictionLog, db

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
BURNOUT_MODEL_PATH = os.path.join(MODELS_DIR, "burnout_iforest.joblib")
BURNOUT_THRESHOLD = 0.0  # kept for compatibility

# Signal weights — must sum to 1.0
SIGNAL_WEIGHTS = {
    "anomaly_freq": 0.25,  # most important — ML-detected anomalies
    "volume_drop": 0.20,  # sudden drop in activity vs baseline
    "reversal_rate": 0.15,  # erroneous moderation actions
    "voice_creep": 0.10,  # unusual voice channel hours
    "off_hours_spike": 0.10,  # activity shifting to off-hours vs baseline
    "pattern_drift": 0.10,  # behavioral pattern changed vs long-term
    "correction_rate": 0.10,  # admins frequently correcting this person
}

BURNOUT_FLAG_THRESHOLD = 0.40  # score >= 0.40 = flagged


def _correction_rate(worker_id, days=30):
    """Return correction frequency as a burnout signal."""
    from database import AdminCorrection, ScoreLog

    cutoff = datetime.utcnow() - timedelta(days=days)
    corr_count = AdminCorrection.query.filter(
        AdminCorrection.worker_id == worker_id, AdminCorrection.created_at >= cutoff
    ).count()
    action_count = ScoreLog.query.filter(
        ScoreLog.worker_id == worker_id, ScoreLog.created_at >= cutoff
    ).count()
    return min(1.0, corr_count / max(action_count, 1))


def _compute_signal_scores(discord_id, worker_id, days=30):
    """Compute all 7 signal scores for a staff member.
    Returns dict of signal_name -> float (0.0-1.0) and list of triggered signals."""
    from database import (
        BehavioralAnomaly,
        MessageRecord,
        ScoreLog,
        UserBehaviorBaseline,
        VoiceActivity,
    )

    cutoff = datetime.utcnow() - timedelta(days=days)
    signals = {}
    triggered = []

    # 1. anomaly_freq — how many ML anomalies in last 30d
    anomaly_count = BehavioralAnomaly.query.filter(
        BehavioralAnomaly.discord_id == discord_id,
        BehavioralAnomaly.detected_at >= cutoff,
        BehavioralAnomaly.anomaly_type == "ml_anomaly",
    ).count()
    signals["anomaly_freq"] = min(1.0, anomaly_count / 5.0)
    if signals["anomaly_freq"] >= 0.4:
        triggered.append("frequent_anomalies")

    # 2. volume_drop — compare recent vs baseline
    baseline = (
        UserBehaviorBaseline.query.filter_by(discord_id=discord_id)
        .order_by(UserBehaviorBaseline.updated_at.desc())
        .first()
    )

    if baseline and baseline.mean_daily_msgs_90d and baseline.mean_daily_msgs_90d > 0:
        recent_msgs = MessageRecord.query.filter(
            MessageRecord.discord_id == discord_id,
            MessageRecord.created_at >= cutoff,
        ).count()
        recent_daily = recent_msgs / max(days, 1)
        drop_ratio = 1.0 - (recent_daily / baseline.mean_daily_msgs_90d)
        signals["volume_drop"] = max(0.0, min(1.0, drop_ratio))
        if signals["volume_drop"] >= 0.4:
            triggered.append("volume_volatility")
    else:
        signals["volume_drop"] = 0.0

    # 3. reversal_rate — erroneous mod actions
    reversals = ScoreLog.query.filter(
        ScoreLog.worker_id == worker_id,
        ScoreLog.change < 0,
        ScoreLog.reason.ilike("%reversal%"),
        ScoreLog.created_at >= cutoff,
    ).count()
    total_actions = ScoreLog.query.filter(
        ScoreLog.worker_id == worker_id,
        ScoreLog.source == "discord",
        ScoreLog.created_at >= cutoff,
    ).count()
    signals["reversal_rate"] = min(1.0, reversals / max(total_actions, 1) * 5)
    if signals["reversal_rate"] >= 0.4:
        triggered.append("increasing_reversals")

    # 4. voice_creep — unusual voice hours vs baseline
    voice_sessions = (
        VoiceActivity.query.filter(
            VoiceActivity.discord_id == discord_id,
            VoiceActivity.created_at >= cutoff,
        )
        .with_entities(VoiceActivity.duration_seconds)
        .all()
    )
    if voice_sessions:
        avg_duration = np.mean([v[0] for v in voice_sessions if v[0]])
        # Flag if avg session > 4 hours (14400s) — unhealthy work pattern
        signals["voice_creep"] = min(1.0, avg_duration / 14400)
    else:
        signals["voice_creep"] = 0.0
    if signals["voice_creep"] >= 0.5:
        triggered.append("voice_creep")

    # 5. off_hours_spike — activity drifting to off-hours vs baseline
    if baseline and baseline.off_hours_ratio_90d is not None:
        total_recent = MessageRecord.query.filter(
            MessageRecord.discord_id == discord_id,
            MessageRecord.created_at >= cutoff,
        ).count()
        off_recent = MessageRecord.query.filter(
            MessageRecord.discord_id == discord_id,
            MessageRecord.created_at >= cutoff,
            MessageRecord.hour_of_day != None,
            ~MessageRecord.hour_of_day.between(9, 16),
        ).count()
        current_off_ratio = off_recent / max(total_recent, 1)
        spike = current_off_ratio - baseline.off_hours_ratio_90d
        signals["off_hours_spike"] = max(0.0, min(1.0, spike * 3))
    else:
        signals["off_hours_spike"] = 0.0
    if signals["off_hours_spike"] >= 0.4:
        triggered.append("off_hours_pattern")

    # 6. pattern_drift — behavioral pattern changed (from baseline)
    if baseline and baseline.pattern_drift is not None:
        signals["pattern_drift"] = min(1.0, baseline.pattern_drift)
    else:
        signals["pattern_drift"] = 0.0
    if signals["pattern_drift"] >= 0.3:
        triggered.append("pattern_drift")

    # 7. correction_rate — admin keeps fixing this person's scores
    signals["correction_rate"] = _correction_rate(worker_id, days)
    if signals["correction_rate"] >= 0.3:
        triggered.append("frequent_corrections")

    return signals, triggered


def _compute_burnout_score(signal_scores):
    """Weighted sum of signal scores -> burnout score 0.0-1.0."""
    total = 0.0
    for signal, weight in SIGNAL_WEIGHTS.items():
        total += float(signal_scores.get(signal, 0.0)) * weight
    return round(total, 4)


def _log_burnout_prediction(
    discord_id, worker_id, raw_score, burnout_score, is_flagged, signals
):
    try:
        entry = PredictionLog(
            model_name="burnout",
            prediction_value=float(burnout_score),
            confidence=float(burnout_score),
            metadata_json=json.dumps(
                {
                    "discord_id": discord_id,
                    "worker_id": worker_id,
                    "is_flagged": is_flagged,
                    "raw_score": round(raw_score, 4),
                    "signals": signals,
                    "threshold": BURNOUT_FLAG_THRESHOLD,
                    "method": "weighted_signal_scoring",
                }
            ),
            prediction_time=datetime.utcnow(),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        print(f"[burnout] PredictionLog write failed: {e}")


def train(contamination=0.1, days=30):
    """No-op for weighted scoring — kept for API compatibility.
    Returns status indicating weighted scoring is active."""
    return {
        "status": "weighted_scoring_active",
        "method": "deterministic_weighted_signals",
        "note": "IsolationForest replaced — no training needed for weighted scoring",
        "threshold": BURNOUT_FLAG_THRESHOLD,
        "signals": list(SIGNAL_WEIGHTS.keys()),
    }


def score_worker(discord_id, days=30):
    """Score a single worker for burnout risk using weighted signals."""
    from database import BurnoutRisk
    from database import Worker as WorkerModel

    worker = WorkerModel.query.filter_by(discord_id=discord_id).first()
    if not worker:
        return None

    signal_scores, triggered = _compute_signal_scores(discord_id, worker.id, days)
    burnout_score_float = _compute_burnout_score(signal_scores)
    burnout_score_int = int(burnout_score_float * 100)
    is_flagged = burnout_score_float >= BURNOUT_FLAG_THRESHOLD

    result = {
        "burnout_score": burnout_score_int,
        "burnout_score_float": burnout_score_float,
        "is_flagged": is_flagged,
        "raw_anomaly_score": burnout_score_float,
        "signals": triggered,
        "signal_scores": signal_scores,
        "threshold": BURNOUT_FLAG_THRESHOLD,
        "method": "weighted_signal_scoring",
    }

    _log_burnout_prediction(
        discord_id=discord_id,
        worker_id=worker.id,
        raw_score=burnout_score_float,
        burnout_score=burnout_score_int,
        is_flagged=is_flagged,
        signals=triggered,
    )

    if is_flagged:
        existing = BurnoutRisk.query.filter_by(discord_id=discord_id).first()
        signals_str = json.dumps(triggered)
        if existing:
            existing.score = float(burnout_score_int)
            existing.signals = signals_str
            existing.detected_at = datetime.utcnow()
        else:
            db.session.add(
                BurnoutRisk(
                    worker_id=worker.id,
                    discord_id=discord_id,
                    name=worker.name,
                    score=float(burnout_score_int),
                    signals=signals_str,
                    detected_at=datetime.utcnow(),
                )
            )
        db.session.commit()

        # Slack notification for flagged workers (fire-and-forget)
        try:
            from database import WorkerIdentity
            from services.slack import notify_burnout_flagged

            ident = WorkerIdentity.query.filter_by(discord_id=discord_id).first()
            if ident and ident.worker_id:
                notify_burnout_flagged(
                    worker_name=worker.name,
                    burnout_score=burnout_score_int,
                    signals=triggered,
                    worker_id=ident.worker_id,
                )
        except Exception:
            pass

    # Update cross-model signal in UserBehaviorBaseline
    try:
        from database import UserBehaviorBaseline

        baseline = UserBehaviorBaseline.query.filter_by(discord_id=discord_id).first()
        if baseline:
            baseline.recent_burnout_score = float(burnout_score_float)
            db.session.commit()
    except Exception:
        pass

    return result


def scan_all(days=30):
    """Score ALL staff members and return flagged workers with full signal data."""
    from database import Worker as WorkerModel

    workers = WorkerModel.query.filter(WorkerModel.discord_id != None).all()
    results = []
    for w in workers:
        try:
            result = score_worker(w.discord_id, days=days)
            if result and result["is_flagged"]:
                results.append(
                    {
                        "worker_id": w.id,
                        "discord_id": w.discord_id,
                        "name": w.name,
                        "burnout_score": result["burnout_score"],
                        "raw_score": result["burnout_score_float"],
                        "signals": result["signals"],
                        "signal_scores": result["signal_scores"],
                    }
                )
        except Exception as e:
            print(f"[burnout] scan_all error for {w.name}: {e}")
    return results


def get_precision_recall(days=30):
    from database import BurnoutRisk

    cutoff = datetime.utcnow() - timedelta(days=days)
    with_feedback = BurnoutRisk.query.filter(
        BurnoutRisk.feedback != None,
        BurnoutRisk.detected_at >= cutoff,
    ).all()
    if not with_feedback:
        return {
            "total_with_feedback": 0,
            "confirmed": 0,
            "dismissed": 0,
            "precision": None,
        }
    confirmed = sum(1 for b in with_feedback if b.feedback == "confirmed")
    dismissed = sum(1 for b in with_feedback if b.feedback == "dismissed")
    total = len(with_feedback)
    precision = (
        round(confirmed / max(confirmed + dismissed, 1), 3)
        if (confirmed + dismissed) > 0
        else None
    )
    return {
        "total_with_feedback": total,
        "confirmed": confirmed,
        "dismissed": dismissed,
        "precision": precision,
        "precision_pct": round(precision * 100, 1) if precision is not None else None,
        "method": "weighted_signal_scoring",
    }


def resolve_burnout_outcomes(days_back=30):
    """Resolve pending burnout predictions against admin feedback on BurnoutRisk."""
    from database import BurnoutRisk

    cutoff = datetime.utcnow() - timedelta(days=days_back)
    pending = (
        PredictionLog.query.filter(
            PredictionLog.model_name == "burnout",
            PredictionLog.actual_value == None,
            PredictionLog.prediction_time >= cutoff,
        )
        .limit(500)
        .all()
    )
    resolved = 0
    for log in pending:
        meta = json.loads(log.metadata_json) if log.metadata_json else {}
        discord_id = meta.get("discord_id")
        if not discord_id:
            continue
        burnout = BurnoutRisk.query.filter_by(discord_id=discord_id).first()
        if burnout and burnout.feedback:
            log.actual_value = 1 if burnout.feedback == "confirmed" else 0
            log.outcome_time = datetime.utcnow()
            log.was_correct = (
                burnout.feedback == "confirmed" and meta.get("is_flagged")
            ) or (burnout.feedback == "dismissed" and not meta.get("is_flagged"))
            resolved += 1
    if resolved:
        db.session.commit()
    return resolved
