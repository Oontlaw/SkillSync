import os
import tempfile

import pytest


TEST_DB = os.path.join(tempfile.gettempdir(), 'skillsync_pytest.db')
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('API_KEY', 'test-api-key')
os.environ['DATABASE_URL'] = f'sqlite:///{TEST_DB}'
os.environ['FLASK_ENV'] = 'testing'

from app import app as flask_app
from database import db


@pytest.fixture()
def app(monkeypatch):
    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
    )
    monkeypatch.setattr(
        'routes.dashboard.ml_engine.get_model_status',
        lambda: {
            'anomaly_precision': {},
            'burnout_precision': {},
            'health': {'drift_detected': False, 'drift_reasons': []},
            'growth': {'trained': False, 'guild_count': 0},
            'federated': {'trained': False, 'history': []},
        },
    )
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
    yield flask_app
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def login_discord(client, guilds):
    with client.session_transaction() as session:
        session['user'] = {'id': 'admin-1', 'name': 'Admin'}
        session['accessible_guilds'] = [
            {'id': str(guild_id), 'name': f'Guild {guild_id}'}
            for guild_id in guilds
        ]
        session['_csrf_token'] = 'csrf-test'


def login_workspace(client, member):
    with client.session_transaction() as session:
        session['ws_org_id'] = member.org_id
        session['ws_member_id'] = member.id
        session['ws_member_role'] = member.role
        session['ws_member_name'] = member.name
        session['ws_org_name'] = member.organisation.name
        session['ws_org_slug'] = member.organisation.slug
        session['_csrf_token'] = 'csrf-test'


@pytest.fixture()
def csrf_headers():
    return {'X-CSRF-Token': 'csrf-test'}
