import os
import numpy as np
import joblib
from sklearn.ensemble import RandomForestRegressor
from ml.features import guild_forecast_features

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')


def _model_path(guild_id):
    return os.path.join(MODELS_DIR, f'forecast_{guild_id}.joblib')


def train(guild_id, days=30):
    """Train an hourly activity forecast model for a guild."""
    X, y, _ = guild_forecast_features(guild_id, days=days)
    if X is None or len(y) < 48:
        return {'status': 'skipped', 'reason': f'Only {len(y) if y is not None else 0} hourly data points'}
    model = RandomForestRegressor(
        n_estimators=100,
        max_depth=10,
        min_samples_leaf=4,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X, y)
    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(model, _model_path(guild_id))
    score = model.score(X, y)
    return {'status': 'trained', 'guild_id': guild_id, 'r2_score': round(float(score), 3), 'samples': len(y)}


def predict_next_24h(guild_id, days=30):
    """Predict message counts for the next 24 hours (hours 0-23).
    Returns array of shape (24,) with predicted counts per hour."""
    path = _model_path(guild_id)
    if not os.path.exists(path):
        return None
    model = joblib.load(path)
    # Build prediction matrix for next 24 hours
    from datetime import datetime
    now = datetime.utcnow()
    current_dow = now.weekday()
    # Get rolling averages from recent history
    X_hist, _, _ = guild_forecast_features(guild_id, days=days)
    if X_hist is None or len(X_hist) < 24:
        return None
    last_counts = X_hist[:, -3:].mean(axis=0) if X_hist.shape[1] >= 3 else np.array([0, 0, 0])
    # Build 24 hourly feature vectors
    X_pred = []
    for h in range(24):
        hour_sin = np.sin(2 * np.pi * h / 24)
        hour_cos = np.cos(2 * np.pi * h / 24)
        dow_sin = np.sin(2 * np.pi * current_dow / 7)
        dow_cos = np.cos(2 * np.pi * current_dow / 7)
        X_pred.append([hour_sin, hour_cos, dow_sin, dow_cos, last_counts[0], last_counts[1], last_counts[2]])
    X_pred = np.array(X_pred)
    preds = model.predict(X_pred)
    preds = np.maximum(0, np.round(preds)).astype(int)
    return preds


def train_all_guilds(days=30):
    """Train forecast models for all guilds with sufficient data."""
    from database import GuildInfo
    guilds = GuildInfo.query.all()
    results = []
    for g in guilds:
        result = train(g.guild_id, days=days)
        results.append({'guild_id': g.guild_id, 'name': g.name, **result})
    return results
