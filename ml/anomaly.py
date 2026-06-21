import os
import numpy as np
import joblib
from sklearn.ensemble import IsolationForest
from ml.features import all_user_feature_vectors, user_anomaly_feature_vector

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')
ANOMALY_MODEL_PATH = os.path.join(MODELS_DIR, 'anomaly_iforest.joblib')
ANOMALY_THRESHOLD = -0.3  # decision_function threshold for flagging


def train(min_msgs=10, days=30, contamination=0.05):
    """Train Isolation Forest on per-user message behavior features."""
    X, ids = all_user_feature_vectors(days=days, min_msgs=min_msgs)
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
    vec = user_anomaly_feature_vector(discord_id, days)
    if vec is None:
        return None
    vec = vec.reshape(1, -1)
    score = float(model.decision_function(vec)[0])
    is_anomaly = score < ANOMALY_THRESHOLD
    # Convert raw score to 0-100 severity
    severity = min(100, max(0, int((ANOMALY_THRESHOLD - score) * 100))) if is_anomaly else 0
    return {
        'anomaly_score': round(score, 4),
        'is_anomaly': bool(is_anomaly),
        'severity': severity,
        'threshold': ANOMALY_THRESHOLD,
    }


def scan_all(min_msgs=10, days=30):
    """Score all users and return list of anomalies found."""
    X, ids = all_user_feature_vectors(days=days, min_msgs=min_msgs)
    if X.shape[0] < 5 or not os.path.exists(ANOMALY_MODEL_PATH):
        return []
    model = joblib.load(ANOMALY_MODEL_PATH)
    scores = model.decision_function(X)
    results = []
    for i, did in enumerate(ids):
        if scores[i] < ANOMALY_THRESHOLD:
            severity = min(100, max(0, int((ANOMALY_THRESHOLD - scores[i]) * 100)))
            results.append({
                'discord_id': did,
                'anomaly_score': round(float(scores[i]), 4),
                'severity': severity,
                'threshold': ANOMALY_THRESHOLD,
            })
    return results
