"""
ML Engine — orchestrates training across all models.
Call `train_all()` to retrain all models. Models are persisted to ml/models/*.joblib.
Integration points:
  - routes/observer.py: POST /observer/ml/retrain, replace heuristic anomaly/burnout
  - bot.py background loop: periodic retrain every 6h
"""
import os
import json
import numpy as np
from datetime import datetime

from ml import anomaly, forecast, burnout

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')


def train_all(days=30, min_msgs=10):
    """Train all ML models and return results."""
    results = {}

    # 1. Anomaly detection
    try:
        results['anomaly'] = anomaly.train(min_msgs=min_msgs, days=days)
    except Exception as e:
        results['anomaly'] = {'status': 'error', 'error': str(e)}

    # 2. Burnout risk
    try:
        results['burnout'] = burnout.train(days=days)
    except Exception as e:
        results['burnout'] = {'status': 'error', 'error': str(e)}

    # 3. Forecast for each guild
    from database import GuildInfo
    try:
        guilds = GuildInfo.query.all()
        forecast_results = []
        for g in guilds:
            try:
                forecast_results.append(forecast.train(g.guild_id, days=days))
            except Exception as e:
                forecast_results.append({'guild_id': g.guild_id, 'status': 'error', 'error': str(e)})
        results['forecast'] = {'guilds': len(forecast_results), 'results': forecast_results}
    except Exception as e:
        results['forecast'] = {'status': 'error', 'error': str(e)}

    results['trained_at'] = datetime.utcnow().isoformat()

    # Write summary
    summary_path = os.path.join(MODELS_DIR, 'training_summary.json')
    os.makedirs(MODELS_DIR, exist_ok=True)
    with open(summary_path, 'w') as f:
        json.dump(results, f, default=str)

    return results


def get_model_status():
    """Return status of all trained models."""
    status = {}
    for name, path in [
        ('anomaly', anomaly.ANOMALY_MODEL_PATH),
        ('burnout', burnout.BURNOUT_MODEL_PATH),
    ]:
        exists = os.path.exists(path)
        if exists:
            size = os.path.getsize(path)
            modified = datetime.fromtimestamp(os.path.getmtime(path)).isoformat()
            status[name] = {'trained': True, 'size_bytes': size, 'last_modified': modified}
        else:
            status[name] = {'trained': False}

    # Check forecast models
    forecast_count = len([f for f in os.listdir(MODELS_DIR) if f.startswith('forecast_') and f.endswith('.joblib')])
    status['forecast'] = {'trained': forecast_count > 0, 'guild_count': forecast_count}

    return status
