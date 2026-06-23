"""Migrate all data from SQLite to PostgreSQL."""
import os, sys
from datetime import datetime
from sqlalchemy import create_engine, MetaData, text, inspect

PG_URI = 'postgresql://skillsync:skillsync@127.0.0.1:5432/skillsync'
SQLITE_PATH = os.path.join(os.path.dirname(__file__), 'instance', 'skillsync.db')

sys.path.insert(0, os.path.dirname(__file__))
os.environ['DATABASE_URL'] = 'sqlite:///dummy.db'
os.environ['FLASK_SKIP_MIGRATE'] = '1'
os.environ['SECRET_KEY'] = 'migration'
os.environ['DISCORD_TOKEN'] = 'dummy'

from flask import Flask
from database import db

tmp = Flask(__name__)
tmp.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///dummy.db'
tmp.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(tmp)

with tmp.app_context():
    METADATA = db.metadata

# Columns that are Boolean in PG but stored as 0/1 in SQLite
# All boolean columns from SQLite (stored as 0/1) that PG expects as true/false
BOOLEAN_COLS = {
    'guild_info': ['store_content'],
    'guild_roles': ['is_admin', 'can_ban', 'can_kick', 'can_manage_messages',
                    'can_manage_guild', 'can_manage_roles', 'is_mod', 'is_manually_set'],
    'guild_members': ['is_bot', 'is_staff', 'is_online', 'is_owner', 'is_manually_set'],
    'guild_channels': ['is_public'],
    'automod_rules': ['enabled'],
    'role_change_log': ['was_staff_before', 'is_staff_now'],
    'message_records': ['is_public_channel'],
    'community_events': ['is_participating'],
    'work_pull_request': ['merged'],
    'tasks': ['extra_contribution'],
    'score_logs': ['admin_correction'],
    'pending_bans': ['auto_cleared'],
}

def create_pg_schema():
    pg_engine = create_engine(PG_URI)
    METADATA.create_all(pg_engine)
    inspector = inspect(pg_engine)
    tables = inspector.get_table_names()
    print(f"  Created {len(tables)} tables: {', '.join(tables)}")
    pg_engine.dispose()

def get_sqlite_data():
    import sqlite3
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cursor.fetchall()]
    data = {}
    for table in tables:
        cursor.execute(f'SELECT * FROM "{table}"')
        rows = [dict(r) for r in cursor.fetchall()]
        if rows:
            data[table] = rows
            print(f"  {table}: {len(rows)} rows")
    conn.close()
    return data

def insert_into_pg(data):
    pg_engine = create_engine(PG_URI)
    # Exact table names matching SQLAlchemy models
    table_order = [
        'workers',
        'guild_info', 'guild_roles', 'guild_members', 'guild_channels',
        'tasks', 'community_events', 'score_logs', 'admin_corrections',
        'message_records', 'voice_activity',
        'behavioral_anomalies', 'burnout_risks',
        'automod_rules', 'automod_triggers',
        'ping_join_events', 'mention_records',
        'role_change_log', 'pending_bans', 'pending_timeouts',
        'work_connection', 'work_pull_request',
    ]
    total = 0
    with pg_engine.connect() as conn:
        for table in table_order:
            if table not in data:
                continue
            rows = data[table]
            if not rows:
                continue
            cols = list(rows[0].keys())
            bool_cols = set(BOOLEAN_COLS.get(table, []))
            placeholders = ', '.join([f':{c}' for c in cols])
            col_names = ', '.join(f'"{c}"' for c in cols)
            stmt = text(f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders})')
            transaction = conn.begin()
            try:
                for row in rows:
                    clean = {}
                    for k, v in row.items():
                        if k in bool_cols:
                            clean[k] = bool(v)
                        elif isinstance(v, str):
                            try:
                                clean[k] = datetime.fromisoformat(v)
                            except (ValueError, TypeError):
                                clean[k] = v
                        else:
                            clean[k] = v
                    conn.execute(stmt, clean)
                transaction.commit()
                total += len(rows)
                print(f"  {table}: {len(rows)} rows migrated")
            except Exception as e:
                transaction.rollback()
                err_msg = str(e)[:200].encode('ascii', errors='replace').decode('ascii')
                print(f"  {table}: ERROR - {err_msg}")
    pg_engine.dispose()
    return total

with tmp.app_context():
    print("Creating PostgreSQL schema...")
    create_pg_schema()

    # Stamp Alembic version so Flask doesn't re-run migrations
    LATEST_REVISION = 'eb8f4d2a1c0d'  # latest migration ID (head)
    pg_engine = create_engine(PG_URI)
    with pg_engine.connect() as conn:
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text(f"INSERT INTO alembic_version (version_num) VALUES ('{LATEST_REVISION}')"))
        conn.commit()
    pg_engine.dispose()
    print(f"  Stamped alembic_version with {LATEST_REVISION}")

    print("\nReading SQLite data...")
    data = get_sqlite_data()
    total_tables = len(data)
    total_rows = sum(len(v) for v in data.values())
    print(f"\nFound {total_rows} rows across {total_tables} tables")
    print("\nMigrating to PostgreSQL...")
    migrated = insert_into_pg(data)
    print(f"\nDone! Migrated {migrated} rows to PostgreSQL")
