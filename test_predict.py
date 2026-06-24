import sys; sys.path.insert(0, '.')
from app import app
import os; os.environ['FLASK_ENV'] = 'development'

with app.app_context():
    # Test anomaly predict on a real user
    from ml.anomaly import predict as anomaly_predict
    from database import GuildMember
    member = GuildMember.query.filter(GuildMember.is_bot == False).first()
    if member:
        print(f'Testing anomaly predict on {member.member_id}')
        result = anomaly_predict(member.member_id, days=30)
        print(f'Result: {result}')
    else:
        print('No members found')
    
    # Check PredictionLog after
    from database import db, PredictionLog
    for m in ['anomaly', 'burnout', 'corrector']:
        cnt = PredictionLog.query.filter(PredictionLog.model_name == m).count()
        print(f'  {m}: total={cnt}')
