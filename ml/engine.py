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

from ml import anomaly, forecast, burnout, corrector, federated, growth

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')
TRAINING_HISTORY_PATH = os.path.join(MODELS_DIR, 'training_history.json')


def _load_training_history():
    """Load training history from file."""
    if os.path.exists(TRAINING_HISTORY_PATH):
        try:
            with open(TRAINING_HISTORY_PATH, 'r') as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_training_history(history):
    """Save training history to file."""
    os.makedirs(MODELS_DIR, exist_ok=True)
    with open(TRAINING_HISTORY_PATH, 'w') as f:
        json.dump(history, f, default=str)


def resolve_all_outcomes():
    """Resolve pending predictions across all models."""
    from ml.forecast import resolve_outcomes
    from ml.corrector import resolve_corrector_outcomes
    resolved = resolve_outcomes(days_back=7)
    results = {'forecast_resolved': resolved}
    try:
        results['anomaly'] = anomaly.resolve_anomaly_outcomes(days_back=30)
    except Exception as e:
        results['anomaly'] = {'error': str(e)}
    try:
        results['burnout'] = burnout.resolve_burnout_outcomes(days_back=30)
    except Exception as e:
        results['burnout'] = {'error': str(e)}
    try:
        results['corrector'] = resolve_corrector_outcomes(days_back=30)
    except Exception as e:
        results['corrector'] = {'error': str(e)}
    return results


def get_all_accuracy_metrics(days=7):
    """Return accuracy metrics for all models."""
    from ml.forecast import get_accuracy_metrics
    return {
        'forecast': get_accuracy_metrics(days=days),
    }


def train_all(days=30, min_msgs=10):
    """Train all ML models and return results."""
    results = {}

    # 0. Resolve pending predictions first
    try:
        results['outcome_resolution'] = resolve_all_outcomes()
    except Exception as e:
        results['outcome_resolution'] = {'error': str(e)}

    # 1. Anomaly detection
    try:
        results['anomaly'] = anomaly.train(min_msgs=min_msgs, days=days)
    except Exception as e:
        results['anomaly'] = {'status': 'error', 'error': str(e)}

    # 2. Score Corrector (admin correction feedback loop)
    try:
        results['corrector'] = corrector.train(days=days)
    except Exception as e:
        results['corrector'] = {'status': 'error', 'error': str(e)}

    # 3. Burnout risk
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

    # 5. Federated learning
    try:
        results['federated'] = federated.train_federated(days=days)
    except Exception as e:
        results['federated'] = {'status': 'error', 'error': str(e)}

    # 6. Growth models per guild
    from database import GuildInfo
    try:
        guilds = GuildInfo.query.all()
        growth_results = []
        for g in guilds:
            try:
                growth_results.append(growth.train(g.guild_id, days=days))
            except Exception as e:
                growth_results.append({'guild_id': g.guild_id, 'status': 'error', 'error': str(e)})
        results['growth'] = {'guilds': len(growth_results), 'results': growth_results}
    except Exception as e:
        results['growth'] = {'status': 'error', 'error': str(e)}

    results['trained_at'] = datetime.utcnow().isoformat()

    # Save to training history
    history = _load_training_history()
    history.append(results)
    # Keep last 50 training runs
    if len(history) > 50:
        history = history[-50:]
    _save_training_history(history)

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
        ('corrector', corrector.CORRECTOR_MODEL_PATH),
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

    # Federated learning
    fed_status = federated.get_status()
    status['federated'] = {
        'trained': fed_status['trained'],
        'rounds': fed_status['rounds'],
        'clients': fed_status['clients'],
        'mean_global_accuracy': fed_status.get('mean_global_accuracy'),
        'mean_local_accuracy': fed_status.get('mean_local_accuracy'),
        'accuracy_gap': fed_status.get('accuracy_gap'),
        'baseline_accuracy': fed_status.get('baseline_accuracy'),
        'global_vs_baseline': fed_status.get('global_vs_baseline'),
        'history': fed_status.get('history'),
    }

    # Prediction accuracy metrics (last 7 days)
    try:
        status['accuracy_metrics'] = get_all_accuracy_metrics(days=7)
    except Exception as e:
        status['accuracy_metrics'] = {'error': str(e)}

    # Precision/recall from admin feedback
    try:
        status['anomaly_precision'] = anomaly.get_precision_recall(days=30)
    except Exception as e:
        status['anomaly_precision'] = {'error': str(e)}
    try:
        status['burnout_precision'] = burnout.get_precision_recall(days=30)
    except Exception as e:
        status['burnout_precision'] = {'error': str(e)}

    # Training history
    status['training_history'] = _load_training_history()

    # Drift health check
    try:
        status['health'] = get_model_health()
    except Exception as e:
        status['health'] = {'error': str(e)}

    # Growth model status
    try:
        growth_count = len([f for f in os.listdir(MODELS_DIR) if f.startswith('growth_') and f.endswith('.joblib')])
        status['growth'] = {'trained': growth_count > 0, 'guild_count': growth_count}
    except Exception as e:
        status['growth'] = {'error': str(e)}

    return status


def get_model_health():
    """Check for model drift — returns dict with drift_detected and drift_reasons."""
    health = {
        'drift_detected': False,
        'drift_reasons': [],
        'forecast_accuracy': None,
        'anomaly_precision': None,
        'burnout_precision': None,
    }

    try:
        forecast_metrics = get_all_accuracy_metrics(days=7)
        forecast_data = forecast_metrics.get('forecast', {})
        accuracy_pct = forecast_data.get('accuracy_pct')
        if accuracy_pct is not None:
            health['forecast_accuracy'] = accuracy_pct
            if accuracy_pct < 50:
                health['drift_detected'] = True
                health['drift_reasons'].append(f"Forecast accuracy {accuracy_pct}% < 50%")
    except Exception as e:
        health['forecast_accuracy_error'] = str(e)

    try:
        anomaly_pr = anomaly.get_precision_recall(days=30)
        if anomaly_pr.get('precision') is not None:
            health['anomaly_precision'] = anomaly_pr['precision']
            if anomaly_pr.get('total_with_feedback', 0) >= 3 and anomaly_pr['precision'] < 0.5:
                health['drift_detected'] = True
                health['drift_reasons'].append(f"Anomaly precision {anomaly_pr['precision_pct']}% < 50% (3+ feedback samples)")
    except Exception as e:
        health['anomaly_precision_error'] = str(e)

    try:
        burnout_pr = burnout.get_precision_recall(days=30)
        if burnout_pr.get('precision') is not None:
            health['burnout_precision'] = burnout_pr['precision']
            if burnout_pr.get('total_with_feedback', 0) >= 3 and burnout_pr['precision'] < 0.5:
                health['drift_detected'] = True
                health['drift_reasons'].append(f"Burnout precision {burnout_pr['precision_pct']}% < 50% (3+ feedback samples)")
    except Exception as e:
        health['burnout_precision_error'] = str(e)

    return health
