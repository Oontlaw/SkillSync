import os
import numpy as np
import joblib
from datetime import datetime, timedelta
from sklearn.ensemble import IsolationForest
from ml.features import all_user_feature_vectors, user_anomaly_feature_vector
import json
from database import db, PredictionLog

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')
ANOMALY_MODEL_PATH = os.path.join(MODELS_DIR, 'anomaly_iforest.joblib')
ANOMALY_THRESHOLD = -0.15


def _correction_features(discord_id, days=30):
    """Return correction-derived signal: correction_count and net_delta.
    These augment the base feature vector to flag workers whose scores
    admins frequently override (potential anomaly signal)."""
    from database import AdminCorrection, Worker, ScoreLog
    cutoff = datetime.utcnow() - timedelta(days=days)
    w = Worker.query.filter_by(discord_id=discord_id).first()
    if not w:
        return np.array([0.0, 0.0])
    corrections = AdminCorrection.query.filter(
        AdminCorrection.worker_id == w.id,
        AdminCorrection.created_at >= cutoff
    ).all()
    if not corrections:
        return np.array([0.0, 0.0])
    count = min(len(corrections) / 10, 1.0)
    total_delta = sum(c.corrected_score_change - c.original_score_change for c in corrections)
    net_delta = np.tanh(total_delta / 20)
    return np.array([count, net_delta])


def all_user_vectors_with_corrections(days=30, min_msgs=10):
    """Build feature matrix augmented with correction signals."""
    X, ids = all_user_feature_vectors(days=days, min_msgs=min_msgs)
    if X.shape[0] == 0:
        return X, ids
    augs = []
    for did in ids:
        augs.append(_correction_features(did, days))
    X_aug = np.concatenate([X, np.array(augs)], axis=1)
    return X_aug, ids


def _log_anomaly_prediction(discord_id, score, is_anomaly, severity):
    """Log a single anomaly prediction to PredictionLog."""
    try:
        entry = PredictionLog(
            model_name='anomaly',
            prediction_value=float(score),
            confidence=float(min(1.0, max(0.0, abs(score) / max(abs(ANOMALY_THRESHOLD), 0.01)))),
            metadata_json=json.dumps({
                'discord_id': discord_id,
                'is_anomaly': is_anomaly,
                'severity': severity,
                'threshold': ANOMALY_THRESHOLD,
            }),
            prediction_time=datetime.utcnow(),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        print(f'[anomaly] PredictionLog write failed: {e}')


def train(min_msgs=10, days=30, contamination=0.1):
    """Train Isolation Forest on per-user message behavior + correction features."""
    X, ids = all_user_vectors_with_corrections(days=days, min_msgs=min_msgs)
    if X.shape[0] < 5:
        return {'status': 'skipped', 'reason': f'Only {X.shape[0]} users with sufficient data'}
    model = IsolationForest(
        n_estimators=100,
        contamination=contamination,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X)
    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(model, ANOMALY_MODEL_PATH)
    scores = model.decision_function(X)
    n_anomalies = int((scores < ANOMALY_THRESHOLD).sum())
    return {
        'status': 'trained',
        'users': len(ids),
        'anomalies_found': n_anomalies,
        'threshold': ANOMALY_THRESHOLD,
        'model_path': ANOMALY_MODEL_PATH,
    }


def predict(discord_id, days=30):
    """Score a single user for anomalous behavior.
    Returns dict with anomaly_score (lower = more anomalous), is_anomaly, severity."""
    if not os.path.exists(ANOMALY_MODEL_PATH):
        return None
    model = joblib.load(ANOMALY_MODEL_PATH)
    
    # Get base feature vector
    vec = user_anomaly_feature_vector(discord_id, days)
    if vec is None:
        return None
    
    # Append correction features to match model training (28 + 2 = 30 dimensions)
    try:
        correction_vec = _correction_features(discord_id, days)
        vec = np.concatenate([vec, correction_vec])
    except Exception as e:
        print(f'[anomaly] Failed to append correction features: {e}')
        return None
    
    # Verify feature count matches model expectations
    if vec.shape[0] != 30:
        print(f'[anomaly] Feature vector dimension mismatch: expected 30, got {vec.shape[0]}')
        return None
    
    vec = vec.reshape(1, -1)
    score = float(model.decision_function(vec)[0])
    is_anomaly = score < ANOMALY_THRESHOLD
    # Convert raw score to 0-100 severity
    severity = min(100, max(0, int((ANOMALY_THRESHOLD - score) * 100))) if is_anomaly else 0
    result = {
        'anomaly_score': round(score, 4),
        'is_anomaly': bool(is_anomaly),
        'severity': severity,
        'threshold': ANOMALY_THRESHOLD,
    }
    _log_anomaly_prediction(discord_id, score, is_anomaly, severity)
    if is_anomaly:
        # Also create a BehavioralAnomaly record for dashboard feedback loop
        from database import BehavioralAnomaly
        existing = BehavioralAnomaly.query.filter_by(
            discord_id=discord_id, anomaly_type='ml_anomaly', cleared_at=None
        ).first()
        if not existing:
            db.session.add(BehavioralAnomaly(
                discord_id=discord_id,
                anomaly_type='ml_anomaly',
                severity=severity,
                details=f'ML-detected behavioral anomaly (score: {round(score, 4)})',
                source='discord',
            ))
            db.session.commit()
    return result


def scan_all(min_msgs=10, days=30):
    """Score all users and return list of anomalies found."""
    X, ids = all_user_vectors_with_corrections(days=days, min_msgs=min_msgs)
    if X.shape[0] < 5 or not os.path.exists(ANOMALY_MODEL_PATH):
        return []
    model = joblib.load(ANOMALY_MODEL_PATH)
    scores = model.decision_function(X)
    results = []
    for i, did in enumerate(ids):
        if scores[i] < ANOMALY_THRESHOLD:
            severity = min(100, max(0, int((ANOMALY_THRESHOLD - scores[i]) * 100)))
            _log_anomaly_prediction(did, scores[i], True, severity)
            # Also create a BehavioralAnomaly record for dashboard feedback loop
            from database import BehavioralAnomaly
            existing = BehavioralAnomaly.query.filter_by(
                discord_id=did, anomaly_type='ml_anomaly', cleared_at=None
            ).first()
            if not existing:
                db.session.add(BehavioralAnomaly(
                    discord_id=did,
                    anomaly_type='ml_anomaly',
                    severity=severity,
                    details=f'ML-detected behavioral anomaly (score: {round(float(scores[i]), 4)})',
                    source='discord',
                ))
            results.append({
                'discord_id': did,
                'anomaly_score': round(float(scores[i]), 4),
                'severity': severity,
                'threshold': ANOMALY_THRESHOLD,
            })
    db.session.commit()
    return results



def get_precision_recall(days=30):
    """Compute precision and recall from admin feedback on anomaly predictions."""
    from database import BehavioralAnomaly
    cutoff = datetime.utcnow() - timedelta(days=days)
    with_feedback = BehavioralAnomaly.query.filter(
        BehavioralAnomaly.feedback != None,
        BehavioralAnomaly.detected_at >= cutoff,
        BehavioralAnomaly.anomaly_type == 'ml_anomaly',
    ).all()
    if not with_feedback:
        return {'total_with_feedback': 0, 'confirmed': 0, 'dismissed': 0, 'precision': None, 'recall': None}
    confirmed = sum(1 for a in with_feedback if a.feedback == 'confirmed')
    dismissed = sum(1 for a in with_feedback if a.feedback == 'dismissed')
    total = len(with_feedback)
    precision = round(confirmed / max(confirmed + dismissed, 1), 3) if (confirmed + dismissed) > 0 else None
    return {
        'total_with_feedback': total,
        'confirmed': confirmed,
        'dismissed': dismissed,
        'precision': precision,
        'precision_pct': round(precision * 100, 1) if precision is not None else None,
    }
