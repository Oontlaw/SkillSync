import sys; sys.path.insert(0, '.')
from app import app
from database import db
import os; os.environ['FLASK_ENV'] = 'development'
with app.app_context():
    from sqlalchemy import text
    result = db.session.execute(text("SELECT pg_size_pretty(pg_database_size(current_database())) as db_size, (SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public') as table_count")).fetchone()
    print(f'DB size: {result[0]}, Tables: {result[1]}')
    result2 = db.session.execute(text("SELECT relname as table, n_live_tup as rows FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10")).fetchall()
    print('Top 10 tables by row count:')
    for r in result2:
        print(f'  {r[0]}: {r[1]} rows')
