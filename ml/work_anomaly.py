"""
Work-specific anomaly detection and burnout detection.

Uses Isolation Forest on work feature vectors from ml/work_features.py.
Completely separate from ml/anomaly.py and ml/burnout.py (which operate on
Discord data). Models are saved per org_id for tenant isolation.
"""

import os

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from database import BehavioralAnomaly, Worker, WorkerIdentity, db
from ml.work_features import all_work_feature_vectors, work_feature_vector_for_worker

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
WORK_ANOMALY_THRESHOLD = -0.15

BURNOUT_SIGNALS = {
    "late_ratio_threshold": 0.4,
    "miss_acceleration_threshold": 0.3,
    "score_trend_negative": -0.2,
    "streak_collapse": True,
}


def _model_path(org_id: int) -> str:
    return os.path.join(MODELS_DIR, f"work_anomaly_{org_id}.joblib")


def _severity_float(severity_str: str) -> float:
    """Map severity label to float for BehavioralAnomaly.severity column."""
    mapping = {"high": 0.7, "medium": 0.4, "low": 0.2, "none": 0.0}
    return mapping.get(severity_str, 0.0)


def _discord_id_for_worker(worker_id: int) -> str:
    """Get a discord_id for the BehavioralAnomaly record.
    Uses the worker's own discord_id if available, otherwise a synthetic key.
    """
    worker = db.session.get(Worker, worker_id)
    if worker and worker.discord_id:
        return worker.discord_id
    return f"work_{worker_id}"


def train(
    org_id: int, days: int = 30, min_tasks: int = 3, contamination: float = 0.1
) -> dict:
    """
    Train per-org Isolation Forest on work feature vectors.

    Model is saved to ml/models/work_anomaly_{org_id}.joblib.

    Returns dict with status, worker count, and anomalies found.
    """
    X, worker_ids = all_work_feature_vectors(org_id, days=days, min_tasks=min_tasks)

    if X.shape[0] < 3:
        return {
            "status": "skipped",
            "reason": f"Only {X.shape[0]} workers with sufficient data (need >=3)",
            "org_id": org_id,
        }

    model = IsolationForest(
        n_estimators=100, contamination=contamination, random_state=42
    )
    model.fit(X)

    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(model, _model_path(org_id))

    predictions = model.predict(X)
    anomalies = int(np.sum(predictions == -1))

    return {
        "status": "trained",
        "org_id": org_id,
        "workers": X.shape[0],
        "anomalies_found": anomalies,
    }


def predict(worker_id: int, org_id: int, days: int = 30) -> dict:
    """
    Score a worker for anomalous work behaviour.

    Loads the org's trained Isolation Forest model.

    Returns dict with worker_id, anomaly_score, is_anomaly, severity, features.
    """
    model_path = _model_path(org_id)
    if not os.path.exists(model_path):
        return {
            "worker_id": worker_id,
            "error": "Model not trained for this org",
            "is_anomaly": False,
        }

    model = joblib.load(model_path)
    fv = work_feature_vector_for_worker(worker_id, org_id, days=days)

    fv_2d = fv.reshape(1, -1)
    score = float(model.decision_function(fv_2d)[0])
    pred = int(model.predict(fv_2d)[0])

    is_anomaly = pred == -1 and score < WORK_ANOMALY_THRESHOLD

    if score < -0.3:
        severity = "high"
    elif score < -0.2:
        severity = "medium"
    elif is_anomaly:
        severity = "low"
    else:
        severity = "none"

    return {
        "worker_id": worker_id,
        "anomaly_score": round(score, 4),
        "is_anomaly": is_anomaly,
        "severity": severity,
        "features": fv.tolist(),
    }


def run_org_scan(org_id: int) -> list:
    """
    Run predict() for every worker in the org.

    For each anomaly found:
    - Writes a BehavioralAnomaly record with source='work_engine'
    - Fires award_work_points(worker_id, 'work_anomaly_detected')

    Returns list of anomaly result dicts.
    """
    identities = WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()
    worker_ids = [i.worker_id for i in identities if i.worker_id]

    results = []
    for wid in worker_ids:
        try:
            result = predict(wid, org_id)
            if result.get("is_anomaly"):
                anomaly = BehavioralAnomaly(
                    discord_id=_discord_id_for_worker(wid),
                    guild_id=None,
                    anomaly_type="work_anomaly",
                    severity=_severity_float(result["severity"]),
                    source="work_engine",
                )
                db.session.add(anomaly)
                db.session.flush()

                from work_engine.scoring import award_work_points

                award_work_points(
                    worker_id=wid,
                    reason_key="work_anomaly_detected",
                    source="work_engine",
                    note=f"Work anomaly detected (severity: {result['severity']})",
                    org_id=org_id,
                )

                result["anomaly_id"] = anomaly.id
                results.append(result)
        except Exception as e:
            results.append({"worker_id": wid, "error": str(e)})

    db.session.commit()
    return results


def detect_work_burnout(worker_id: int, org_id: int, days: int = 30) -> dict:
    """
    Heuristic burnout check using work feature vector only.

    Checks four signals:
    - late_ratio > 40 %
    - miss_acceleration > 0.3
    - declining score slope < -0.2
    - streak collapsed to 0 after being 7+

    Returns dict with burnout_risk (none/low/medium/high) and triggered signals.
    """
    fv = work_feature_vector_for_worker(worker_id, org_id, days=days)

    signals = []

    if fv[6] > BURNOUT_SIGNALS["late_ratio_threshold"]:
        signals.append("high_late_ratio")

    if fv[5] > BURNOUT_SIGNALS["miss_acceleration_threshold"]:
        signals.append("miss_acceleration")

    if fv[7] < BURNOUT_SIGNALS["score_trend_negative"]:
        signals.append("declining_score")

    if BURNOUT_SIGNALS["streak_collapse"]:
        streak = int(fv[4] * 30)
        if streak == 0:
            signals.append("streak_collapse")

    if len(signals) >= 3:
        risk = "high"
    elif len(signals) >= 2:
        risk = "medium"
    elif len(signals) >= 1:
        risk = "low"
    else:
        risk = "none"

    return {
        "worker_id": worker_id,
        "burnout_risk": risk,
        "signals": signals,
        "feature_vector": fv.tolist(),
    }
