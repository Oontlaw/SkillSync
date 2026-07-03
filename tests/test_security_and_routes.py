from database import (
    BehavioralAnomaly,
    GuildInfo,
    GuildMember,
    MessageRecord,
    Organisation,
    OrgMember,
    ScoreLog,
    Task,
    Worker,
    WorkerIdentity,
    db,
)
from tests.conftest import login_discord, login_workspace


def add_guild_worker(guild_id='1', discord_id='100', name='Worker'):
    guild = GuildInfo(guild_id=guild_id, name=f'Guild {guild_id}')
    member = GuildMember(
        guild_id=guild_id,
        member_id=discord_id,
        name=name,
        is_staff=True,
        is_online=True,
    )
    worker = Worker(
        name=name,
        email=f'{discord_id}@example.com',
        discord_id=discord_id,
    )
    db.session.add_all([guild, member, worker])
    db.session.commit()
    return worker


def test_zero_access_never_falls_back_to_global_data(app, client):
    with app.app_context():
        worker = add_guild_worker()
        db.session.add(MessageRecord(
            discord_id=worker.discord_id,
            name='Hidden Worker',
            guild_id='1',
            channel_name='public',
            message_length=10,
        ))
        db.session.commit()

    login_discord(client, [])
    assert client.get('/api/workers').get_json() == []
    response = client.get('/')
    assert response.status_code == 200
    assert b'Hidden Worker' not in response.data


def test_dashboard_anomalies_show_member_names(app, client):
    with app.app_context():
        worker = add_guild_worker('1', '100', 'Guild Alice')
        db.session.add(BehavioralAnomaly(
            discord_id=worker.discord_id,
            guild_id='1',
            anomaly_type='volume_spike',
            severity=90,
        ))
        db.session.commit()

    login_discord(client, ['1'])
    response = client.get('/')

    assert response.status_code == 200
    assert b'Guild Alice' in response.data


def test_cross_guild_worker_detail_is_rejected(app, client):
    with app.app_context():
        add_guild_worker('1', '100')
        other = add_guild_worker('2', '200', 'Other Worker')
        other_id = other.id
    login_discord(client, ['1'])
    response = client.get(f'/worker/{other_id}')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/')


def test_mutations_require_guild_admin_access(app, client, csrf_headers):
    with app.app_context():
        add_guild_worker()
    login_discord(client, [])
    response = client.post(
        '/api/tasks',
        json={'worker_id': 1, 'title': 'Blocked'},
        headers=csrf_headers,
    )
    assert response.status_code == 403


def test_task_completion_is_idempotent_and_atomic(app, client, csrf_headers):
    with app.app_context():
        worker = add_guild_worker()
        task = Task(worker_id=worker.id, title='Ship it')
        db.session.add(task)
        db.session.commit()
        task_id = task.id
    login_discord(client, ['1'])

    payload = {'guild_id': '1'}
    first = client.post(
        f'/api/tasks/{task_id}/complete',
        json=payload,
        headers=csrf_headers,
    )
    second = client.post(
        f'/api/tasks/{task_id}/complete',
        json=payload,
        headers=csrf_headers,
    )
    assert first.status_code == 200
    assert second.get_json()['status'] == 'unchanged'
    with app.app_context():
        assert ScoreLog.query.filter_by(worker_id=1).count() == 1


def test_workspace_records_are_scoped_to_current_org(app, client):
    with app.app_context():
        worker_a = Worker(name='A', email='a@example.com', discord_id='101')
        worker_b = Worker(name='B', email='b@example.com', discord_id='202')
        org_a = Organisation(name='A Org', slug='a-org', api_key='a-key')
        org_b = Organisation(name='B Org', slug='b-org', api_key='b-key')
        db.session.add_all([worker_a, worker_b, org_a, org_b])
        db.session.flush()
        member = OrgMember(org_id=org_a.id, email='admin@a.test', name='Admin A', role='admin')
        member.set_password('password')
        db.session.add_all([
            member,
            WorkerIdentity(org_id=org_a.id, worker_id=worker_a.id, discord_id='101'),
            WorkerIdentity(org_id=org_b.id, worker_id=worker_b.id, discord_id='202'),
            BehavioralAnomaly(
                discord_id='101',
                anomaly_type='work',
                source='work_engine',
                details='Visible anomaly',
            ),
            BehavioralAnomaly(
                discord_id='202',
                anomaly_type='work',
                source='work_engine',
                details='Hidden anomaly',
            ),
        ])
        db.session.commit()
        member_id = member.id
    with app.app_context():
        member = db.session.get(OrgMember, member_id)
        login_workspace(client, member)

    response = client.get('/workspace/')
    assert response.status_code == 200
    assert b'Visible anomaly' in response.data
    assert b'Hidden anomaly' not in response.data


def test_workspace_login_and_registered_routes_render(app, client):
    assert client.get('/workspace/login').status_code == 200
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert '/' in rules
    assert '/v2/' in rules
    assert '/workspace/login' in rules
    assert '/api/observer/ml/health' in rules
