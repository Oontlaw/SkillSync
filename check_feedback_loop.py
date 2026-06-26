import json

from app import app
from database import PredictionLog, db

with app.app_context():
    # Count total forecast predictions
    total = PredictionLog.query.filter(PredictionLog.model_name == "forecast").count()
    print(f"Total forecast predictions: {total}")

    # Check if hour_error_history is being populated
    logs = (
        PredictionLog.query.filter(
            PredictionLog.model_name == "forecast",
            PredictionLog.hour_error_history != None,
        )
        .order_by(PredictionLog.prediction_time.desc())
        .limit(5)
        .all()
    )

    print(f"\nPredictions with error history: {len(logs)}")
    for log in logs:
        print(
            f"- ID: {log.id}, Time: {log.prediction_time}, Error History: {log.hour_error_history}"
        )

    # Check if errors are being resolved
    resolved = (
        PredictionLog.query.filter(
            PredictionLog.model_name == "forecast", PredictionLog.actual_value != None
        )
        .order_by(PredictionLog.prediction_time.desc())
        .limit(5)
        .all()
    )

    print(f"\nResolved predictions: {len(resolved)}")
    for log in resolved:
        print(
            f"- ID: {log.id}, Predicted: {log.prediction_value}, Actual: {log.actual_value}, Error: {log.error_signed}"
        )
