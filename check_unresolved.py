from app import app
from database import PredictionLog, db

with app.app_context():
    unresolved = PredictionLog.query.filter(
        PredictionLog.model_name == "forecast", PredictionLog.actual_value == None
    ).count()
    print(f"Unresolved predictions: {unresolved}")

    # Check if resolve_outcomes() is being called
    from ml.forecast import resolve_outcomes

    resolved_count = resolve_outcomes(days_back=7)
    print(f"Resolved {resolved_count} predictions in this run.")

    # Check again
    unresolved_after = PredictionLog.query.filter(
        PredictionLog.model_name == "forecast", PredictionLog.actual_value == None
    ).count()
    print(f"Unresolved predictions after resolve_outcomes(): {unresolved_after}")
