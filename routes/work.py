from functools import wraps
from flask import Blueprint, request, jsonify
from database import db, Task, Worker, ScoreLog
from datetime import datetime
from work_engine.webhook import parse_webhook_payload

work_bp = Blueprint('work', __name__)

API_KEY = None


def _get_api_key():
    global API_KEY
    if API_KEY is None:
        import os
        API_KEY = os.getenv('API_KEY', '')
    return API_KEY


def require_work_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        key = _get_api_key()
        if not key or not auth.startswith('Bearer ') or auth.split(' ', 1)[1] != key:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


def _find_worker(assignee_email, assignee_discord_id):
    if assignee_discord_id:
        w = Worker.query.filter_by(discord_id=assignee_discord_id).first()
        if w:
            return w
    if assignee_email:
        return Worker.query.filter_by(email=assignee_email).first()
    return None


def _upsert_task(external_id, source, worker_id, data):
    existing = Task.query.filter_by(external_id=external_id, source=source).first()
    if existing:
        old_status = existing.status
        if data.get('title'):
            existing.title = data['title']
        if data.get('description'):
            existing.description = data['description']
        if data.get('priority'):
            existing.priority = data['priority']
        if data.get('external_url'):
            existing.external_url = data['external_url']
        if data.get('due_at'):
            existing.due_at = datetime.fromisoformat(data['due_at']) if isinstance(data['due_at'], str) else data['due_at']
        existing.status = data.get('status', old_status)
        task = existing
    else:
        task = Task(
            worker_id=worker_id,
            title=data.get('title', f'Task {external_id}'),
            description=data.get('description', ''),
            status=data.get('status', 'pending'),
            source=source,
            external_id=external_id,
            external_url=data.get('external_url', ''),
            priority=data.get('priority', 'medium'),
        )
        if data.get('due_at'):
            task.due_at = datetime.fromisoformat(data['due_at']) if isinstance(data['due_at'], str) else data['due_at']
        db.session.add(task)
        old_status = None

    db.session.flush()

    # Award points if status changed to completed/missed and not already awarded
    points_awarded = 0
    if old_status != task.status and task.status in ('completed', 'missed'):
        from scoring import award_points
        if task.status == 'completed':
            due = task.due_at
            now = datetime.utcnow()
            key = 'task_completed_on_time' if not due or now <= due else 'task_completed_late'
            reason = f'Task completed: {task.title}'
            result = award_points(worker_id, key, source='system', note=reason)
            points_awarded = result.get('change', 0)
        elif task.status == 'missed':
            result = award_points(worker_id, 'task_missed', source='system', note=f'Task missed: {task.title}')
            points_awarded = result.get('change', 0)
        task.points_awarded = points_awarded

    db.session.commit()
    return task, points_awarded


@work_bp.route('/work/sync', methods=['POST'])
@require_work_key
def sync_task():
    """Receive a task update from an external system (Jira, Trello, GitHub, etc.).
    Parses the payload, finds the assigned worker, upserts the task, and awards points.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'No JSON payload'}), 400

    parsed = parse_webhook_payload(data)
    if not parsed or not parsed.get('external_id'):
        return jsonify({'error': 'Could not parse payload: missing task_id'}), 400

    worker = _find_worker(parsed.get('assignee_email', ''), parsed.get('assignee_discord_id', ''))
    if not worker:
        return jsonify({
            'error': 'No matching worker found for assignee',
            'external_id': parsed['external_id'],
        }), 404

    task, points = _upsert_task(
        external_id=parsed['external_id'],
        source=parsed.get('source', 'webhook'),
        worker_id=worker.id,
        data=parsed,
    )

    return jsonify({
        'status': 'synced',
        'task_id': task.id,
        'external_id': parsed['external_id'],
        'worker': worker.name,
        'task_status': task.status,
        'points_awarded': points,
    }), 201 if points == 0 else 200


@work_bp.route('/work/sync/batch', methods=['POST'])
@require_work_key
def sync_tasks_batch():
    """Receive a batch of task updates. Payload is a JSON array of task objects."""
    data = request.get_json(silent=True)
    if not isinstance(data, list):
        return jsonify({'error': 'Payload must be a JSON array'}), 400
    results = []
    for item in data:
        try:
            parsed = parse_webhook_payload(item)
            if not parsed or not parsed.get('external_id'):
                results.append({'error': 'Missing task_id', 'index': len(results)})
                continue
            worker = _find_worker(parsed.get('assignee_email', ''), parsed.get('assignee_discord_id', ''))
            if not worker:
                results.append({'error': 'No worker found', 'external_id': parsed['external_id']})
                continue
            task, points = _upsert_task(
                external_id=parsed['external_id'],
                source=parsed.get('source', 'webhook'),
                worker_id=worker.id,
                data=parsed,
            )
            results.append({
                'status': 'synced', 'external_id': parsed['external_id'],
                'task_id': task.id, 'points_awarded': points,
            })
        except Exception as e:
            results.append({'error': str(e)})
    return jsonify({'synced': len([r for r in results if r.get('status') == 'synced']), 'results': results})


@work_bp.route('/work/tasks', methods=['GET'])
@require_work_key
def list_external_tasks():
    """List all tasks created via Work Engine, with optional source filter."""
    source = request.args.get('source')
    status = request.args.get('status')
    q = Task.query.filter(Task.source != None)
    if source:
        q = q.filter(Task.source == source)
    if status:
        q = q.filter(Task.status == status)
    tasks = q.order_by(Task.assigned_at.desc()).limit(100).all()
    return jsonify([{
        'id': t.id, 'title': t.title, 'status': t.status,
        'source': t.source, 'external_id': t.external_id,
        'priority': t.priority, 'points_awarded': t.points_awarded,
        'worker_name': t.worker.name if t.worker else 'Unknown',
        'assigned_at': t.assigned_at.isoformat() if t.assigned_at else None,
    } for t in tasks])
