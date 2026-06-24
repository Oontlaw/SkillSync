import sys; sys.path.insert(0, '.')
from app import app
from database import db, PredictionLog, BehavioralAnomaly, BurnoutRisk, MemberJoinLeave, GuildInfo, GuildMember
import os; os.environ['FLASK_ENV'] = 'development'
with app.app_context():
    print('=== DB Health ===')
    print(f'Tables: {len(db.metadata.tables)}')
    print(f'PredictionLogs: {PredictionLog.query.count()}')
    print(f'Anomalies: {BehavioralAnomaly.query.count()}')
    print(f'Burnout risks: {BurnoutRisk.query.count()}')
    print(f'Join/Leave events: {MemberJoinLeave.query.count()}')
    print(f'Guilds: {GuildInfo.query.count()}')
    print(f'Tracked members: {GuildMember.query.count()}')
    
    print('ML Status:')
    from ml.engine import get_model_status
    status = get_model_status()
    for k, v in status.items():
        if isinstance(v, dict) and 'trained' in v:
            print(f'  {k}: trained={v["trained"]}')
        else:
            print(f'  {k}: {type(v).__name__}')
