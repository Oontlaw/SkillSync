from app import app
from ml.forecast import resolve_outcomes

with app.app_context():
    print("Running resolve_outcomes()...")
    resolved_count = resolve_outcomes(days_back=7)
    print(f"Resolved {resolved_count} predictions.")
