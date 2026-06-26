import json

from app import app
from database import PredictionLog, db

with app.app_context():
    # Get the latest 5 predictions
    latest = (
        PredictionLog.query.filter(PredictionLog.model_name == "forecast")
        .order_by(PredictionLog.prediction_time.desc())
        .limit(5)
        .all()
    )

    print(f"Latest {len(latest)} predictions:")
    for log in latest:
        meta = json.loads(log.metadata_json) if log.metadata_json else {}
        print(
            f"- ID: {log.id}, Time: {log.prediction_time}, Guild: {meta.get('guild_id')}, Hour: {meta.get('predicted_hour')}"
        )
        print(
            f"  Predicted: {log.prediction_value}, Actual: {log.actual_value}, Error History: {log.hour_error_history}"
        )
