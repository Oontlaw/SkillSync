import os
import json
import numpy as np
from collections import defaultdict
from datetime import datetime, timedelta
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
import joblib

from database import db, MessageRecord, GuildInfo

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')
GLOBAL_MODEL_PATH = os.path.join(MODELS_DIR, 'federated_global.joblib')
HISTORY_PATH = os.path.join(MODELS_DIR, 'federated_history.json')
CLIENTS_DIR = os.path.join(MODELS_DIR, 'federated_clients')


def _features_and_target(days=30, test_size=0.2, random_state=None):
    """Query message_records and return (X_train, y_train, X_test, y_test) per guild.
    
    Features (6-dim):
      0: hour_sin        — cyclical encoding of hour_of_day
      1: hour_cos        — cyclical encoding of hour_of_day
      2: day_of_week     — 0=Monday .. 6=Sunday
      3: log_len         — log1p(message_length), clamped to [0, 2000]
      4: is_public       — 1 if public channel, 0 otherwise
      5: is_weekend      — 1 if day_of_week >= 5, 0 otherwise
    y: is_off_hours (1 if hour 22-6, else 0)
    Returns dict: guild_id -> {X_train, y_train, X_test, y_test, count}
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = MessageRecord.query.filter(
        MessageRecord.created_at >= cutoff,
        MessageRecord.hour_of_day != None,
        MessageRecord.message_length != None,
    ).with_entities(
        MessageRecord.guild_id,
        MessageRecord.hour_of_day,
        MessageRecord.day_of_week,
        MessageRecord.message_length,
        MessageRecord.is_public_channel,
    ).all()

    guild_data = defaultdict(lambda: {'features': [], 'targets': []})
    for r in rows:
        guild_id = str(r.guild_id)
        h = r.hour_of_day
        is_off = 1 if h >= 22 or h <= 6 else 0
        hour_sin = np.sin(2 * np.pi * h / 24)
        hour_cos = np.cos(2 * np.pi * h / 24)
        length = min(r.message_length, 2000)
        log_len = np.log1p(length)
        guild_data[guild_id]['features'].append([
            hour_sin,
            hour_cos,
            r.day_of_week,
            log_len,
            1 if r.is_public_channel else 0,
            1 if r.day_of_week >= 5 else 0,
        ])
        guild_data[guild_id]['targets'].append(is_off)

    result = {}
    for gid, data in guild_data.items():
        arr = np.array(data['features'], dtype=np.float64)
        labels = np.array(data['targets'], dtype=np.int64)
        if len(labels) < 10:
            continue
        if len(np.unique(labels)) < 2:
            continue
        X_train, X_test, y_train, y_test = train_test_split(
            arr, labels, test_size=test_size, random_state=random_state, stratify=labels
        )
        result[gid] = {
            'X_train': X_train, 'y_train': y_train,
            'X_test': X_test, 'y_test': y_test,
            'count': len(labels),
        }
    return result


def train_client(X, y):
    """Train a LogisticRegression on one guild's training data."""
    model = LogisticRegression(
        C=1.0, max_iter=1000, solver='lbfgs', random_state=42
    )
    model.fit(X, y)
    return model


def fed_avg(clients):
    """Weighted average of client model coefficients by sample count."""
    total = sum(c['count'] for c in clients.values())
    if total == 0:
        return None, None
    coef_shape = clients[list(clients.keys())[0]]['model'].coef_.shape
    avg_coef = np.zeros(coef_shape)
    avg_intercept = 0.0
    for gid, c in clients.items():
        w = c['count'] / total
        avg_coef += w * c['model'].coef_
        avg_intercept += w * c['model'].intercept_[0]
    return avg_coef, avg_intercept


def evaluate_global(global_coef, global_intercept, clients):
    """Evaluate global model on each client's TEST set."""
    results = []
    for gid, c in clients.items():
        X_test, y_test = c['X_test'], c['y_test']
        X_train, y_train = c['X_train'], c['y_train']
        # Global model on test set
        logits = X_test @ global_coef.T + global_intercept
        global_preds = (logits.flatten() >= 0).astype(int)
        # Local model on test set
        local_preds = c['model'].predict(X_test)
        # Baseline: always predict majority class of training set
        majority = int(y_train.mean() >= 0.5)
        baseline_preds = np.full_like(y_test, majority)
        results.append({
            'guild_id': gid,
            'train_count': len(y_train),
            'test_count': len(y_test),
            'local_accuracy': float(accuracy_score(y_test, local_preds)),
            'global_accuracy': float(accuracy_score(y_test, global_preds)),
            'baseline_accuracy': float(accuracy_score(y_test, baseline_preds)),
        })
    return results


def train_federated(days=30):
    """Full FedAvg round: split by guild, train clients, aggregate, evaluate on test set."""
    rs = np.random.randint(0, 2**31)
    guild_data = _features_and_target(days=days, test_size=0.2, random_state=rs)
    if len(guild_data) < 2:
        return {'status': 'skipped', 'reason': f'Need >=2 guilds with data, got {len(guild_data)}'}

    clients = {}
    for gid, data in guild_data.items():
        model = train_client(data['X_train'], data['y_train'])
        clients[gid] = {
            'model': model, 'count': data['count'],
            'X_train': data['X_train'], 'y_train': data['y_train'],
            'X_test': data['X_test'], 'y_test': data['y_test'],
        }

    global_coef, global_intercept = fed_avg(clients)
    if global_coef is None:
        return {'status': 'error', 'reason': 'FedAvg returned None'}

    eval_results = evaluate_global(global_coef, global_intercept, clients)

    # Save global model
    global_model = LogisticRegression(C=1.0, max_iter=1000, solver='lbfgs', random_state=42)
    global_model.coef_ = global_coef
    global_model.intercept_ = np.array([global_intercept])
    global_model.classes_ = np.array([0, 1])
    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(global_model, GLOBAL_MODEL_PATH)

    # Save client models
    os.makedirs(CLIENTS_DIR, exist_ok=True)
    for gid, c in clients.items():
        joblib.dump(c['model'], os.path.join(CLIENTS_DIR, f'client_{gid}.joblib'))

    # Update history
    history = []
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH) as f:
                history = json.load(f)
        except Exception:
            pass

    round_num = len(history) + 1
    global_acc = float(np.mean([r['global_accuracy'] for r in eval_results]))
    local_acc = float(np.mean([r['local_accuracy'] for r in eval_results]))
    baseline_acc = float(np.mean([r['baseline_accuracy'] for r in eval_results]))
    entry = {
        'round': round_num,
        'clients': len(clients),
        'total_messages': sum(c['count'] for c in clients.values()),
        'mean_local_accuracy': local_acc,
        'mean_global_accuracy': global_acc,
        'accuracy_gap': local_acc - global_acc,
        'baseline_accuracy': baseline_acc,
        'global_vs_baseline': global_acc - baseline_acc,
        'clients_detail': eval_results,
        'random_state': rs,
        'trained_at': datetime.utcnow().isoformat(),
    }
    history.append(entry)
    with open(HISTORY_PATH, 'w') as f:
        json.dump(history, f, default=str, indent=2)

    return entry


def get_status():
    """Return federated learning status."""
    status = {
        'trained': os.path.exists(GLOBAL_MODEL_PATH),
        'rounds': 0,
        'clients': [],
        'last_trained': None,
        'history': [],
    }
    if os.path.exists(GLOBAL_MODEL_PATH):
        try:
            stats = os.stat(GLOBAL_MODEL_PATH)
            status['last_trained'] = datetime.fromtimestamp(stats.st_mtime).isoformat()
        except Exception:
            pass
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH) as f:
                history = json.load(f)
            status['rounds'] = len(history)
            status['history'] = history
            if history:
                last = history[-1]
                status['clients'] = last.get('clients_detail', [])
                status['mean_global_accuracy'] = last.get('mean_global_accuracy')
                status['mean_local_accuracy'] = last.get('mean_local_accuracy')
                status['accuracy_gap'] = last.get('accuracy_gap')
                status['baseline_accuracy'] = last.get('baseline_accuracy')
                status['global_vs_baseline'] = last.get('global_vs_baseline')
        except Exception:
            pass
    return status
