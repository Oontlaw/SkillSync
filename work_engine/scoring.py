"""
Work Engine scoring module — completely separate from scoring.py (community engine).
All score logs written here use source='work_engine' for clean separation.
"""

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func

from database import AdminCorrection, ScoreLog, Task, Worker, db

# --- Point values ---
WORK_POINTS = {
    "task_completed_on_time": 15,
    "task_completed_early": 20,
    "task_completed_late": 5,
    "task_missed": -20,
    "task_missed_no_due_date": -10,
    "extra_contribution": 25,
    "blocker_resolved": 15,
    "pr_review_completed": 8,
    "helped_teammate": 10,
    "knowledge_share": 8,
    "repeated_deadline_miss": -25,
    "task_reassigned_away": -5,
    "work_anomaly_detected": -12,
}

# Priority multipliers applied at award time for task-related reasons
PRIORITY_MULTIPLIER = {
    "critical": 1.5,
    "high": 1.2,
    "medium": 1.0,
    "low": 0.8,
}

_TASK_REASONS = {
    "task_completed_on_time",
    "task_completed_early",
    "task_completed_late",
    "task_missed",
    "task_missed_no_due_date",
}


def _compute_worker_score(worker_id: int) -> float:
    """Compute total work score from ScoreLog rows with source='work_engine'."""
    q = db.session.query(func.sum(ScoreLog.change)).filter(
        ScoreLog.worker_id == worker_id, ScoreLog.source == "work_engine"
    )
    return q.scalar() or 0.0


def award_work_points(
    worker_id: int,
    reason_key: str,
    source: str = "work_engine",
    task: Task = None,
    custom_points: float = None,
    note: str = None,
    org_id: int = None,
) -> dict:
    """
    Award or deduct work-specific points. Writes to ScoreLog with source='work_engine'.

    Applies priority multiplier if a Task object is provided and reason_key
    is task-related (completion or miss). Automatically fires
    'repeated_deadline_miss' penalty if applicable.

    Args:
        worker_id: ID of the worker to award.
        reason_key: Key from WORK_POINTS dict.
        source: Source label (default 'work_engine').
        task: Optional Task object for priority multiplier logic.
        custom_points: Override the default point value.
        note: Custom reason text.
        org_id: Org ID (used for repeated-miss auto-penalty scope).

    Returns:
        Dict with worker name, change, new_score, reason, multiplier.
    """
    worker = db.session.get(Worker, worker_id)
    if not worker:
        return {"error": "Worker not found"}

    points = (
        custom_points if custom_points is not None else WORK_POINTS.get(reason_key, 0)
    )

    # Apply priority multiplier for task-related reasons
    multiplier = 1.0
    if task and reason_key in _TASK_REASONS and task.priority:
        multiplier = PRIORITY_MULTIPLIER.get(task.priority, 1.0)
        points = round(points * multiplier, 1)

    reason_text = note or reason_key.replace("_", " ").title()

    log = ScoreLog(
        worker_id=worker_id,
        change=points,
        reason=reason_text,
        source="work_engine",
        admin_correction=False,
    )
    db.session.add(log)
    db.session.flush()

    # Auto-penalty for repeated misses
    if reason_key == "task_missed":
        _check_repeated_misses(worker_id)

    db.session.commit()

    return {
        "worker": worker.name,
        "change": points,
        "new_score": _compute_worker_score(worker_id),
        "reason": reason_text,
        "multiplier": multiplier,
    }


def _check_repeated_misses(worker_id: int):
    """Fire 'repeated_deadline_miss' if 3+ misses in last 30 days and no recent penalty."""
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    seven_days_ago = datetime.utcnow() - timedelta(days=7)

    # Count misses in last 30 days
    miss_count = (
        db.session.query(func.count(ScoreLog.id))
        .filter(
            ScoreLog.worker_id == worker_id,
            ScoreLog.source == "work_engine",
            ScoreLog.reason.like("%Missed%"),
            ScoreLog.created_at >= thirty_days_ago,
        )
        .scalar()
        or 0
    )

    if miss_count < 3:
        return

    # Check if penalty already fired in last 7 days
    existing = (
        db.session.query(ScoreLog.id)
        .filter(
            ScoreLog.worker_id == worker_id,
            ScoreLog.source == "work_engine",
            ScoreLog.reason == "Repeated Deadline Miss",
            ScoreLog.created_at >= seven_days_ago,
        )
        .first()
    )

    if existing:
        return

    points = WORK_POINTS["repeated_deadline_miss"]
    log = ScoreLog(
        worker_id=worker_id,
        change=points,
        reason="Repeated Deadline Miss",
        source="work_engine",
        admin_correction=False,
    )
    db.session.add(log)
