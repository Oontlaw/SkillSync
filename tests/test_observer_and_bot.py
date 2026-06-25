import asyncio
from types import SimpleNamespace

from database import BehavioralAnomaly, GuildInfo, PredictionLog, db
from bot_core import state as bot_state
from bot_core.scanner import scan_guild


def api_headers():
    return {'Authorization': 'Bearer test-api-key'}


def test_activity_rejects_invalid_batch_shape(app, client):
    response = client.post(
        '/api/observer/activity',
        json={'batch': True, 'updates': []},
        headers=api_headers(),
    )
    assert response.status_code == 400


def test_anomaly_scan_is_idempotent_and_guild_scoped(app, client, monkeypatch):
    with app.app_context():
        db.session.add(GuildInfo(guild_id='1', name='Guild 1'))
        db.session.commit()

    def fake_scan_all(guild_id=None):
        prediction = PredictionLog(
            model_name='anomaly',
            metadata_json='{"discord_id":"100"}',
        )
        db.session.add(prediction)
        db.session.commit()
        return [{
            'discord_id': '100',
            'anomaly_score': -0.5,
            'severity': 35,
            'prediction_log_id': prediction.id,
        }]

    monkeypatch.setattr('routes.observer.ml_anomaly.scan_all', fake_scan_all)
    payload = {'guild_id': '1'}
    first = client.post('/api/observer/ml/anomalies/scan', json=payload, headers=api_headers())
    second = client.post('/api/observer/ml/anomalies/scan', json=payload, headers=api_headers())
    assert first.status_code == 200
    assert second.status_code == 200
    with app.app_context():
        rows = BehavioralAnomaly.query.all()
        assert len(rows) == 1
        assert rows[0].guild_id == '1'
        prediction = PredictionLog.query.order_by(PredictionLog.id.desc()).first()
        assert f'"entity_id": {rows[0].id}' in prediction.metadata_json


def test_growth_and_health_routes(app, client, monkeypatch):
    monkeypatch.setattr('routes.observer.ml_growth.predict_next_7d', lambda guild_id: {'net_growth': 3})
    monkeypatch.setattr('routes.observer.ml_engine.get_model_health', lambda: {'drift_detected': False})
    forecast = client.get('/api/observer/ml/growth/forecast/1', headers=api_headers())
    health = client.get('/api/observer/ml/health', headers=api_headers())
    assert forecast.status_code == 200
    assert forecast.get_json()['guild_id'] == '1'
    assert health.get_json() == {'drift_detected': False}


def test_scan_resets_online_set_before_seeding(monkeypatch):
    bot_state.online_members['1'] = {999}

    class FakeRole:
        position = 1
        name = 'member'
        color = None
        members = []
        permissions = SimpleNamespace(
            administrator=False,
            ban_members=False,
            kick_members=False,
            manage_messages=False,
            manage_guild=False,
            manage_roles=False,
        )

        def is_default(self):
            return False

    class FakeMember:
        id = 100
        name = 'online'
        display_name = 'online'
        joined_at = None
        bot = False
        roles = []
        top_role = None
        status = SimpleNamespace()
        activities = []

    fake_guild = SimpleNamespace(
        id=1,
        name='Guild',
        owner_id=None,
        owner=None,
        roles=[FakeRole()],
        members=[],
        channels=[],
        member_count=0,
        fetch_automod_rules=lambda: None,
    )

    async def fake_rules():
        return []

    async def fake_post(endpoint, payload):
        return {'ok': True}

    fake_guild.fetch_automod_rules = fake_rules
    monkeypatch.setattr('bot_core.scanner.api_post', fake_post)
    asyncio.run(scan_guild(fake_guild))
    assert bot_state.online_members['1'] == set()
