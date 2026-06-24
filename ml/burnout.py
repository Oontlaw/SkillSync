import os
import numpy as np
import joblib
from sklearn.ensemble import IsolationForest
from ml.features import staff_feature_vectors
import json
from datetime import datetime
from database import db, PredictionLog

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')
BURNOUT_MODEL_PATH = os.path.join(MODELS_DIR, 'burnout_iforest.joblib')
BURNOUT_THRESHOLD = 0.0


def _correction_rate(worker_id, days=30):
    """Return correction frequency as a burnout signal.
    Frequent admin corrections can indicate the worker's behavior
    is erratic or the system keeps mis-scoring them — both burnout flags."""
    from database import AdminCorrection, ScoreLog
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    corr_count = AdminCorrection.query.filter(
        AdminCorrection.worker_id == worker_id,
        AdminCorrection.created_at >= cutoff
    ).count()
    action_count = ScoreLog.query.filter(
        ScoreLog.worker_id == worker_id,
        ScoreLog.created_at >= cutoff
    ).count()
    return min(1.0, corr_count / max(action_count, 1))


def _staff_vectors_with_corrections(days=30):
    """Build staff feature vectors augmented with correction rate (8-dim)."""
    X, wids, dids, names = staff_feature_vectors(days=days)
    if X is None:
        return None, [], [], []
    augs = []
    for wid in wids:
        augs.append([_correction_rate(wid, days)])
    X_aug = np.concatenate([X, np.array(augs)], axis=1)
    return X_aug, wids, dids, names


def _log_burnout_prediction(discord_id, worker_id, raw_score, burnout_score, is_flagged, signals):
    """Log a burnout prediction to PredictionLog."""
    try:
        entry = PredictionLog(
            model_name='burnout',
            prediction_value=float(burnout_score),
            confidence=float(min(1.0, max(0.0, abs(raw_score) / max(abs(BURNOUT_THRESHOLD), 0.01)))),
            metadata_json=json.dumps({
                'discord_id': discord_id,
                'worker_id': worker_id,
                'is_flagged': is_flagged,
                'raw_score': round(raw_score, 4),
                'signals': signals,
                'threshold': BURNOUT_THRESHOLD,
            }),
            prediction_time=datetime.utcnow(),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        print(f'[burnout] PredictionLog write failed: {e}')


def train(contamination=0.1, days=30):
    """Train Isolation Forest on staff + correction feature vectors for burnout detection."""
    X, wids, dids, names = _staff_vectors_with_corrections(days=days)
    if X is None or X.shape[0] < 5:
        return {'status': 'skipped', 'reason': f'Only {X.shape[0] if X is not None else 0} staff with data'}
    model = IsolationForest(
        n_estimators=100,
        contamination=contamination,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X)
    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(model, BURNOUT_MODEL_PATH)
    scores = model.decision_function(X)
    flagged = int((scores < BURNOUT_THRESHOLD).sum())
    return {
        'status': 'trained',
        'staff_scanned': len(wids),
        'flagged': flagged,
        'threshold': BURNOUT_THRESHOLD,
    }


def score_worker(discord_id, days=30):
    """Score a single worker for burnout risk.
    Returns dict with burnout_score (0-100), signals, is_flagged."""
    import os
    if not os.path.exists(BURNOUT_MODEL_PATH):
        return None
    model = joblib.load(BURNOUT_MODEL_PATH)

    # Build feature vector for this single worker
    X, wids, dids, names = _staff_vectors_with_corrections(days=days)
    if X is None:
        return None
    try:
        idx = dids.index(discord_id)
    except ValueError:
        return None
    vec = X[idx].reshape(1, -1)

    raw_score = float(model.decision_function(vec)[0])
    is_flagged = raw_score < BURNOUT_THRESHOLD
    # Convert to 0-100 burnout score (invert: lower ML score = higher burnout)
    burnout_score = min(100, max(0, int((BURNOUT_THRESHOLD - raw_score) * 100))) if is_flagged else 0

    signals = []
    features = vec[0]
    if features[0] > 0.3:
        signals.append('frequent_anomalies')
    if features[1] > 0.1:
        signals.append('increasing_reversals')
    if features[2] > 0.3:
        signals.append('voice_creep')
    if features[3] > 0.5:
        signals.append('high_action_volume')
    if features[4] > 0.5:
        signals.append('off_hours_pattern')
    if features[5] > 0.6:
        signals.append('erratic_activity')
    if len(features) > 7 and features[7] > 0.3:
        signals.append('frequent_corrections')

    result = {
        'burnout_score': burnout_score,
        'is_flagged': bool(is_flagged),
        'raw_anomaly_score': round(raw_score, 4),
        'signals': signals,
    }
    worker = None
    try:
        from database import Worker as WorkerModel
        worker = WorkerModel.query.filter_by(discord_id=discord_id).first()
    except Exception:
        pass
    _log_burnout_prediction(
        discord_id=discord_id,
        worker_id=worker.id if worker else None,
        raw_score=raw_score,
        burnout_score=burnout_score,
        is_flagged=is_flagged,
        signals=signals,
    )
    return result


def scan_all(days=30):
    """Score all staff and return list of flagged workers with burnout info."""
    X, wids, dids, names = _staff_vectors_with_corrections(days=days)
    if X is None or not os.path.exists(BURNOUT_MODEL_PATH):
        return []
    model = joblib.load(BURNOUT_MODEL_PATH)
    scores = model.decision_function(X)
    results = []
    for i in range(len(wids)):
        is_flagged = scores[i] < BURNOUT_THRESHOLD
        burnout_score = min(100, max(0, int((BURNOUT_THRESHOLD - scores[i]) * 100))) if is_flagged else 0
        if is_flagged:
            _log_burnout_prediction(
                discord_id=dids[i],
                worker_id=wids[i],
                raw_score=scores[i],
                burnout_score=burnout_score,
                is_flagged=True,
                signals=[],
            )
            results.append({
                'worker_id': wids[i],
                'discord_id': dids[i],
                'name': names[i],
                'burnout_score': burnout_score,
                'raw_score': round(float(scores[i]), 4),
                'signals': [],
            })
    return results


def get_precision_recall(days=30):
    """Compute precision from admin feedback on burnout risk predictions."""
    from database import BurnoutRisk
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    with_feedback = BurnoutRisk.query.filter(
        BurnoutRisk.feedback != None,
        BurnoutRisk.detected_at >= cutoff,
    ).all()
    if not with_feedback:
        return {'total_with_feedback': 0, 'confirmed': 0, 'dismissed': 0, 'precision': None}
    confirmed = sum(1 for b in with_feedback if b.feedback == 'confirmed')
    dismissed = sum(1 for b in with_feedback if b.feedback == 'dismissed')
    precision = round(confirmed / max(confirmed + dismissed, 1), 3) if (confirmed + dismissed) > 0 else None
    return {
        'total_with_feedback': len(with_feedback),
        'confirmed': confirmed,
        'dismissed': dismissed,
        'precision': precision,
        'precision_pct': round(precision * 100, 1) if precision is not None else None,
    }
