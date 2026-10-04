from database import (
    BehavioralAnomaly,
    GuildInfo,
    GuildMember,
    MessageRecord,
    MessageRef,
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
    # NOTE: '/v2/' (dashboard_v2 blueprint) was deliberately removed — do not
    # re-add it to satisfy stale expectations.
    assert '/workspace/login' in rules
    assert '/api/observer/ml/health' in rules


# ── P0 regression tests (Phase 1 hardening) ──


def test_cross_guild_task_and_history_are_rejected(app, client, csrf_headers):
    """Legacy /api mutations and reads must 404 on another guild's worker."""
    from datetime import datetime, timedelta

    with app.app_context():
        add_guild_worker('1', '100')
        other = add_guild_worker('2', '200', 'Other Worker')
        due = datetime.utcnow() + timedelta(days=1)
        task = Task(worker_id=other.id, title='Not yours', due_at=due)
        db.session.add(task)
        db.session.commit()
        task_id, other_id = task.id, other.id
    login_discord(client, ['1'])

    assert client.post(
        f'/api/tasks/{task_id}/complete', json={'guild_id': '1'}, headers=csrf_headers
    ).status_code == 404
    assert client.post(
        f'/api/tasks/{task_id}/miss', headers=csrf_headers
    ).status_code == 404
    assert client.post(
        f'/api/tasks/{task_id}/anomaly', json={'reason': 'x'}, headers=csrf_headers
    ).status_code == 404
    assert client.get(f'/api/workers/{other_id}/history').status_code == 404
    # and a scoped worker still works end to end
    with app.app_context():
        mine = db.session.get(Worker, 1)
        task2 = Task(worker_id=mine.id, title='Yours', due_at=due)
        db.session.add(task2)
        db.session.commit()
        task2_id = task2.id
    resp = client.post(
        f'/api/tasks/{task2_id}/complete', json={'guild_id': '1'}, headers=csrf_headers
    )
    assert resp.status_code == 200


def test_admin_correction_scoped_and_admin_name_from_session(app, client, csrf_headers):
    """/api/admin/correct: another guild's case is invisible, and the recorded
    admin name comes from the session, never the client payload."""
    with app.app_context():
        mine = add_guild_worker('1', '100', 'Mine')
        other = add_guild_worker('2', '200', 'Other Worker')
        log_mine = ScoreLog(worker_id=mine.id, change=5, reason='r', source='system')
        log_other = ScoreLog(worker_id=other.id, change=5, reason='r', source='system')
        db.session.add_all([log_mine, log_other])
        db.session.commit()
        mine_id, other_id = log_mine.id, log_other.id
    login_discord(client, ['1'])

    assert client.post(
        '/api/admin/correct', json={'case_id': other_id, 'new_change': 1},
        headers=csrf_headers,
    ).status_code == 404

    resp = client.post(
        '/api/admin/correct', json={'case_id': mine_id, 'new_change': 1},
        headers=csrf_headers,
    )
    assert resp.status_code == 200
    with app.app_context():
        from database import AdminCorrection

        ac = AdminCorrection.query.first()
        assert ac is not None and ac.corrected_by == 'Admin'


def test_workspace_task_update_awards_points(app, client, csrf_headers):
    """C2 regression: task-complete via workspace must 302 and write a
    ScoreLog row (previously crashed passing pts into reason_key)."""
    with app.app_context():
        worker = Worker(name='W', email='w@example.com', discord_id='100')
        db.session.add(worker)
        db.session.flush()
        org = Organisation(name='O', slug='o', api_key='k')
        db.session.add(org)
        db.session.flush()
        member = OrgMember(org_id=org.id, email='a@o.test', name='Admin A', role='admin')
        member.set_password('pw')
        db.session.add(member)
        db.session.add(WorkerIdentity(org_id=org.id, worker_id=worker.id, discord_id='100'))
        task = Task(worker_id=worker.id, title='Ship')
        db.session.add(task)
        db.session.commit()
        task_id, member_id = task.id, member.id
    with app.app_context():
        from database import OrgMember as OM

        login_workspace(client, db.session.get(OM, member_id))
    resp = client.post(
        f'/workspace/tasks/{task_id}/update',
        data={'status': 'completed'},
        headers=csrf_headers,
    )
    assert resp.status_code == 302
    with app.app_context():
        from database import ScoreLog as SL

        log = SL.query.filter_by(worker_id=1).first()
        assert log is not None and log.change == 10.0


def test_profiling_tables_ttl_purge(app, client):
    """Per-table retention knobs: pings (default 90d), voice and join events
    (default 180d) purge on their own horizons; recent rows survive."""
    from datetime import datetime, timedelta

    from database import PingJoinEvent, PingEvent, VoiceActivity

    now = datetime.utcnow()
    mid = now - timedelta(days=120)   # past ping horizon, inside voice/join
    ancient = now - timedelta(days=400)
    with app.app_context():
        for ts in (mid, now):
            db.session.add(PingEvent(
                guild_id='1', pinger_id='a', pingee_id='b', channel_id='c',
                message_id=f'm-{ts}', ping_type='mention', created_at=ts,
            ))
        db.session.add(VoiceActivity(
            guild_id='1', discord_id='a', created_at=ancient, joined_at=ancient, left_at=ancient,
        ))
        db.session.add(VoiceActivity(guild_id='1', discord_id='a', created_at=mid))
        db.session.add(PingJoinEvent(guild_id='1', moderator_id='a', created_at=ancient))
        db.session.add(PingJoinEvent(guild_id='1', moderator_id='a', created_at=mid))
        db.session.commit()

    resp = client.post(
        '/api/observer/cleanup', json={'retention_days': 90},
        headers={'Authorization': 'Bearer test-api-key'},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['deleted_pings'] == 1          # 120d > ping horizon (90)
    assert body['deleted_voice'] == 1          # 400d > voice horizon (180)
    assert body['deleted_join_events'] == 1    # 400d > join horizon (180)
    with app.app_context():
        assert PingEvent.query.count() == 1
        assert VoiceActivity.query.count() == 1   # the 120d row survives
        assert PingJoinEvent.query.count() == 1   # the 120d row survives


def test_consent_optout_gates_computation(app, client):
    """Opted-out members vanish from pair_scores, user metrics, and
    voice-only bonds; NULL consent counts as opted IN."""
    from datetime import datetime, timedelta

    from database import GuildMember, PairScore, PingEvent, UserBehaviorMetric, db
    from interactions import recompute_pair_scores, recompute_user_metrics

    now = datetime.utcnow()
    with app.app_context():
        # A: opted out, B: their partner, C/D: control pair (no consent row)
        db.session.add(GuildMember(guild_id='1', member_id='A', name='A', consent_optin=False))
        db.session.add(GuildMember(guild_id='1', member_id='B', name='B'))
        for i in range(4):
            db.session.add(PingEvent(
                guild_id='1', pinger_id='A', pingee_id='B', channel_id='c',
                message_id=f'ab{i}', ping_type='mention', created_at=now - timedelta(days=i),
            ))
            db.session.add(PingEvent(
                guild_id='1', pinger_id='C', pingee_id='D', channel_id='c',
                message_id=f'cd{i}', ping_type='mention', created_at=now - timedelta(days=i),
            ))
        for i in range(4):  # C needs message activity to earn a user-metric row
            db.session.add(MessageRef(
                guild_id='1', channel_id='c', message_id=f'cr{i}',
                author_id='C', created_at=now - timedelta(days=i),
            ))
        db.session.add(UserBehaviorMetric(
            guild_id='1', discord_id='A', name='A', computed_at=now,
        ))
        db.session.commit()
        recompute_pair_scores()
        recompute_user_metrics()
        assert PairScore.query.filter_by(guild_id='1').count() == 2  # only C<->D
        assert not any(
            {r.pinger_id, r.pingee_id} & {'A', 'B'}
            for r in PairScore.query.filter_by(guild_id='1').all()
        )
        assert UserBehaviorMetric.query.filter_by(discord_id='A').count() == 0
        assert UserBehaviorMetric.query.filter_by(discord_id='C').count() == 1


def test_consent_optout_gates_ingest(app, client):
    """Opted-out members produce ZERO new profiling rows at ingest."""
    from database import GuildMember, MessageRef, PingEvent, db

    with app.app_context():
        db.session.add(GuildMember(guild_id='1', member_id='X', name='X', consent_optin=False))
        db.session.commit()

    auth = {'Authorization': 'Bearer test-api-key'}
    resp = client.post('/api/observer/ping-events', json=[
        {'guild_id': '1', 'pinger_id': 'X', 'pingee_id': 'Y',
         'channel_id': 'c', 'channel_name': 'c', 'message_id': 'p1'},
        {'guild_id': '1', 'pinger_id': 'Y', 'pingee_id': 'Z',
         'channel_id': 'c', 'channel_name': 'c', 'message_id': 'p2'},
    ], headers=auth)
    assert resp.status_code == 201
    assert resp.get_json()['inserted'] == 1
    assert resp.get_json()['consent_skipped'] == 1

    resp = client.post('/api/observer/message-refs', json=[
        {'guild_id': '1', 'channel_id': 'c', 'message_id': 'r1', 'author_id': 'X'},
    ], headers=auth)
    assert resp.get_json()['inserted'] == 0

    with app.app_context():
        assert PingEvent.query.count() == 1
        assert PingEvent.query.first().pinger_id == 'Y'
        assert MessageRef.query.count() == 0


def test_consent_endpoint_upserts(app, client):
    """/observer/consent creates or updates the consent row."""
    from database import GuildMember, db

    auth = {'Authorization': 'Bearer test-api-key'}
    resp = client.post('/api/observer/consent', json={
        'guild_id': '1', 'discord_id': 'Z', 'name': 'Zed', 'optin': False,
    }, headers=auth)
    assert resp.status_code == 200
    with app.app_context():
        row = GuildMember.query.filter_by(guild_id='1', member_id='Z').first()
        assert row is not None and row.consent_optin is False
        assert row.consent_source == 'bot-command'

    resp = client.post('/api/observer/consent', json={
        'guild_id': '1', 'discord_id': 'Z', 'optin': True,
    }, headers=auth)
    assert resp.status_code == 200
    with app.app_context():
        row = GuildMember.query.filter_by(guild_id='1', member_id='Z').first()
        assert row.consent_optin is True


def test_oauth_state_rejects_missing_wrong_and_foreign(app, client):
    """Callback requires state bound to THIS session: missing, wrong, and
    foreign-session states all fail closed."""
    assert client.get('/auth/callback').status_code == 403
    assert client.get('/auth/callback?state=wrong&code=x').status_code == 403
    # a state minted for ANOTHER session must not complete this one
    from routes.auth import _remember_state

    _remember_state('foreign-state')
    assert client.get('/auth/callback?state=foreign-state&code=x').status_code == 403


def test_oauth_state_is_single_use(app):
    """A consumed state can never be replayed, even with the same session."""
    from flask import session as flask_session

    from routes.auth import _consume_state, _remember_state

    with app.test_request_context('/'):
        flask_session['oauth_state'] = 'good-state'
        _remember_state('good-state')
        assert _consume_state('good-state') is True
        # replay: same session value, but the registry entry is gone
        flask_session['oauth_state'] = 'good-state'
        assert _consume_state('good-state') is False
