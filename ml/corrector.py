import os
import numpy as np
import joblib
from collections import defaultdict
from datetime import datetime, timedelta
from sklearn.linear_model import LogisticRegression
from sklearn.tree import DecisionTreeRegressor
from sklearn.preprocessing import StandardScaler

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')
CORRECTOR_MODEL_PATH = os.path.join(MODELS_DIR, 'score_corrector.joblib')
SCALER_PATH = os.path.join(MODELS_DIR, 'corrector_scaler.joblib')
MIN_CORRECTIONS = 5


def _build_training_data(days=365):
    """Build feature matrix X and targets from AdminCorrection records.
    Features (8-dim):
      0: abs(original_change)         — magnitude of original score change
      1: correction_delta              — admin's delta (corrected - original), signed
      2: worker_past_corrections       — how many times this worker was corrected before
      3: worker_total_actions          — total ScoreLog entries for this worker
      4: worker_total_score            — current computed score
      5: worker_anomaly_count          — behavioral anomalies in last 30d
      6: worker_msg_volume             — messages sent in last 30d
      7: days_since_first_correction   — how long worker has been in correction system

    Targets:
      y_reg: corrected_score_change (regression)
      y_cls: direction label (0=decrease, 1=unchanged, 2=increase)
    """
    from database import db, AdminCorrection, ScoreLog, Worker, BehavioralAnomaly, MessageRecord
    from scoring import _compute_score

    cutoff_30 = datetime.utcnow() - timedelta(days=30)
    cutoff_365 = datetime.utcnow() - timedelta(days=days)

    corrections = AdminCorrection.query.filter(
        AdminCorrection.created_at >= cutoff_365
    ).order_by(AdminCorrection.created_at).all()

    if len(corrections) < MIN_CORRECTIONS:
        return None, None, None, None, len(corrections)

    # Precompute per-worker stats
    worker_stats = {}
    for c in corrections:
        wid = c.worker_id
        if wid not in worker_stats:
            anomalies = BehavioralAnomaly.query.filter(
                BehavioralAnomaly.discord_id == Worker.query.get(wid).discord_id
                if Worker.query.get(wid) and Worker.query.get(wid).discord_id else None,
                BehavioralAnomaly.detected_at >= cutoff_30
            ).count() if Worker.query.get(wid) and Worker.query.get(wid).discord_id else 0

            msgs = MessageRecord.query.filter(
                MessageRecord.discord_id == Worker.query.get(wid).discord_id
                if Worker.query.get(wid) and Worker.query.get(wid).discord_id else None,
                MessageRecord.created_at >= cutoff_30
            ).count() if Worker.query.get(wid) and Worker.query.get(wid).discord_id else 0

            total_actions = ScoreLog.query.filter(
                ScoreLog.worker_id == wid
            ).count()

            worker_stats[wid] = {
                'anomaly_count': anomalies,
                'msg_volume': msgs,
                'total_actions': total_actions,
            }

    # Count corrections before each record for "past_corrections" feature
    corr_count = defaultdict(int)
    first_corr = {}

    X, y_reg, y_cls = [], [], []
    for c in corrections:
        wid = c.worker_id
        delta = c.corrected_score_change - c.original_score_change
        direction = 0 if delta < 0 else (2 if delta > 0 else 1)

        stats = worker_stats.get(wid, {})
        total_score = _compute_score(wid)

        if wid not in first_corr:
            first_corr[wid] = c.created_at
        days_in_system = (c.created_at - first_corr[wid]).days if first_corr[wid] else 0

        X.append([
            abs(c.original_score_change),
            delta,
            corr_count[wid],
            stats.get('total_actions', 0),
            total_score,
            stats.get('anomaly_count', 0),
            stats.get('msg_volume', 0),
            days_in_system,
        ])
        y_reg.append(c.corrected_score_change)
        y_cls.append(direction)
        corr_count[wid] += 1

    return np.array(X), np.array(y_reg), np.array(y_cls), corrections, len(corrections)


def train(days=365):
    """Train the score corrector model from AdminCorrection history.
    Uses DecisionTreeRegressor for magnitude prediction and
    LogisticRegression for direction classification.
    """
    X, y_reg, y_cls, corrections, count = _build_training_data(days=days)
    if X is None or len(y_reg) < MIN_CORRECTIONS:
        return {'status': 'skipped', 'reason': f'Only {count} corrections in DB (need {MIN_CORRECTIONS})'}

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Regression: predict exact corrected value
    reg = DecisionTreeRegressor(max_depth=6, min_samples_leaf=3, random_state=42)
    reg.fit(X_scaled, y_reg)
    r2 = float(reg.score(X_scaled, y_reg))

    # Classification: predict direction (decrease/unchanged/increase)
    cls = LogisticRegression(max_iter=1000, random_state=42, multi_class='auto')
    cls.fit(X_scaled, y_cls)
    acc = float(cls.score(X_scaled, y_cls))

    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump({'regressor': reg, 'classifier': cls}, CORRECTOR_MODEL_PATH)
    joblib.dump(scaler, SCALER_PATH)

    return {
        'status': 'trained',
        'corrections_used': len(y_reg),
        'r2_score': round(r2, 4),
        'classifier_accuracy': round(acc, 4),
        'n_features': X.shape[1],
    }


def predict(original_change, worker_id=None, worker_stats=None):
    """Predict the correct score change given context.
    If worker_id is provided, stats are fetched from DB.
    worker_stats can pre-supply a feature vector (8-dim).
    Returns dict with predicted_change, direction, confidence.
    """
    if not os.path.exists(CORRECTOR_MODEL_PATH):
        return None

    model_data = joblib.load(CORRECTOR_MODEL_PATH)
    scaler = joblib.load(SCALER_PATH)
    reg = model_data['regressor']
    cls = model_data['classifier']

    if worker_stats is not None:
        vec = np.array(worker_stats).reshape(1, -1)
    elif worker_id is not None:
        from database import AdminCorrection, ScoreLog, BehavioralAnomaly, MessageRecord, Worker
        from scoring import _compute_score
        cutoff_30 = datetime.utcnow() - timedelta(days=30)

        past_corrections = AdminCorrection.query.filter_by(worker_id=worker_id).count()
        total_actions = ScoreLog.query.filter_by(worker_id=worker_id).count()
        total_score = _compute_score(worker_id)
        w = db.session.get(Worker, worker_id) if worker_id else None
        anomalies = BehavioralAnomaly.query.filter(
            BehavioralAnomaly.discord_id == w.discord_id,
            BehavioralAnomaly.detected_at >= cutoff_30
        ).count() if w and w.discord_id else 0
        msgs = MessageRecord.query.filter(
            MessageRecord.discord_id == w.discord_id,
            MessageRecord.created_at >= cutoff_30
        ).count() if w and w.discord_id else 0
        vec = np.array([[abs(original_change), 0, past_corrections, total_actions,
                         total_score, anomalies, msgs, 0]]).reshape(1, -1)
    else:
        vec = np.array([[abs(original_change), 0, 0, 0, 0, 0, 0, 0]]).reshape(1, -1)

    vec_scaled = scaler.transform(vec)
    pred_change = float(reg.predict(vec_scaled)[0])
    pred_dir = int(cls.predict(vec_scaled)[0])
    dir_proba = float(max(cls.predict_proba(vec_scaled)[0]))

    direction_map = {0: 'decrease', 1: 'unchanged', 2: 'increase'}
    return {
        'predicted_change': round(pred_change, 1),
        'direction': direction_map.get(pred_dir, 'unknown'),
        'confidence': round(dir_proba, 3),
    }


def get_stats():
    """Return training statistics from the persisted model."""
    if not os.path.exists(CORRECTOR_MODEL_PATH):
        return {'trained': False}
    model_data = joblib.load(CORRECTOR_MODEL_PATH)
    return {
        'trained': True,
        'model_path': CORRECTOR_MODEL_PATH,
    }
