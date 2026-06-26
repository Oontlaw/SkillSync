from datetime import datetime
from functools import wraps

from flask import Blueprint, jsonify, request, session

from database import Organisation, ScoreLog, Task, Worker, db
from work_engine.webhook import parse_webhook_payload

work_bp = Blueprint("work", __name__)

API_KEY = None


def _get_api_key():
    global API_KEY
    if API_KEY is None:
        import os

        API_KEY = os.getenv("API_KEY", "")
    return API_KEY


def require_work_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        key = _get_api_key()
        if not key or not auth.startswith("Bearer ") or auth.split(" ", 1)[1] != key:
            return jsonify({"error": "Unauthorized"}), 401
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
        if data.get("title"):
            existing.title = data["title"]
        if data.get("description"):
            existing.description = data["description"]
        if data.get("priority"):
            existing.priority = data["priority"]
        if data.get("external_url"):
            existing.external_url = data["external_url"]
        if data.get("due_at"):
            existing.due_at = (
                datetime.fromisoformat(data["due_at"])
                if isinstance(data["due_at"], str)
                else data["due_at"]
            )
        existing.status = data.get("status", old_status)
        task = existing
    else:
        task = Task(
            worker_id=worker_id,
            title=data.get("title", f"Task {external_id}"),
            description=data.get("description", ""),
            status=data.get("status", "pending"),
            source=source,
            external_id=external_id,
            external_url=data.get("external_url", ""),
            priority=data.get("priority", "medium"),
        )
        if data.get("due_at"):
            task.due_at = (
                datetime.fromisoformat(data["due_at"])
                if isinstance(data["due_at"], str)
                else data["due_at"]
            )
        db.session.add(task)
        old_status = None

    db.session.flush()

    # Award work points if status changed to completed/missed and not already awarded
    points_awarded = 0
    if old_status != task.status and task.status in ("completed", "missed"):
        from work_engine.scoring import award_work_points

        if task.status == "completed":
            due = task.due_at
            now = datetime.utcnow()
            if not due:
                key = "task_completed_on_time"
            elif now <= due:
                # Check if more than 1 day early
                if (due - now).total_seconds() > 86400:
                    key = "task_completed_early"
                else:
                    key = "task_completed_on_time"
            else:
                key = "task_completed_late"

            task.completed_at = now
            task.is_early = key == "task_completed_early"
            task.is_late = key == "task_completed_late"
            task.priority_at_completion = task.priority

            reason = f"Task completed: {task.title}"
            result = award_work_points(worker_id, key, task=task, note=reason)
            points_awarded = result.get("change", 0)
        elif task.status == "missed":
            result = award_work_points(
                worker_id,
                "task_missed",
                task=task,
                note=f"Task missed: {task.title}",
            )
            points_awarded = result.get("change", 0)
        task.points_awarded = points_awarded

    db.session.commit()
    return task, points_awarded


@work_bp.route("/work/sync", methods=["POST"])
@require_work_key
def sync_task():
    """Receive a task update from an external system (Jira, Trello, GitHub, etc.).
    Parses the payload, finds the assigned worker, upserts the task, and awards points.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No JSON payload"}), 400

    parsed = parse_webhook_payload(data)
    if not parsed or not parsed.get("external_id"):
        return jsonify({"error": "Could not parse payload: missing task_id"}), 400

    worker = _find_worker(
        parsed.get("assignee_email", ""), parsed.get("assignee_discord_id", "")
    )
    if not worker:
        return jsonify(
            {
                "error": "No matching worker found for assignee",
                "external_id": parsed["external_id"],
            }
        ), 404

    task, points = _upsert_task(
        external_id=parsed["external_id"],
        source=parsed.get("source", "webhook"),
        worker_id=worker.id,
        data=parsed,
    )

    return jsonify(
        {
            "status": "synced",
            "task_id": task.id,
            "external_id": parsed["external_id"],
            "worker": worker.name,
            "task_status": task.status,
            "points_awarded": points,
        }
    ), 201 if points == 0 else 200


@work_bp.route("/work/sync/batch", methods=["POST"])
@require_work_key
def sync_tasks_batch():
    """Receive a batch of task updates. Payload is a JSON array of task objects."""
    data = request.get_json(silent=True)
    if not isinstance(data, list):
        return jsonify({"error": "Payload must be a JSON array"}), 400
    results = []
    for item in data:
        try:
            parsed = parse_webhook_payload(item)
            if not parsed or not parsed.get("external_id"):
                results.append({"error": "Missing task_id", "index": len(results)})
                continue
            worker = _find_worker(
                parsed.get("assignee_email", ""), parsed.get("assignee_discord_id", "")
            )
            if not worker:
                results.append(
                    {"error": "No worker found", "external_id": parsed["external_id"]}
                )
                continue
            task, points = _upsert_task(
                external_id=parsed["external_id"],
                source=parsed.get("source", "webhook"),
                worker_id=worker.id,
                data=parsed,
            )
            results.append(
                {
                    "status": "synced",
                    "external_id": parsed["external_id"],
                    "task_id": task.id,
                    "points_awarded": points,
                }
            )
        except Exception as e:
            results.append({"error": str(e)})
    return jsonify(
        {
            "synced": len([r for r in results if r.get("status") == "synced"]),
            "results": results,
        }
    )


@work_bp.route("/work/tasks", methods=["GET"])
@require_work_key
def list_external_tasks():
    """List all tasks created via Work Engine, with optional source filter."""
    source = request.args.get("source")
    status = request.args.get("status")
    q = Task.query.filter(Task.source != None)
    if source:
        q = q.filter(Task.source == source)
    if status:
        q = q.filter(Task.status == status)
    tasks = q.order_by(Task.assigned_at.desc()).limit(100).all()
    return jsonify(
        [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "source": t.source,
                "external_id": t.external_id,
                "priority": t.priority,
                "points_awarded": t.points_awarded,
                "worker_name": t.worker.name if t.worker else "Unknown",
                "assigned_at": t.assigned_at.isoformat() if t.assigned_at else None,
            }
            for t in tasks
        ]
    )


@work_bp.route("/work/award", methods=["POST"])
@require_work_key
def manual_award():
    """
    HR/admin manually awards work points. Payload:
    { "worker_id": 1, "reason_key": "helped_teammate", "note": "...", "custom_points": null }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No JSON payload"}), 400

    worker_id = data.get("worker_id")
    reason_key = data.get("reason_key")
    if not worker_id or not reason_key:
        return jsonify({"error": "worker_id and reason_key required"}), 400

    from work_engine.scoring import award_work_points

    result = award_work_points(
        worker_id=worker_id,
        reason_key=reason_key,
        source="work_engine",
        custom_points=data.get("custom_points"),
        note=data.get("note"),
    )

    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


@work_bp.route("/work/jira/test", methods=["POST"])
def test_jira_connection():
    """Test Jira credentials for the logged-in org (called from workspace settings UI)."""
    if not session.get("ws_member_id") or session.get("ws_member_role") not in (
        "admin",
        "hr",
    ):
        return jsonify({"error": "Unauthorised"}), 403

    org_id = session.get("ws_org_id")
    if not org_id:
        return jsonify({"error": "No active workspace session"}), 403

    org = db.session.get(Organisation, org_id)
    if not org:
        return jsonify({"error": "Organisation not found"}), 404

    # Accept inline credentials from the form (user may not have saved yet)
    data = request.get_json(silent=True) or {}
    jira_url = data.get("jira_url") or org.jira_url
    jira_email = data.get("jira_email") or org.jira_email
    jira_api_token = data.get("jira_api_token") or org.jira_api_token
    jira_project = data.get("jira_project") or org.jira_project

    if not all([jira_url, jira_email, jira_api_token, jira_project]):
        return jsonify(
            {"ok": False, "error": "Fill in all four Jira fields first"}
        ), 400

    import requests as req

    try:
        url = f"{jira_url.rstrip('/')}/rest/api/3/project/{jira_project}"
        resp = req.get(url, auth=(jira_email, jira_api_token), timeout=10)
        if resp.status_code == 200:
            proj_data = resp.json()
            return jsonify(
                {
                    "ok": True,
                    "project_name": proj_data.get("name", jira_project),
                    "project_key": jira_project,
                }
            )
        else:
            return jsonify(
                {
                    "ok": False,
                    "error": f"Jira returned {resp.status_code}: {resp.text[:200]}",
                }
            ), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
