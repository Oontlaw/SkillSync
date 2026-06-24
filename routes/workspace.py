import secrets
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from database import db, Organisation, OrgMember, WorkerIdentity, Worker, Task, ScoreLog, BehavioralAnomaly

workspace_bp = Blueprint('workspace', __name__, url_prefix='/workspace')


def ws_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('ws_member_id'):
            return redirect(url_for('workspace.workspace_login'))
        return f(*args, **kwargs)
    return decorated


def ws_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('ws_member_id'):
            return redirect(url_for('workspace.workspace_login'))
        if session.get('ws_member_role') not in ('admin', 'hr'):
            return jsonify({'error': 'Admin or HR role required'}), 403
        return f(*args, **kwargs)
    return decorated


@workspace_bp.route('/login', methods=['GET', 'POST'])
def workspace_login():
    if request.method == 'POST':
        slug = request.form.get('slug', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        import sys; print(f'[LOGIN] slug={slug!r} email={email!r} pass_len={len(password)}', file=sys.stderr, flush=True)
        org = Organisation.query.filter_by(slug=slug, is_active=True).first()
        if not org:
            print(f'[LOGIN] org NOT FOUND for slug={slug!r}', file=sys.stderr, flush=True)
            return render_template('workspace_login.html', error='Organisation not found')
        member = OrgMember.query.filter_by(org_id=org.id, email=email, is_active=True).first()
        if member:
            from werkzeug.security import check_password_hash
            print(f'[LOGIN] member={member.id} hash={member.password_hash[:30]} check={check_password_hash(member.password_hash, password)}', file=sys.stderr, flush=True)
        else:
            print(f'[LOGIN] member NOT FOUND for org_id={org.id} email={email!r}', file=sys.stderr, flush=True)
        if not member:
            print(f'[LOGIN] FAIL - no member', file=sys.stderr, flush=True)
            return render_template('workspace_login.html', error='No account found with that email for this organisation')
        if not member.check_password(password):
            print(f'[LOGIN] FAIL - wrong password', file=sys.stderr, flush=True)
            return render_template('workspace_login.html', error='Invalid password')
        print(f'[LOGIN] SUCCESS', file=sys.stderr, flush=True)
        member.last_login = datetime.utcnow()
        db.session.commit()
        session['ws_org_id'] = org.id
        session['ws_member_id'] = member.id
        session['ws_member_role'] = member.role
        session['ws_member_name'] = member.name
        session['ws_org_name'] = org.name
        session['ws_org_slug'] = org.slug
        return redirect(url_for('workspace.workspace_dashboard'))
    return render_template('workspace_login.html')


@workspace_bp.route('/logout', methods=['POST'])
def workspace_logout():
    for key in ['ws_org_id', 'ws_member_id', 'ws_member_role', 'ws_member_name', 'ws_org_name', 'ws_org_slug']:
        session.pop(key, None)
    return redirect(url_for('workspace.workspace_login'))


@workspace_bp.route('/register', methods=['GET', 'POST'])
def workspace_register():
    if request.method == 'POST':
        org_name = request.form.get('org_name', '').strip()
        slug = request.form.get('slug', '').strip().lower()
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        if not all([org_name, slug, name, email, password, confirm]):
            return render_template('workspace_register.html', error='All fields required')
        if password != confirm:
            return render_template('workspace_register.html', error='Passwords do not match')
        if Organisation.query.filter_by(slug=slug).first():
            return render_template('workspace_register.html', error='Organisation slug already taken')
        api_key = secrets.token_urlsafe(32)
        org = Organisation(name=org_name, slug=slug, api_key=api_key)
        db.session.add(org)
        db.session.flush()
        member = OrgMember(org_id=org.id, email=email, name=name, role='admin')
        member.set_password(password)
        db.session.add(member)
        db.session.commit()
        return redirect(url_for('workspace.workspace_login'))
    return render_template('workspace_register.html')


@workspace_bp.route('/')
@ws_login_required
def workspace_dashboard():
    org_id = session['ws_org_id']
    org_name = session['ws_org_name']
    ws_identities = WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()
    linked_ids = [i.worker_id for i in ws_identities if i.worker_id]
    total_workers = len(linked_ids)
    total_tasks = Task.query.filter(Task.worker_id.in_(linked_ids)).count() if linked_ids else 0
    recent_anomalies = BehavioralAnomaly.query.filter_by(source='work_engine').order_by(BehavioralAnomaly.detected_at.desc()).limit(10).all()
    return render_template('workspace_dashboard.html',
        total_workers=total_workers,
        total_tasks=total_tasks,
        recent_anomalies=recent_anomalies,
        org_name=org_name,
    )


@workspace_bp.route('/workers')
@ws_login_required
def workspace_workers():
    org_id = session['ws_org_id']
    org_name = session['ws_org_name']
    identities = WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()
    worker_ids = [i.worker_id for i in identities if i.worker_id]
    workers = Worker.query.filter(Worker.id.in_(worker_ids)).all() if worker_ids else []
    worker_map = {w.id: w for w in workers}
    rows = []
    for ident in identities:
        w = worker_map.get(ident.worker_id)
        rows.append({
            'identity': ident,
            'worker': w,
            'task_count': Task.query.filter_by(worker_id=ident.worker_id).count() if ident.worker_id else 0,
            'score': w.score if w else None,
        })
    return render_template('workspace_workers.html', rows=rows, org_name=org_name)


@workspace_bp.route('/workers/<int:worker_id>')
@ws_login_required
def workspace_worker_detail(worker_id):
    org_id = session['ws_org_id']
    org_name = session['ws_org_name']
    identity = WorkerIdentity.query.filter_by(org_id=org_id, worker_id=worker_id).first()
    worker = db.session.get(Worker, worker_id)
    if not identity or not worker:
        return render_template('workspace_worker_detail.html', error='Worker not found or not linked', org_name=org_name)
    from ml.features import community_prior_for_worker
    tasks = Task.query.filter_by(worker_id=worker_id).order_by(Task.created_at.desc()).limit(50).all()
    scores = ScoreLog.query.filter_by(worker_id=worker_id).order_by(ScoreLog.created_at.desc()).limit(30).all()
    prior = None
    if identity.consent_community_prior and worker.discord_id:
        prior = community_prior_for_worker(worker.id)
    return render_template('workspace_worker_detail.html',
        identity=identity, worker=worker, tasks=tasks, scores=scores, prior=prior, org_name=org_name)


@workspace_bp.route('/identities')
@ws_login_required
def workspace_identities():
    org_id = session['ws_org_id']
    org_name = session['ws_org_name']
    role = session['ws_member_role']
    identities = WorkerIdentity.query.filter_by(org_id=org_id).order_by(WorkerIdentity.linked_at.desc()).all()
    unlinked_workers = Worker.query.filter(
        ~Worker.id.in_(db.session.query(WorkerIdentity.worker_id).filter(
            WorkerIdentity.org_id == org_id, WorkerIdentity.worker_id != None
        ))
    ).all() if role in ('admin', 'hr') else []
    return render_template('workspace_identities.html',
        identities=identities, unlinked_workers=unlinked_workers, role=role, org_name=org_name)


@workspace_bp.route('/identities/link', methods=['POST'])
@ws_admin_required
def workspace_link_identity():
    org_id = session['ws_org_id']
    data = request.get_json(force=True)
    discord_id = data.get('discord_id', '').strip()
    worker_id = data.get('worker_id', type=int)
    org_employee_id = data.get('org_employee_id', '').strip()
    jira_account_id = data.get('jira_account_id', '').strip()
    display_name = data.get('display_name', '').strip()
    email = data.get('email', '').strip()
    if not discord_id and not worker_id:
        return jsonify({'error': 'discord_id or worker_id required'}), 400
    existing = WorkerIdentity.query.filter_by(org_id=org_id, discord_id=discord_id).first() if discord_id else None
    if existing:
        if worker_id:
            existing.worker_id = worker_id
        if org_employee_id:
            existing.org_employee_id = org_employee_id
        if jira_account_id:
            existing.jira_account_id = jira_account_id
        if display_name:
            existing.display_name = display_name
        if email:
            existing.email = email
        existing.linked_at = datetime.utcnow()
        existing.linked_by = session['ws_member_name']
    else:
        identity = WorkerIdentity(
            org_id=org_id, worker_id=worker_id, discord_id=discord_id or None,
            org_employee_id=org_employee_id or None, jira_account_id=jira_account_id or None,
            display_name=display_name or None, email=email or None,
            linked_by=session['ws_member_name'],
        )
        db.session.add(identity)
    db.session.commit()
    return jsonify({'ok': True})


@workspace_bp.route('/settings', methods=['GET', 'POST'])
@ws_admin_required
def workspace_settings():
    org_id = session['ws_org_id']
    org = db.session.get(Organisation, org_id)
    org_name = session['ws_org_name']
    if request.method == 'POST':
        data = request.get_json(force=True)
        org.share_feature_vectors = data.get('share_feature_vectors', org.share_feature_vectors)
        org.share_anomaly_types = data.get('share_anomaly_types', org.share_anomaly_types)
        org.store_task_content = data.get('store_task_content', org.store_task_content)
        db.session.commit()
        return jsonify({'ok': True})
    return render_template('workspace_settings.html', org=org, org_name=org_name)
