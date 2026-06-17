from flask import Blueprint, render_template
from database import Worker, Task, ScoreLog, AdminCorrection

dashboard_bp = Blueprint('dashboard', __name__)

@dashboard_bp.route('/')
def index():
    workers = Worker.query.order_by(Worker.score.desc()).all()
    total_workers = len(workers)
    total_tasks = Task.query.count()
    total_corrections = AdminCorrection.query.count()
    recent_logs = ScoreLog.query.order_by(ScoreLog.created_at.desc()).limit(10).all()

    return render_template('dashboard.html',
        workers=workers,
        total_workers=total_workers,
        total_tasks=total_tasks,
        total_corrections=total_corrections,
        recent_logs=recent_logs
    )

@dashboard_bp.route('/worker/<int:worker_id>')
def worker_detail(worker_id):
    worker = Worker.query.get_or_404(worker_id)
    logs = ScoreLog.query.filter_by(worker_id=worker_id).order_by(ScoreLog.created_at.desc()).all()
    tasks = Task.query.filter_by(worker_id=worker_id).order_by(Task.assigned_at.desc()).all()
    return render_template('worker.html', worker=worker, logs=logs, tasks=tasks)
