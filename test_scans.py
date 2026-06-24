import sys; sys.path.insert(0, '.')
from app import app
import os; os.environ['FLASK_ENV'] = 'development'

with app.app_context():
    # Test anomaly scan
    from ml.anomaly import scan_all
    result = scan_all(min_msgs=1, days=1)
    print(f'Anomaly scan_all returned {len(result)} anomalies')
    
    # Test burnout scan  
    from ml.burnout import scan_all as burnout_scan
    result2 = burnout_scan(days=1)
    print(f'Burnout scan_all returned {len(result2)} risks')
    
    # Test corrector predict
    from ml.corrector import predict
    result3 = predict(original_change=5.0, worker_id=1)
    print(f'Corrector predict: {result3}')

    # Check PredictionLog after
    from database import db, PredictionLog
    for m in ['anomaly', 'burnout', 'corrector']:
        cnt = PredictionLog.query.filter(PredictionLog.model_name == m).count()
        print(f'  {m}: total={cnt}')
