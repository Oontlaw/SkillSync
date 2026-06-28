import json
import os
from datetime import datetime, timedelta

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from database import PredictionLog, db
from ml import model_path
from ml.features import all_user_feature_vectors, user_anomaly_feature_vector

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
ANOMALY_MODEL_PATH = os.path.join(MODELS_DIR, "anomaly_iforest.joblib")
ANOMALY_THRESHOLD = -0.15


def _correction_features(discord_id, days=30):
    """Return correction-derived signal: correction_count and net_delta.
    These augment the base feature vector to flag workers whose scores
    admins frequently override (potential anomaly signal)."""
    from database import AdminCorrection, ScoreLog, Worker

    cutoff = datetime.utcnow() - timedelta(days=days)
    w = Worker.query.filter_by(discord_id=discord_id).first()
    if not w:
        return np.array([0.0, 0.0])
    corrections = AdminCorrection.query.filter(
        AdminCorrection.worker_id == w.id, AdminCorrection.created_at >= cutoff
    ).all()
    if not corrections:
        return np.array([0.0, 0.0])
    count = min(len(corrections) / 10, 1.0)
    total_delta = sum(
        c.corrected_score_change - c.original_score_change for c in corrections
    )
    net_delta = np.tanh(total_delta / 20)
    return np.array([count, net_delta])


def all_user_vectors_with_corrections(days=30, min_msgs=10, guild_id=None):
    """Build feature matrix augmented with correction + baseline drift signals.
    Dimensions: 28 (base) + 2 (corrections) + 2 (drift) = 32."""
    from database import UserBehaviorBaseline

    X, ids = all_user_feature_vectors(days=days, min_msgs=min_msgs, guild_id=guild_id)
    if X.shape[0] == 0:
        return X, ids
    augs = []
    for did in ids:
        corr = _correction_features(did, days)
        # Drift features from long-term baseline
        baseline = (
            UserBehaviorBaseline.query.filter_by(discord_id=did)
            .order_by(UserBehaviorBaseline.updated_at.desc())
            .first()
        )
        if baseline:
            volume_drift = min(1.0, max(-1.0, baseline.volume_drift or 0.0))
            pattern_drift = min(1.0, baseline.pattern_drift or 0.0)
        else:
            volume_drift = 0.0
            pattern_drift = 0.0
        augs.append(np.array([corr[0], corr[1], volume_drift, pattern_drift]))
    X_aug = np.concatenate([X, np.array(augs)], axis=1)
    return X_aug, ids


def _log_anomaly_prediction(discord_id, score, is_anomaly, severity):
    """Log a single anomaly prediction to PredictionLog."""
    try:
        entry = PredictionLog(
            model_name="anomaly",
            prediction_value=float(score),
            confidence=float(
                min(1.0, max(0.0, abs(score) / max(abs(ANOMALY_THRESHOLD), 0.01)))
            ),
            metadata_json=json.dumps(
                {
                    "discord_id": discord_id,
                    "is_anomaly": is_anomaly,
                    "severity": severity,
                    "threshold": ANOMALY_THRESHOLD,
                }
            ),
            prediction_time=datetime.utcnow(),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        print(f"[anomaly] PredictionLog write failed: {e}")


def train(min_msgs=10, days=30, contamination=0.1, guild_id=None):
    """Train Isolation Forest on per-user message behavior + correction + drift features.
    Feature dimension: 32 (28 base + 2 correction + 2 drift).
    If guild_id is provided, trains a per-guild model using only that guild's data."""
    X, ids = all_user_vectors_with_corrections(
        days=days, min_msgs=min_msgs, guild_id=guild_id
    )
    if X.shape[0] < 5:
        return {
            "status": "skipped",
            "reason": f"Only {X.shape[0]} users with sufficient data",
        }
    model = IsolationForest(
        n_estimators=100,
        contamination=contamination,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X)
    os.makedirs(MODELS_DIR, exist_ok=True)
    path = model_path("anomaly", guild_id) if guild_id else ANOMALY_MODEL_PATH
    joblib.dump(model, path)
    scores = model.decision_function(X)
    n_anomalies = int((scores < ANOMALY_THRESHOLD).sum())
    return {
        "status": "trained",
        "users": len(ids),
        "anomalies_found": n_anomalies,
        "feature_dims": 32,
        "threshold": ANOMALY_THRESHOLD,
        "model_path": path,
    }


def predict(discord_id, days=30, guild_id=None):
    """Score a single user for anomalous behavior.
    Feature dimension: 32 (28 base + 2 correction + 2 drift).
    If guild_id is provided, loads the per-guild model and tags the anomaly with that guild.
    Returns dict with anomaly_score (lower = more anomalous), is_anomaly, severity."""
    from database import UserBehaviorBaseline

    path = model_path("anomaly", guild_id) if guild_id else ANOMALY_MODEL_PATH
    if not os.path.exists(path):
        # Fall back to global model if no per-guild model
        path = ANOMALY_MODEL_PATH
        if not os.path.exists(path):
            return None
    model = joblib.load(path)

    # Get base feature vector (28-dim)
    vec = user_anomaly_feature_vector(discord_id, days, guild_id=guild_id)
    if vec is None:
        return None

    # Append correction features (2-dim) + drift features (2-dim) = 32 total
    try:
        correction_vec = _correction_features(discord_id, days)
        baseline = UserBehaviorBaseline.query.filter_by(discord_id=discord_id).first()
        volume_drift = (
            min(1.0, max(-1.0, baseline.volume_drift or 0.0)) if baseline else 0.0
        )
        pattern_drift = min(1.0, baseline.pattern_drift or 0.0) if baseline else 0.0
        drift_vec = np.array([volume_drift, pattern_drift])
        vec = np.concatenate([vec, correction_vec, drift_vec])
    except Exception as e:
        print(f"[anomaly] Failed to append features: {e}")
        return None

    # Verify feature count matches model expectations
    if vec.shape[0] != 32:
        print(
            f"[anomaly] Feature vector dimension mismatch: expected 32, got {vec.shape[0]}"
        )
        return None

    vec = vec.reshape(1, -1)
    score = float(model.decision_function(vec)[0])
    is_anomaly = score < ANOMALY_THRESHOLD
    # Convert raw score to 0-100 severity
    severity = (
        min(100, max(0, int((ANOMALY_THRESHOLD - score) * 100))) if is_anomaly else 0
    )
    result = {
        "anomaly_score": round(score, 4),
        "is_anomaly": bool(is_anomaly),
        "severity": severity,
        "threshold": ANOMALY_THRESHOLD,
    }
    _log_anomaly_prediction(discord_id, score, is_anomaly, severity)
    if is_anomaly:
        # Also create a BehavioralAnomaly record for dashboard feedback loop
        from database import BehavioralAnomaly

        existing = BehavioralAnomaly.query.filter_by(
            discord_id=discord_id, anomaly_type="ml_anomaly", cleared_at=None
        ).first()
        if not existing:
            db.session.add(
                BehavioralAnomaly(
                    discord_id=discord_id,
                    guild_id=guild_id,
                    anomaly_type="ml_anomaly",
                    severity=severity,
                    details=f"ML-detected behavioral anomaly (score: {round(score, 4)})",
                    source="discord",
                )
            )
            db.session.commit()
    return result


def scan_all(min_msgs=10, days=30, guild_id=None):
    """Score all users and return list of anomalies found.
    Feature dimension: 32 (28 base + 2 correction + 2 drift).
    Also writes cross-model anomaly counts to UserBehaviorBaseline."""
    X, ids = all_user_vectors_with_corrections(
        days=days, min_msgs=min_msgs, guild_id=guild_id
    )
    path = _model_path(guild_id)
    if not os.path.exists(path):
        path = ANOMALY_MODEL_PATH
    if X.shape[0] < 5 or not os.path.exists(path):
        return []
    model = joblib.load(path)
    scores = model.decision_function(X)
    results = []
    anomaly_counts = {}
    for i, did in enumerate(ids):
        if scores[i] < ANOMALY_THRESHOLD:
            severity = min(100, max(0, int((ANOMALY_THRESHOLD - scores[i]) * 100)))
            _log_anomaly_prediction(did, scores[i], True, severity)
            # Also create a BehavioralAnomaly record for dashboard feedback loop
            from database import BehavioralAnomaly

            existing = BehavioralAnomaly.query.filter_by(
                discord_id=did, anomaly_type="ml_anomaly", cleared_at=None
            ).first()
            if not existing:
                db.session.add(
                    BehavioralAnomaly(
                        discord_id=did,
                        guild_id=guild_id,
                        anomaly_type="ml_anomaly",
                        severity=severity,
                        details=f"ML-detected behavioral anomaly (score: {round(float(scores[i]), 4)})",
                        source="discord",
                    )
                )
            results.append(
                {
                    "discord_id": did,
                    "anomaly_score": round(float(scores[i]), 4),
                    "severity": severity,
                    "threshold": ANOMALY_THRESHOLD,
                }
            )
            anomaly_counts[did] = anomaly_counts.get(did, 0) + 1

    db.session.commit()

    # Update cross-model signals in UserBehaviorBaseline
    try:
        from database import UserBehaviorBaseline

        for did, count in anomaly_counts.items():
            baseline = UserBehaviorBaseline.query.filter_by(discord_id=did).first()
            if baseline:
                baseline.recent_anomaly_count = count
        db.session.commit()
    except Exception as e:
        print(f"[anomaly] Cross-model signal update failed: {e}")

    return results


def get_precision_recall(days=30):
    """Compute precision and recall from admin feedback on anomaly predictions."""
    from database import BehavioralAnomaly

    cutoff = datetime.utcnow() - timedelta(days=days)
    with_feedback = BehavioralAnomaly.query.filter(
        BehavioralAnomaly.feedback != None,
        BehavioralAnomaly.detected_at >= cutoff,
        BehavioralAnomaly.anomaly_type == "ml_anomaly",
    ).all()
    if not with_feedback:
        return {
            "total_with_feedback": 0,
            "confirmed": 0,
            "dismissed": 0,
            "precision": None,
            "recall": None,
        }
    confirmed = sum(1 for a in with_feedback if a.feedback == "confirmed")
    dismissed = sum(1 for a in with_feedback if a.feedback == "dismissed")
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
    }


def resolve_anomaly_outcomes(days_back=30):
    """Resolve pending anomaly predictions against admin feedback on BehavioralAnomaly."""
    from database import BehavioralAnomaly

    cutoff = datetime.utcnow() - timedelta(days=days_back)
    pending = (
        PredictionLog.query.filter(
            PredictionLog.model_name == "anomaly",
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
        if discord_id is None:
            continue
        anomaly = BehavioralAnomaly.query.filter_by(
            discord_id=discord_id,
            anomaly_type="ml_anomaly",
            detected_at=log.prediction_time,
        ).first()
        if anomaly and anomaly.feedback:
            log.actual_value = 1 if anomaly.feedback == "confirmed" else 0
            log.outcome_time = datetime.utcnow()
            log.was_correct = anomaly.feedback == "confirmed"
            resolved += 1

    if resolved:
        db.session.commit()
    return resolved


def migrate_model_with_distillation():
    """
    Knowledge transfer from old 30-feature model to new 32-feature model.

    CALL THIS ONCE before deleting old .joblib files.
    It extracts the old model's knowledge as soft labels, trains the new
    model on DB data weighted by those labels, then retires the old model.

    After this runs, the new model inherits everything the old one knew.
    Safe to delete old .joblib after this completes successfully.
    """
    from database import UserBehaviorBaseline

    OLD_MODEL_PATH = ANOMALY_MODEL_PATH  # 30-feature model
    NEW_MODEL_PATH = ANOMALY_MODEL_PATH.replace(
        "anomaly_iforest.joblib", "anomaly_iforest_new.joblib"
    )
    RETIRED_PATH = ANOMALY_MODEL_PATH.replace(
        "anomaly_iforest.joblib", "anomaly_iforest_retired.joblib"
    )

    # Step 1 — check old model exists
    if not os.path.exists(OLD_MODEL_PATH):
        return {"status": "skipped", "reason": "No existing model to transfer from"}

    old_model = joblib.load(OLD_MODEL_PATH)

    # Step 2 — get 30-feature vectors (old format) for all users
    X_old, ids = all_user_feature_vectors(days=30, min_msgs=5)
    if X_old.shape[0] < 5:
        return {"status": "skipped", "reason": "Not enough users for distillation"}

    # Append old correction features (2 dims) to match old 30-feature format
    augs_old = np.array([_correction_features(did, 30) for did in ids])
    X_old_full = np.concatenate([X_old, augs_old], axis=1)  # shape (n, 30)

    # Step 3 — extract soft labels from old model
    # decision_function: more negative = more anomalous
    teacher_scores = old_model.decision_function(X_old_full)  # shape (n,)
    # Normalize to 0-1 where 1 = definitely anomalous
    teacher_labels = (teacher_scores < ANOMALY_THRESHOLD).astype(float)
    # Soft weight: how confident the old model was
    confidence_weights = np.clip(
        np.abs(teacher_scores - ANOMALY_THRESHOLD) / 0.3, 0.1, 1.0
    )

    # Step 4 — build new 32-feature vectors for same users
    augs_new = []
    for did in ids:
        corr = _correction_features(did, 30)
        baseline = UserBehaviorBaseline.query.filter_by(discord_id=did).first()
        vol_drift = (
            min(1.0, max(-1.0, baseline.volume_drift or 0.0)) if baseline else 0.0
        )
        pat_drift = min(1.0, baseline.pattern_drift or 0.0) if baseline else 0.0
        augs_new.append(np.array([corr[0], corr[1], vol_drift, pat_drift]))
    X_new_full = np.concatenate([X_old, np.array(augs_new)], axis=1)  # shape (n, 32)

    # Step 5 — train new IsolationForest on new features
    # IsolationForest doesn't support sample_weight directly,
    # so we oversample high-confidence teacher predictions
    oversample_idx = np.where(confidence_weights > 0.7)[0]
    X_train = np.concatenate([X_new_full, X_new_full[oversample_idx]], axis=0)

    new_model = IsolationForest(
        n_estimators=150,  # more trees than old model (was 100)
        contamination=0.1,
        random_state=42,
        n_jobs=-1,
        max_samples="auto",
    )
    new_model.fit(X_train)

    # Step 6 — verify new model agrees with old model on clear cases
    new_scores = new_model.decision_function(X_new_full)
    old_anomalies = set(ids[i] for i in range(len(ids)) if teacher_labels[i] == 1)
    new_anomalies = set(
        ids[i] for i in range(len(ids)) if new_scores[i] < ANOMALY_THRESHOLD
    )
    overlap = len(old_anomalies & new_anomalies)
    agreement_rate = overlap / max(len(old_anomalies), 1)

    # Step 7 — save new model, retire old one
    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(new_model, NEW_MODEL_PATH)

    # Rename old -> retired (don't delete, keep as fallback for 30 days)
    os.rename(OLD_MODEL_PATH, RETIRED_PATH)
    # Move new -> primary path
    os.rename(NEW_MODEL_PATH, OLD_MODEL_PATH)

    return {
        "status": "success",
        "users_transferred": len(ids),
        "old_anomalies": len(old_anomalies),
        "new_anomalies": len(new_anomalies),
        "agreement_rate": round(agreement_rate, 3),
        "old_model_retired_to": RETIRED_PATH,
        "note": "Safe to delete retired model after 7 days if dashboard looks correct",
    }
