from database import db, ScoreLog, Worker
from datetime import datetime
from sqlalchemy import func

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


def _compute_score(worker_id, guild_id=None):
    """Compute total score from ScoreLog, optionally filtered by guild."""
    q = db.session.query(func.sum(ScoreLog.change)).filter(ScoreLog.worker_id == worker_id)
    if guild_id:
        q = q.filter(ScoreLog.guild_id == guild_id)
    return q.scalar() or 0.0


def _compute_all_scores(guild_id=None):
    """Return dict of {worker_id: score} from ScoreLog."""
    q = db.session.query(ScoreLog.worker_id, func.sum(ScoreLog.change).label('total'))
    if guild_id:
        q = q.filter(ScoreLog.guild_id == guild_id)
    return {r.worker_id: float(r.total) for r in q.group_by(ScoreLog.worker_id).all()}


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
        'new_score': _compute_score(worker_id),
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

    difference = corrected_change - original_change

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
        'new_score': _compute_score(worker_id),
        'corrected_by': admin_name
    }


def get_leaderboard(limit=10, guild_id=None):
    """Return top workers by score computed from ScoreLog."""
    scores = _compute_all_scores(guild_id=guild_id)
    sorted_workers = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    result = []
    for wid, total in sorted_workers:
        w = Worker.query.get(wid)
        if w:
            w.score = total  # set computed on the fly
            result.append(w)
    return result


def get_worker_history(worker_id):
    """Return full score log for a worker."""
    return ScoreLog.query.filter_by(worker_id=worker_id).order_by(ScoreLog.created_at.desc()).all()
