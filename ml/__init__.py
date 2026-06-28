import os

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
os.makedirs(MODELS_DIR, exist_ok=True)


def model_path(*parts):
    """
    Build a path under ml/models/.
    Usage:
        model_path("anomaly", guild_id)         → ml/models/anomaly_{guild_id}.joblib
        model_path("forecast", guild_id)        → ml/models/forecast_{guild_id}.joblib
        model_path("growth", guild_id, "join")  → ml/models/growth_{guild_id}_join.joblib
        model_path("work_anomaly", org_id)      → ml/models/work_anomaly_{org_id}.joblib
    """
    name = "_".join(str(p) for p in parts) + ".joblib"
    return os.path.join(MODELS_DIR, name)


from . import (
    anomaly,
    burnout,
    corrector,
    engine,
    features,
    federated,
    forecast,
    work_anomaly,
    work_features,
)

__all__ = [
    "features",
    "anomaly",
    "forecast",
    "burnout",
    "corrector",
    "engine",
    "federated",
    "growth",
    "work_features",
    "work_anomaly",
    "model_path",
]
