import os
from datetime import datetime
from functools import wraps

import requests
from flask import Blueprint, jsonify, request, session

from database import ScoreLog, Task, Worker, db
from routes.security import (
    accessible_guild_ids,
    accessible_worker_ids,
    login_required,
)
from scoring import award_points, correct_case, get_leaderboard, get_worker_history

api_bp = Blueprint("api", __name__)


def _authority_error():
    """Legacy /api surface is guild-scoped: a session with zero accessible
    guilds has no authority over any guild data. (Cross-guild authority is
    enforced per-resource by the workerscope checks below.)"""
    if not accessible_guild_ids():
        return jsonify({"error": "No accessible guilds"}), 403
    return None


def _worker_in_scope(worker_id):
    """Worker is reachable through one of the session's guilds via their
    linked discord_id (GuildMember membership)."""
    return worker_id in set(accessible_worker_ids())


# --- Workers ---


@api_bp.route("/workers", methods=["GET"])
@login_required
def get_workers():
    worker_ids = accessible_worker_ids()
    workers = Worker.query.filter(Worker.id.in_(worker_ids)).all() if worker_ids else []
    from sqlalchemy import func

    from database import ScoreLog

    scores = dict(
        db.session.query(ScoreLog.worker_id, func.sum(ScoreLog.change))
        .group_by(ScoreLog.worker_id)
        .all()
    )
    return jsonify(
        [
            {
                "id": w.id,
                "name": w.name,
                "email": w.email,
                "score": float(scores.get(w.id, 0)),
                "role": w.role,
                "discord_id": w.discord_id,
            }
            for w in workers
        ]
    )


@api_bp.route("/workers", methods=["POST"])
@login_required
def add_worker():
    data = request.json
    if not data:
        return jsonify({"error": "No JSON body"}), 400
    worker = Worker(
        name=data["name"],
        email=data["email"],
        discord_id=data.get("discord_id"),
        role=data.get("role", "worker"),
    )
    db.session.add(worker)
    db.session.commit()
    return jsonify({"message": "Worker added", "id": worker.id}), 201


# --- Tasks ---


@api_bp.route("/tasks", methods=["POST"])
@login_required
def assign_task():
    denied = _authority_error()
    if denied:
        return denied
    data = request.json
    if not data:
        return jsonify({"error": "No JSON body"}), 400
    try:
        worker_id = int(data["worker_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "worker_id is required"}), 400
    if not _worker_in_scope(worker_id):
        return jsonify({"error": "Worker not found"}), 404
    task = Task(
        worker_id=worker_id,
        title=data["title"],
        description=data.get("description", ""),
        due_at=datetime.fromisoformat(data["due_at"]) if data.get("due_at") else None,
    )
    db.session.add(task)
    db.session.commit()
    return jsonify({"message": "Task assigned", "task_id": task.id}), 201


@api_bp.route("/tasks/<int:task_id>/complete", methods=["POST"])
@login_required
def complete_task(task_id):
    denied = _authority_error()
    if denied:
        return denied
    data = request.json
    if not data:
        return jsonify({"error": "No JSON body"}), 400
    task = Task.query.get_or_404(task_id)
    if not _worker_in_scope(task.worker_id):
        return jsonify({"error": "Task not found"}), 404
    if task.status == "completed":
        # idempotent: re-posting a completed task never re-awards points
        return jsonify({"status": "unchanged", "task_id": task.id})
    task.completed_at = datetime.utcnow()
    task.extra_contribution = data.get("extra_contribution", False)
    task.extra_notes = data.get("extra_notes", "")

    # Determine if on time or late
    if task.due_at and task.completed_at > task.due_at:
        task.status = "completed"
        result = award_points(
            task.worker_id,
            "task_completed_late",
            note=f"Task completed late: {task.title}",
        )
    else:
        task.status = "completed"
        result = award_points(
            task.worker_id,
            "task_completed_on_time",
            note=f"Task completed on time: {task.title}",
        )

    # Bonus for extra contribution
    if task.extra_contribution:
        bonus = award_points(
            task.worker_id,
            "extra_contribution",
            note=f"Extra contribution: {task.extra_notes}",
        )
        result["bonus"] = bonus

    task.points_awarded = result["change"]
    db.session.commit()
    return jsonify(result)


@api_bp.route("/tasks/<int:task_id>/miss", methods=["POST"])
@login_required
def miss_task(task_id):
    denied = _authority_error()
    if denied:
        return denied
    task = Task.query.get_or_404(task_id)
    if not _worker_in_scope(task.worker_id):
        return jsonify({"error": "Task not found"}), 404
    if task.status == "missed":
        return jsonify({"status": "unchanged", "task_id": task.id})
    task.status = "missed"
    result = award_points(
        task.worker_id, "task_missed", note=f"Task missed: {task.title}"
    )
    task.points_awarded = result["change"]
    db.session.commit()
    return jsonify(result)


@api_bp.route("/tasks/<int:task_id>/anomaly", methods=["POST"])
@login_required
def flag_anomaly(task_id):
    denied = _authority_error()
    if denied:
        return denied
    data = request.json
    if not data:
        return jsonify({"error": "No JSON body"}), 400
    task = Task.query.get_or_404(task_id)
    if not _worker_in_scope(task.worker_id):
        return jsonify({"error": "Task not found"}), 404
    task.status = "anomaly"
    result = award_points(
        task.worker_id, "anomaly_detected", note=data.get("reason", "Anomaly detected")
    )
    task.points_awarded = result["change"]
    db.session.commit()
    return jsonify(result)


# --- Admin Correction ---


@api_bp.route("/admin/correct", methods=["POST"])
@login_required
def correct_score():
    denied = _authority_error()
    if denied:
        return denied
    data = request.json
    if not data:
        return jsonify({"error": "No JSON body"}), 400
    if "case_id" not in data or "new_change" not in data:
        return jsonify({"error": "case_id and new_change are required"}), 400
    log = db.session.get(ScoreLog, data["case_id"])
    if not log or not _worker_in_scope(log.worker_id):
        return jsonify({"error": "Case not found"}), 404
    # the acting admin is the session user, never a client-supplied string
    admin_name = (session.get("user") or {}).get("name") or "Unknown"
    result = correct_case(
        case_id=data["case_id"],
        new_change=data["new_change"],
        reason=data.get("reason", ""),
        admin_name=admin_name,
    )

    # Fire-and-forget: signal ML retrain (admin correction → feedback loop)
    api_key = os.getenv("API_KEY", "")
    try:
        requests.post(
            f"http://localhost:5000/api/observer/ml/request-retrain",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"trigger": "admin_correction"},
            timeout=2,
        )
    except Exception:
        pass

    return jsonify(result)


# --- Leaderboard & History ---


@api_bp.route("/leaderboard", methods=["GET"])
@login_required
def leaderboard():
    worker_ids = set(accessible_worker_ids())
    workers = [w for w in get_leaderboard() if w.id in worker_ids]
    return jsonify([{"name": w.name, "score": w.score, "id": w.id} for w in workers])


@api_bp.route("/workers/<int:worker_id>/history", methods=["GET"])
@login_required
def history(worker_id):
    if not _worker_in_scope(worker_id):
        return jsonify({"error": "Worker not found"}), 404
    logs = get_worker_history(worker_id)
    return jsonify(
        [
            {
                "change": l.change,
                "reason": l.reason,
                "source": l.source,
                "date": l.created_at.isoformat(),
            }
            for l in logs
        ]
    )
