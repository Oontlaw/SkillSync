"""
ML Engine — orchestrates training across all models.
Call `train_all()` to retrain all models. Models are persisted to ml/models/*.joblib.
Integration points:
  - routes/observer.py: POST /observer/ml/retrain, replace heuristic anomaly/burnout
  - bot.py background loop: periodic retrain every 6h
"""

import json
import os
import re
import shutil
from datetime import datetime, timedelta

import numpy as np

from database import ModelRegistry, db
from ml import anomaly, burnout, corrector, federated, forecast, growth

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
TRAINING_HISTORY_PATH = os.path.join(MODELS_DIR, "training_history.json")


def _load_training_history():
    """Load training history from file."""
    if os.path.exists(TRAINING_HISTORY_PATH):
        try:
            with open(TRAINING_HISTORY_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_training_history(history):
    """Save training history to file."""
    os.makedirs(MODELS_DIR, exist_ok=True)
    with open(TRAINING_HISTORY_PATH, "w") as f:
        json.dump(history, f, default=str)


def resolve_all_outcomes():
    """Resolve pending predictions across all models."""
    from ml.corrector import resolve_corrector_outcomes
    from ml.forecast import resolve_outcomes

    resolved = resolve_outcomes(days_back=7)
    results = {"forecast_resolved": resolved}
    try:
        results["anomaly"] = anomaly.resolve_anomaly_outcomes(days_back=30)
    except Exception as e:
        results["anomaly"] = {"error": str(e)}
    try:
        results["burnout"] = burnout.resolve_burnout_outcomes(days_back=30)
    except Exception as e:
        results["burnout"] = {"error": str(e)}
    try:
        results["corrector"] = resolve_corrector_outcomes(days_back=30)
    except Exception as e:
        results["corrector"] = {"error": str(e)}
    return results


def get_all_accuracy_metrics(days=7):
    """Return accuracy metrics for all models."""
    from ml.forecast import get_accuracy_metrics

    metrics = {"forecast": get_accuracy_metrics(days=days)}
    try:
        metrics["anomaly"] = anomaly.get_precision_recall(days=30)
    except Exception as e:
        metrics["anomaly"] = {"error": str(e)}
    try:
        metrics["burnout"] = burnout.get_precision_recall(days=30)
    except Exception as e:
        metrics["burnout"] = {"error": str(e)}
    return metrics


def _register_model_artifact(
    model_name,
    artifact_path,
    result,
    *,
    train_days,
    guild_id=None,
    n_samples=None,
    extra_artifact_paths=(),
):
    """Version a successfully trained artifact and atomically activate it."""
    if not os.path.isfile(artifact_path):
        raise FileNotFoundError(f"Trained model artifact not found: {artifact_path}")
    guild_id = str(guild_id) if guild_id is not None else None
    query = ModelRegistry.query.filter_by(model_name=model_name)
    query = query.filter(
        ModelRegistry.guild_id.is_(None)
        if guild_id is None
        else ModelRegistry.guild_id == guild_id
    )
    latest = query.order_by(ModelRegistry.version.desc()).first()
    version = latest.version + 1 if latest else 1
    scope = re.sub(r"[^A-Za-z0-9._-]", "_", guild_id or "global")
    version_dir = os.path.join(
        MODELS_DIR, "versions", model_name, scope, f"v{version}"
    )
    os.makedirs(version_dir, exist_ok=True)
    versioned_path = os.path.join(version_dir, os.path.basename(artifact_path))
    shutil.copy2(artifact_path, versioned_path)
    for path in extra_artifact_paths:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Related model artifact not found: {path}")
        shutil.copy2(path, os.path.join(version_dir, os.path.basename(path)))

    query.update({"is_active": False}, synchronize_session=False)
    if n_samples is None:
        n_samples = next(
            (
                result[key]
                for key in ("samples", "users", "workers", "corrections_used", "total_messages")
                if isinstance(result.get(key), (int, float))
            ),
            0,
        )
    metrics = {
        key: value
        for key, value in result.items()
        if key not in {"model_path", "artifact_path"}
    }
    db.session.add(
        ModelRegistry(
            model_name=model_name,
            version=version,
            guild_id=guild_id,
            trained_at=datetime.utcnow(),
            train_start=datetime.utcnow() - timedelta(days=train_days),
            train_end=datetime.utcnow(),
            n_samples=int(n_samples or 0),
            metrics=json.loads(json.dumps(metrics, default=str)),
            artifact_path=versioned_path,
            is_active=True,
        )
    )
    db.session.commit()
    return versioned_path


def _register_if_trained(model_name, result, artifact_path, *, days, **kwargs):
    if result.get("status") != "trained":
        return None
    return _register_model_artifact(
        model_name, artifact_path, result, train_days=days, **kwargs
    )


def train_growth_all(days=90):
    """Train and register join/leave models for every guild."""
    from database import GuildInfo

    results = []
    for guild in GuildInfo.query.all():
        try:
            result = growth.train(guild.guild_id, days=days)
            if result.get("status") == "trained":
                for target in ("join", "leave"):
                    _register_model_artifact(
                        f"growth_{target}",
                        growth.model_path("growth", guild.guild_id, target),
                        result[target],
                        train_days=days,
                        guild_id=guild.guild_id,
                        n_samples=result[target].get("samples"),
                    )
            results.append({"guild_id": guild.guild_id, "name": guild.name, **result})
        except Exception as e:
            results.append(
                {"guild_id": guild.guild_id, "status": "error", "error": str(e)}
            )
    return results


def train_all(days=30, min_msgs=10):
    """Train all ML models and return results."""
    results = {}

    # 0a. Update long-term baselines (must run before all models)
    try:
        from ml.features import update_guild_baselines, update_user_baselines

        user_baseline_count = update_user_baselines(days_long=90, days_short=7)
        guild_baseline_count = update_guild_baselines()
        results["baselines"] = {
            "user_baselines_updated": user_baseline_count,
            "guild_baselines_updated": guild_baseline_count,
        }
    except Exception as e:
        results["baselines"] = {"error": str(e)}

    # 0b. Resolve pending predictions first
    try:
        results["outcome_resolution"] = resolve_all_outcomes()
    except Exception as e:
        results["outcome_resolution"] = {"error": str(e)}

    # 1. Anomaly detection
    try:
        results["anomaly"] = anomaly.train(min_msgs=min_msgs, days=days)
        _register_if_trained(
            "anomaly",
            results["anomaly"],
            results["anomaly"].get("model_path", anomaly.ANOMALY_MODEL_PATH),
            days=days,
            n_samples=results["anomaly"].get("users"),
        )
    except Exception as e:
        results["anomaly"] = {"status": "error", "error": str(e)}

    # 2. Score Corrector (admin correction feedback loop)
    try:
        results["corrector"] = corrector.train(days=days)
        _register_if_trained(
            "corrector",
            results["corrector"],
            corrector.CORRECTOR_MODEL_PATH,
            days=days,
            n_samples=results["corrector"].get("corrections_used"),
            extra_artifact_paths=(corrector.SCALER_PATH,),
        )
    except Exception as e:
        results["corrector"] = {"status": "error", "error": str(e)}

    # 3. Burnout risk
    try:
        results["burnout"] = burnout.train(days=days)
    except Exception as e:
        results["burnout"] = {"status": "error", "error": str(e)}

    # 3. Forecast for each guild
    from database import GuildInfo

    try:
        guilds = GuildInfo.query.all()
        forecast_results = []
        for g in guilds:
            try:
                result = forecast.train(g.guild_id, days=days)
                _register_if_trained(
                    "forecast",
                    result,
                    forecast.model_path("forecast", g.guild_id),
                    days=days,
                    guild_id=g.guild_id,
                    n_samples=result.get("samples"),
                )
                forecast_results.append(result)
            except Exception as e:
                forecast_results.append(
                    {"guild_id": g.guild_id, "status": "error", "error": str(e)}
                )
        results["forecast"] = {
            "guilds": len(forecast_results),
            "results": forecast_results,
        }
    except Exception as e:
        results["forecast"] = {"status": "error", "error": str(e)}

    # 5. Federated learning
    try:
        results["federated"] = federated.train_federated(days=days)
        if results["federated"].get("round"):
            _register_model_artifact(
                "federated",
                federated.GLOBAL_MODEL_PATH,
                results["federated"],
                train_days=days,
                n_samples=results["federated"].get("total_messages"),
            )
            for client in results["federated"].get("clients_detail", []):
                guild_id = str(client["guild_id"])
                client_path = os.path.join(
                    federated.CLIENTS_DIR, f"client_{guild_id}.joblib"
                )
                _register_model_artifact(
                    "federated_client",
                    client_path,
                    client,
                    train_days=days,
                    guild_id=guild_id,
                    n_samples=client.get("train_count"),
                )
    except Exception as e:
        results["federated"] = {"status": "error", "error": str(e)}

    # 6. Growth models per guild
    try:
        growth_results = train_growth_all(days=days)
        results["growth"] = {"guilds": len(growth_results), "results": growth_results}
    except Exception as e:
        results["growth"] = {"status": "error", "error": str(e)}

    # 7. Work engine anomaly per org
    from database import Organisation

    try:
        from ml import work_anomaly

        orgs = Organisation.query.filter_by(is_active=True).all()
        work_results = []
        for org in orgs:
            try:
                wr = work_anomaly.train(org_id=org.id, days=days)
                _register_if_trained(
                    f"work_anomaly_org_{org.id}",
                    wr,
                    os.path.join(MODELS_DIR, f"work_anomaly_{org.id}.joblib"),
                    days=days,
                )
                work_results.append({"org_id": org.id, "result": wr})
            except Exception as e:
                work_results.append(
                    {"org_id": org.id, "status": "error", "error": str(e)}
                )
        results["work_anomaly"] = {"orgs": len(work_results), "results": work_results}
    except Exception as e:
        results["work_anomaly"] = {"status": "error", "error": str(e)}

    results["trained_at"] = datetime.utcnow().isoformat()

    # Save to training history
    history = _load_training_history()
    history.append(results)
    # Keep last 50 training runs
    if len(history) > 50:
        history = history[-50:]
    _save_training_history(history)

    # Write summary
    summary_path = os.path.join(MODELS_DIR, "training_summary.json")
    os.makedirs(MODELS_DIR, exist_ok=True)
    with open(summary_path, "w") as f:
        json.dump(results, f, default=str)

    return results


def get_model_status():
    """Return status of all trained models."""
    status = {}
    for name, path in [
        ("anomaly", anomaly.ANOMALY_MODEL_PATH),
        ("burnout", burnout.BURNOUT_MODEL_PATH),
        ("corrector", corrector.CORRECTOR_MODEL_PATH),
    ]:
        exists = os.path.exists(path)
        if exists:
            size = os.path.getsize(path)
            modified = datetime.fromtimestamp(os.path.getmtime(path)).isoformat()
            status[name] = {
                "trained": True,
                "size_bytes": size,
                "last_modified": modified,
            }
        else:
            status[name] = {"trained": False}

    # Check forecast models
    forecast_count = len(
        [
            f
            for f in os.listdir(MODELS_DIR)
            if f.startswith("forecast_") and f.endswith(".joblib")
        ]
    )
    status["forecast"] = {"trained": forecast_count > 0, "guild_count": forecast_count}

    # Federated learning
    fed_status = federated.get_status()
    status["federated"] = {
        "trained": fed_status["trained"],
        "rounds": fed_status["rounds"],
        "clients": fed_status["clients"],
        "mean_global_accuracy": fed_status.get("mean_global_accuracy"),
        "mean_local_accuracy": fed_status.get("mean_local_accuracy"),
        "accuracy_gap": fed_status.get("accuracy_gap"),
        "baseline_accuracy": fed_status.get("baseline_accuracy"),
        "global_vs_baseline": fed_status.get("global_vs_baseline"),
        "history": fed_status.get("history"),
    }

    # Prediction accuracy metrics (last 7 days)
    try:
        status["accuracy_metrics"] = get_all_accuracy_metrics(days=7)
    except Exception as e:
        status["accuracy_metrics"] = {"error": str(e)}

    # Precision/recall from admin feedback
    try:
        status["anomaly_precision"] = anomaly.get_precision_recall(days=30)
    except Exception as e:
        status["anomaly_precision"] = {"error": str(e)}
    try:
        status["burnout_precision"] = burnout.get_precision_recall(days=30)
    except Exception as e:
        status["burnout_precision"] = {"error": str(e)}

    # Training history
    status["training_history"] = _load_training_history()

    # Drift health check
    try:
        status["health"] = get_model_health()
    except Exception as e:
        status["health"] = {"error": str(e)}

    # Growth model status
    try:
        growth_count = len(
            [
                f
                for f in os.listdir(MODELS_DIR)
                if f.startswith("growth_") and f.endswith(".joblib")
            ]
        )
        status["growth"] = {"trained": growth_count > 0, "guild_count": growth_count}
    except Exception as e:
        status["growth"] = {"error": str(e)}

    return status


def get_model_health():
    """Check for model drift — returns dict with drift_detected and drift_reasons.

    Uses deduplicated daily accuracy (by prediction run) for fair drift detection.
    Separates daily volume accuracy from hourly shape accuracy.
    """
    health = {
        "drift_detected": False,
        "drift_reasons": [],
        "forecast_accuracy": None,
        "forecast_daily_accuracy": None,
        "forecast_hourly_accuracy": None,
        "forecast_accuracy_by_lead_bucket": {},
        "anomaly_precision": None,
        "burnout_precision": None,
    }

    try:
        forecast_metrics = get_all_accuracy_metrics(days=7)
        forecast_data = forecast_metrics.get("forecast", {})
        daily = forecast_data.get("daily", {})
        hourly = forecast_data.get("hourly", {})

        # Daily volume accuracy (deduped by run)
        daily_acc = daily.get("accuracy_pct")
        health["forecast_daily_accuracy"] = daily_acc
        health["forecast_daily_samples"] = daily.get("samples", 0)

        # Hourly shape accuracy
        hourly_acc = hourly.get("accuracy_pct")
        health["forecast_hourly_accuracy"] = hourly_acc

        # Overall forecast accuracy (daily, for backward compat)
        health["forecast_accuracy"] = daily_acc

        # Per-lead-bucket accuracy
        health["forecast_accuracy_by_lead_bucket"] = daily.get("by_lead_bucket", {})

        # Drift detection uses deduped daily accuracy only
        if daily_acc is not None and daily.get("samples", 0) >= 2:
            if daily_acc < 50:
                health["drift_detected"] = True
                health["drift_reasons"].append(
                    f"Forecast daily accuracy {daily_acc}% ({daily.get('samples', 0)} runs) < 50%"
                )
    except Exception as e:
        health["forecast_accuracy_error"] = str(e)

    try:
        anomaly_pr = anomaly.get_precision_recall(days=30)
        if anomaly_pr.get("precision") is not None:
            health["anomaly_precision"] = anomaly_pr["precision"]
            if (
                anomaly_pr.get("total_with_feedback", 0) >= 3
                and anomaly_pr["precision"] < 0.5
            ):
                health["drift_detected"] = True
                health["drift_reasons"].append(
                    f"Anomaly precision {anomaly_pr['precision_pct']}% < 50% (3+ feedback samples)"
                )
    except Exception as e:
        health["anomaly_precision_error"] = str(e)

    try:
        burnout_pr = burnout.get_precision_recall(days=30)
        if burnout_pr.get("precision") is not None:
            health["burnout_precision"] = burnout_pr["precision"]
            if (
                burnout_pr.get("total_with_feedback", 0) >= 3
                and burnout_pr["precision"] < 0.5
            ):
                health["drift_detected"] = True
                health["drift_reasons"].append(
                    f"Burnout precision {burnout_pr['precision_pct']}% < 50% (3+ feedback samples)"
                )
    except Exception as e:
        health["burnout_precision_error"] = str(e)

    # Baseline confidence and drift count
    try:
        from database import UserBehaviorBaseline

        baselines = UserBehaviorBaseline.query.all()
        if baselines:
            mean_confidence = float(np.mean([b.baseline_confidence for b in baselines]))
            drifting_count = sum(1 for b in baselines if b.is_drifting)
            health["baseline_confidence"] = round(mean_confidence, 3)
            health["drifting_users"] = drifting_count
            health["total_baselines"] = len(baselines)
        else:
            health["baseline_confidence"] = 0.0
            health["drifting_users"] = 0
            health["total_baselines"] = 0
    except Exception as e:
        health["baseline_confidence_error"] = str(e)

    return health
