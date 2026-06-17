from database import db, ScoreLog, Worker
from datetime import datetime

# --- Point values ---
POINTS = {
    'task_completed_on_time': 10,
    'task_completed_late': 5,
    'task_missed': -15,
    'anomaly_detected': -10,
    'extra_contribution': 20,
    'community_helpful': 5,
    'community_rule_break': -8,
    'community_moderation_action': 3,
}


def award_points(worker_id, reason_key, source='system', custom_points=None, note=None):
    """
    Award or deduct points from a worker.
    reason_key: key from POINTS dict
    custom_points: override the default points value
    """
    worker = Worker.query.get(worker_id)
    if not worker:
        return {'error': 'Worker not found'}

    points = custom_points if custom_points is not None else POINTS.get(reason_key, 0)
    reason_text = note or reason_key.replace('_', ' ').title()

    worker.score += points

    log = ScoreLog(
        worker_id=worker_id,
        change=points,
        reason=reason_text,
        source=source,
        admin_correction=False
    )
    db.session.add(log)
    db.session.commit()

    return {
        'worker': worker.name,
        'change': points,
        'new_score': worker.score,
        'reason': reason_text
    }


def admin_correction(worker_id, original_change, corrected_change, reason, admin_name):
    """
    Admin overrides a score decision.
    Stores correction as training data for future model improvement.
    """
    from database import AdminCorrection

    worker = Worker.query.get(worker_id)
    if not worker:
        return {'error': 'Worker not found'}

    # Reverse original change and apply corrected one
    difference = corrected_change - original_change
    worker.score += difference

    # Log the correction
    log = ScoreLog(
        worker_id=worker_id,
        change=difference,
        reason=f'Admin correction by {admin_name}: {reason}',
        source='admin',
        admin_correction=True
    )

    correction = AdminCorrection(
        worker_id=worker_id,
        original_score_change=original_change,
        corrected_score_change=corrected_change,
        reason=reason,
        corrected_by=admin_name
    )

    db.session.add(log)
    db.session.add(correction)
    db.session.commit()

    return {
        'worker': worker.name,
        'adjustment': difference,
        'new_score': worker.score,
        'corrected_by': admin_name
    }


def get_leaderboard(limit=10):
    """Return top workers by score."""
    return Worker.query.order_by(Worker.score.desc()).limit(limit).all()


def get_worker_history(worker_id):
    """Return full score log for a worker."""
    return ScoreLog.query.filter_by(worker_id=worker_id).order_by(ScoreLog.created_at.desc()).all()
