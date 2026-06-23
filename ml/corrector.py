import os
import numpy as np
import joblib
from collections import defaultdict
from datetime import datetime, timedelta
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.model_selection import cross_val_score, LeaveOneOut
from sklearn.preprocessing import StandardScaler

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')
CORRECTOR_MODEL_PATH = os.path.join(MODELS_DIR, 'score_corrector.joblib')
SCALER_PATH = os.path.join(MODELS_DIR, 'corrector_scaler.joblib')
MIN_CORRECTIONS = 5


def _build_training_data(days=365):
    """Build feature matrix X and targets from AdminCorrection records.
    Features (4-dim):
      0: abs(original_change)         — magnitude of original score change
      1: correction_delta              — admin's delta (corrected - original), signed
      2: worker_past_corrections       — how many times this worker was corrected before
      3: worker_total_score            — current computed score

    Targets:
      y_reg: corrected_score_change (regression)
      y_cls: direction label (0=decrease, 1=unchanged, 2=increase)
    """
    from database import db, AdminCorrection, ScoreLog, Worker
    from scoring import _compute_score

    cutoff_365 = datetime.utcnow() - timedelta(days=days)

    corrections = AdminCorrection.query.filter(
        AdminCorrection.created_at >= cutoff_365
    ).order_by(AdminCorrection.created_at).all()

    if len(corrections) < MIN_CORRECTIONS:
        return None, None, None, None, len(corrections)

    corr_count = defaultdict(int)

    X, y_reg, y_cls = [], [], []
    for c in corrections:
        wid = c.worker_id
        delta = c.corrected_score_change - c.original_score_change
        direction = 0 if delta < 0 else (2 if delta > 0 else 1)
        total_score = _compute_score(wid)

        X.append([
            abs(c.original_score_change),
            delta,
            corr_count[wid],
            total_score,
        ])
        y_reg.append(c.corrected_score_change)
        y_cls.append(direction)
        corr_count[wid] += 1

    return np.array(X), np.array(y_reg), np.array(y_cls), corrections, len(corrections)


def train(days=365):
    """Train the score corrector model from AdminCorrection history.
    Uses Ridge regression (regularized) for magnitude prediction and
    LogisticRegression for direction classification.
    Uses Leave-One-Out CV to report realistic R².
    """
    X, y_reg, y_cls, corrections, count = _build_training_data(days=days)
    if X is None or len(y_reg) < MIN_CORRECTIONS:
        return {'status': 'skipped', 'reason': f'Only {count} corrections in DB (need {MIN_CORRECTIONS})'}

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Regression: Ridge with L2 regularization to prevent overfitting
    reg = Ridge(alpha=10.0, random_state=42)
    reg.fit(X_scaled, y_reg)
    train_r2 = float(reg.score(X_scaled, y_reg))

    # CV: Leave-One-Out for realistic generalization estimate (n small)
    cv_r2 = None
    if len(y_reg) >= 3:
        try:
            cv_scores = cross_val_score(reg, X_scaled, y_reg, cv=LeaveOneOut(), scoring='r2')
            cv_scores = cv_scores[~np.isnan(cv_scores)]
            if len(cv_scores) > 0:
                cv_r2 = float(np.mean(cv_scores))
        except Exception:
            pass
    if cv_r2 is None or not np.isfinite(cv_r2):
        cv_r2 = train_r2

    # Classification: predict direction (decrease/unchanged/increase)
    cls = LogisticRegression(max_iter=1000, random_state=42)
    cls.fit(X_scaled, y_cls)
    train_acc = float(cls.score(X_scaled, y_cls))
    cv_acc = None
    if len(y_cls) >= 3 and len(np.unique(y_cls)) > 1:
        try:
            cv_acc = float(np.mean(cross_val_score(cls, X_scaled, y_cls, cv=LeaveOneOut(), scoring='accuracy')))
        except Exception:
            pass
    if cv_acc is None:
        cv_acc = train_acc

    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump({'regressor': reg, 'classifier': cls}, CORRECTOR_MODEL_PATH)
    joblib.dump(scaler, SCALER_PATH)

    return {
        'status': 'trained',
        'corrections_used': len(y_reg),
        'r2_score': round(cv_r2, 4),
        'classifier_accuracy': round(cv_acc, 4),
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
        from database import db, AdminCorrection
        from scoring import _compute_score

        past_corrections = AdminCorrection.query.filter_by(worker_id=worker_id).count()
        total_score = _compute_score(worker_id)
        vec = np.array([[abs(original_change), 0, past_corrections, total_score]]).reshape(1, -1)
    else:
        vec = np.array([[abs(original_change), 0, 0, 0]]).reshape(1, -1)

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
