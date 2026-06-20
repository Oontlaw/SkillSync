from functools import wraps
from flask import Blueprint, request, jsonify, session
from database import db, Worker, Task, ScoreLog
from scoring import award_points, admin_correction, get_leaderboard, get_worker_history
from datetime import datetime

api_bp = Blueprint('api', __name__)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return jsonify({'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated


# --- Workers ---

@api_bp.route('/workers', methods=['GET'])
@login_required
def get_workers():
    workers = Worker.query.all()
    return jsonify([{
        'id': w.id, 'name': w.name, 'email': w.email,
        'score': w.score, 'role': w.role, 'discord_id': w.discord_id
    } for w in workers])


@api_bp.route('/workers', methods=['POST'])
@login_required
def add_worker():
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400
    worker = Worker(
        name=data['name'],
        email=data['email'],
        discord_id=data.get('discord_id'),
        role=data.get('role', 'worker')
    )
    db.session.add(worker)
    db.session.commit()
    return jsonify({'message': 'Worker added', 'id': worker.id}), 201


# --- Tasks ---

@api_bp.route('/tasks', methods=['POST'])
@login_required
def assign_task():
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400
    task = Task(
        worker_id=data['worker_id'],
        title=data['title'],
        description=data.get('description', ''),
        due_at=datetime.fromisoformat(data['due_at']) if data.get('due_at') else None
    )
    db.session.add(task)
    db.session.commit()
    return jsonify({'message': 'Task assigned', 'task_id': task.id}), 201


@api_bp.route('/tasks/<int:task_id>/complete', methods=['POST'])
@login_required
def complete_task(task_id):
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400
    task = Task.query.get_or_404(task_id)
    task.completed_at = datetime.utcnow()
    task.extra_contribution = data.get('extra_contribution', False)
    task.extra_notes = data.get('extra_notes', '')

    # Determine if on time or late
    if task.due_at and task.completed_at > task.due_at:
        task.status = 'completed'
        result = award_points(task.worker_id, 'task_completed_late', note=f'Task completed late: {task.title}')
    else:
        task.status = 'completed'
        result = award_points(task.worker_id, 'task_completed_on_time', note=f'Task completed on time: {task.title}')

    # Bonus for extra contribution
    if task.extra_contribution:
        bonus = award_points(task.worker_id, 'extra_contribution', note=f'Extra contribution: {task.extra_notes}')
        result['bonus'] = bonus

    task.points_awarded = result['change']
    db.session.commit()
    return jsonify(result)


@api_bp.route('/tasks/<int:task_id>/miss', methods=['POST'])
@login_required
def miss_task(task_id):
    task = Task.query.get_or_404(task_id)
    task.status = 'missed'
    result = award_points(task.worker_id, 'task_missed', note=f'Task missed: {task.title}')
    task.points_awarded = result['change']
    db.session.commit()
    return jsonify(result)


@api_bp.route('/tasks/<int:task_id>/anomaly', methods=['POST'])
@login_required
def flag_anomaly(task_id):
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400
    task = Task.query.get_or_404(task_id)
    task.status = 'anomaly'
    result = award_points(task.worker_id, 'anomaly_detected', note=data.get('reason', 'Anomaly detected'))
    task.points_awarded = result['change']
    db.session.commit()
    return jsonify(result)


# --- Admin Correction ---

@api_bp.route('/admin/correct', methods=['POST'])
@login_required
def correct_score():
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400
    result = admin_correction(
        worker_id=data['worker_id'],
        original_change=data['original_change'],
        corrected_change=data['corrected_change'],
        reason=data['reason'],
        admin_name=data['admin_name']
    )
    return jsonify(result)


# --- Leaderboard & History ---

@api_bp.route('/leaderboard', methods=['GET'])
@login_required
def leaderboard():
    workers = get_leaderboard()
    return jsonify([{'name': w.name, 'score': w.score, 'id': w.id} for w in workers])


@api_bp.route('/workers/<int:worker_id>/history', methods=['GET'])
@login_required
def history(worker_id):
    logs = get_worker_history(worker_id)
    return jsonify([{
        'change': l.change, 'reason': l.reason,
        'source': l.source, 'date': l.created_at.isoformat()
    } for l in logs])
