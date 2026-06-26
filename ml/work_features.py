"""
Work Engine feature engineering — builds feature vectors from Task and ScoreLog data.
Completely separate from ml/features.py (which operates on Discord data).

Each feature vector has 10 dimensions normalised 0-1 (or bounded).
"""

from datetime import datetime, timedelta

import numpy as np

from database import AdminCorrection, ScoreLog, Task, WorkerIdentity, db

PRIORITY_WEIGHTS = {
    "critical": 1.0,
    "high": 0.75,
    "medium": 0.5,
    "low": 0.25,
}


def work_feature_vector_for_worker(
    worker_id: int, org_id: int, days: int = 30
) -> np.ndarray:
    """
    Build a 10-dimensional feature vector from Task and ScoreLog data
    for a single worker.

    Features:
        [0] completion_rate          — completed / (completed + missed)
        [1] on_time_rate             — completed_on_time / completed
        [2] avg_priority_score       — mean priority weight (critical=1 … low=0.25)
        [3] task_velocity            — tasks completed per week, capped at 10/wk
        [4] streak_days              — consecutive days with >=1 completion (capped 30)
        [5] miss_acceleration        — rate of increase in misses (last 14d vs prior 14d)
        [6] late_ratio               — completed_late / completed_total
        [7] score_trend_slope        — linear slope of ScoreLog changes, tanh-norm
        [8] extra_contribution_rate  — extra_contribution tasks / completed
        [9] correction_count_norm    — AdminCorrections in last 30d (capped at 1.0)
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(days=days)

    tasks = Task.query.filter(
        Task.worker_id == worker_id, Task.assigned_at >= cutoff
    ).all()

    if not tasks:
        return np.zeros(10)

    total = len(tasks)
    completed = [t for t in tasks if t.status == "completed"]
    missed = [t for t in tasks if t.status == "missed"]
    completed_count = len(completed)

    # [0] completion_rate
    denominator = completed_count + len(missed)
    completion_rate = completed_count / denominator if denominator > 0 else 0.0

    # [1] on_time_rate
    if completed_count > 0:
        on_time = sum(
            1
            for t in completed
            if not t.due_at or (t.completed_at and t.completed_at <= t.due_at)
        )
        on_time_rate = on_time / completed_count
    else:
        on_time_rate = 0.0

    # [2] avg_priority_score
    if tasks:
        priorities = [PRIORITY_WEIGHTS.get(t.priority, 0.5) for t in tasks]
        avg_priority_score = float(np.mean(priorities))
    else:
        avg_priority_score = 0.5

    # [3] task_velocity
    weeks = max(days / 7, 1)
    task_velocity = completed_count / weeks
    task_velocity_norm = min(task_velocity / 10, 1.0)

    # [4] streak_days
    streak_days = _compute_streak(worker_id, now)
    streak_norm = min(streak_days / 30, 1.0)

    # [5] miss_acceleration
    mid = now - timedelta(days=14)
    recent_misses = sum(1 for t in missed if t.assigned_at >= mid)
    prior_misses = sum(1 for t in missed if t.assigned_at < mid)
    if prior_misses > 0:
        miss_acceleration = min((recent_misses - prior_misses) / prior_misses, 1.0)
    elif recent_misses > 0:
        miss_acceleration = min(recent_misses / 3, 1.0)
    else:
        miss_acceleration = 0.0

    # [6] late_ratio
    if completed_count > 0:
        late_count = sum(
            1
            for t in completed
            if t.completed_at and t.due_at and t.completed_at > t.due_at
        )
        late_ratio = late_count / completed_count
    else:
        late_ratio = 0.0

    # [7] score_trend_slope
    score_logs = (
        ScoreLog.query.filter(
            ScoreLog.worker_id == worker_id,
            ScoreLog.source == "work_engine",
            ScoreLog.created_at >= cutoff,
        )
        .order_by(ScoreLog.created_at)
        .all()
    )
    score_trend_slope = _compute_slope(score_logs)

    # [8] extra_contribution_rate
    if completed_count > 0:
        extra = sum(1 for t in completed if t.extra_contribution)
        extra_contribution_rate = extra / completed_count
    else:
        extra_contribution_rate = 0.0

    # [9] correction_count_norm
    corrections = AdminCorrection.query.filter(
        AdminCorrection.worker_id == worker_id,
        AdminCorrection.created_at >= cutoff,
    ).count()
    correction_count_norm = min(corrections, 1.0)

    return np.array(
        [
            completion_rate,
            on_time_rate,
            avg_priority_score,
            task_velocity_norm,
            streak_norm,
            miss_acceleration,
            late_ratio,
            score_trend_slope,
            extra_contribution_rate,
            correction_count_norm,
        ]
    )


def all_work_feature_vectors(org_id: int, days: int = 30, min_tasks: int = 3):
    """
    Build feature matrix for all workers in an org.

    Returns:
        X: np.ndarray of shape (n_workers, 10)
        worker_ids: list[int] of worker IDs corresponding to each row
    """
    identities = WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()
    worker_ids = [i.worker_id for i in identities if i.worker_id]

    X_list = []
    valid_ids = []
    for wid in worker_ids:
        fv = work_feature_vector_for_worker(wid, org_id, days=days)
        # Only include workers with at least min_tasks of data
        if np.any(fv):
            # Check total task count
            count = Task.query.filter(
                Task.worker_id == wid,
                Task.assigned_at >= datetime.utcnow() - timedelta(days=days),
            ).count()
            if count >= min_tasks:
                X_list.append(fv)
                valid_ids.append(wid)

    if not X_list:
        return np.empty((0, 10)), []

    return np.array(X_list), valid_ids


def _compute_streak(worker_id: int, now: datetime) -> int:
    """Consecutive days (going backward) with at least one completed task."""
    streak = 0
    for i in range(30):
        day = now - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        count = Task.query.filter(
            Task.worker_id == worker_id,
            Task.status == "completed",
            Task.completed_at >= day_start,
            Task.completed_at < day_end,
        ).count()
        if count > 0:
            streak += 1
        else:
            break
    return streak


def _compute_slope(score_logs) -> float:
    """Linear slope of score changes over time, tanh-normalised to [-1, 1]."""
    if len(score_logs) < 2:
        return 0.0
    values = [sl.change for sl in score_logs]
    x = np.arange(len(values))
    if np.std(x) == 0 or np.std(values) == 0:
        return 0.0
    slope = np.polyfit(x, values, 1)[0]
    return float(np.tanh(slope))
