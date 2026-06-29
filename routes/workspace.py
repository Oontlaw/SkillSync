import os
import secrets
from datetime import datetime, timedelta
from functools import wraps

import requests
from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import Date as SADate
from sqlalchemy import cast, func

from database import (
    AdminCorrection,
    BehavioralAnomaly,
    BurnoutRisk,
    LoginAttempt,
    Organisation,
    OrgMember,
    ScoreLog,
    Task,
    Worker,
    WorkerIdentity,
    db,
)
from routes.security import current_workspace_member
from scoring import correct_case
from services.slack import (
    notify_score_corrected,
    notify_task_completed,
    notify_task_missed,
    notify_worker_linked,
)

workspace_bp = Blueprint("workspace", __name__, url_prefix="/workspace")


# ---------------------------------------------------------------------------
# Auth decorators
# ---------------------------------------------------------------------------


def ws_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_workspace_member():
            return redirect(url_for("workspace.workspace_login"))
        return f(*args, **kwargs)

    return decorated


def ws_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        member = current_workspace_member(require_admin=True)
        if not member:
            if not session.get("ws_member_id"):
                return redirect(url_for("workspace.workspace_login"))
            return jsonify({"error": "Admin or HR role required"}), 403
        return f(*args, **kwargs)

    return decorated


def ws_strict_admin_required(f):
    """Strict admin-only — HR role is not sufficient."""

    @wraps(f)
    def decorated(*args, **kwargs):
        member_id = session.get("ws_member_id")
        org_id = session.get("ws_org_id")
        if not member_id or not org_id:
            return redirect(url_for("workspace.workspace_login"))
        member = OrgMember.query.filter_by(
            id=member_id, org_id=org_id, is_active=True
        ).first()
        if not member or member.role != "admin":
            return jsonify({"error": "Admin role required"}), 403
        return f(*args, **kwargs)

    return decorated


# Brute force protection constants
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15


def _check_login_rate_limit(email: str, ip: str):
    """Returns (is_locked_out, attempts_remaining).
    Counts failed attempts in the last LOGIN_LOCKOUT_MINUTES minutes."""
    cutoff = datetime.utcnow() - timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
    recent_failures = LoginAttempt.query.filter(
        LoginAttempt.email == email.lower(),
        LoginAttempt.attempted_at >= cutoff,
        LoginAttempt.success == False,
    ).count()
    is_locked = recent_failures >= LOGIN_MAX_ATTEMPTS
    remaining = max(0, LOGIN_MAX_ATTEMPTS - recent_failures)
    return is_locked, remaining


def _record_login_attempt(email: str, ip: str, success: bool):
    """Record a login attempt. Clean up old records while we're here."""
    attempt = LoginAttempt(
        email=email.lower(),
        ip_address=ip,
        success=success,
    )
    db.session.add(attempt)
    # Clean up attempts older than 24h to keep table small
    cutoff_cleanup = datetime.utcnow() - timedelta(hours=24)
    LoginAttempt.query.filter(LoginAttempt.attempted_at < cutoff_cleanup).delete()
    db.session.commit()


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@workspace_bp.route("/login", methods=["GET", "POST"])
def workspace_login():
    if request.method == "POST":
        slug = request.form.get("slug", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        ip = (
            request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
            .split(",")[0]
            .strip()
        )

        # Rate limit check FIRST — before any DB query
        is_locked, _remaining = _check_login_rate_limit(email, ip)
        if is_locked:
            return render_template(
                "workspace_login.html",
                error=(
                    f"Too many failed login attempts. "
                    f"Please wait {LOGIN_LOCKOUT_MINUTES} minutes before trying again."
                ),
                locked_out=True,
                login_lockout_minutes=LOGIN_LOCKOUT_MINUTES,
            )

        org = Organisation.query.filter_by(slug=slug, is_active=True).first()
        if not org:
            return render_template(
                "workspace_login.html", error="Organisation not found"
            )
        member = OrgMember.query.filter_by(
            org_id=org.id, email=email, is_active=True
        ).first()
        if not member:
            return render_template(
                "workspace_login.html",
                error="No account found with that email for this organisation",
            )
        if not member.check_password(password):
            _record_login_attempt(email, ip, success=False)
            import time

            time.sleep(0.5)
            return render_template("workspace_login.html", error="Invalid password")

        _record_login_attempt(email, ip, success=True)
        session.clear()  # Fix 1 — prevent session fixation
        member.last_login = datetime.utcnow()
        db.session.commit()
        session["ws_org_id"] = org.id
        session["ws_member_id"] = member.id
        session["ws_member_role"] = member.role
        session["ws_member_name"] = member.name
        session["ws_org_name"] = org.name
        session["ws_org_slug"] = org.slug
        return redirect(url_for("workspace.workspace_dashboard"))
    return render_template("workspace_login.html")


@workspace_bp.route("/logout", methods=["POST"])
def workspace_logout():
    for key in [
        "ws_org_id",
        "ws_member_id",
        "ws_member_role",
        "ws_member_name",
        "ws_org_name",
        "ws_org_slug",
    ]:
        session.pop(key, None)
    return redirect(url_for("workspace.workspace_login"))


@workspace_bp.route("/register", methods=["GET", "POST"])
def workspace_register():
    if request.method == "POST":
        org_name = request.form.get("org_name", "").strip()
        slug = request.form.get("slug", "").strip().lower()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not all([org_name, slug, name, email, password, confirm]):
            return render_template(
                "workspace_register.html", error="All fields required"
            )
        if password != confirm:
            return render_template(
                "workspace_register.html", error="Passwords do not match"
            )

        # Password strength validation
        pw_errors = []
        if len(password) < 8:
            pw_errors.append("Password must be at least 8 characters.")
        if not any(c.isupper() for c in password):
            pw_errors.append("Password must contain at least one uppercase letter.")
        if not any(c.isdigit() for c in password):
            pw_errors.append("Password must contain at least one number.")
        if pw_errors:
            for e in pw_errors:
                flash(e, "error")
            return render_template(
                "workspace_register.html",
                org_name=org_name,
                email=email,
            )
        if Organisation.query.filter_by(slug=slug).first():
            return render_template(
                "workspace_register.html", error="Organisation slug already taken"
            )
        api_key = secrets.token_urlsafe(32)
        org = Organisation(name=org_name, slug=slug, api_key=api_key)
        db.session.add(org)
        db.session.flush()
        member = OrgMember(org_id=org.id, email=email, name=name, role="admin")
        member.set_password(password)
        db.session.add(member)
        db.session.commit()
        return redirect(url_for("workspace.workspace_login"))
    return render_template("workspace_register.html")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@workspace_bp.route("/")
@ws_login_required
def workspace_dashboard():
    org_id = session["ws_org_id"]
    org_name = session["ws_org_name"]
    role = session.get("ws_member_role", "member")

    identities = WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()
    linked_ids = [i.worker_id for i in identities if i.worker_id]

    # Counts
    total_workers = len(linked_ids)
    total_tasks = (
        Task.query.filter(Task.worker_id.in_(linked_ids)).count() if linked_ids else 0
    )
    total_members = OrgMember.query.filter_by(org_id=org_id, is_active=True).count()

    discord_ids = [i.discord_id for i in identities if i.discord_id]
    anomaly_count = (
        BehavioralAnomaly.query.filter(
            BehavioralAnomaly.discord_id.in_(discord_ids),
            BehavioralAnomaly.cleared_at == None,
        ).count()
        if discord_ids
        else 0
    )

    # Task stats
    tasks_completed = (
        Task.query.filter(
            Task.worker_id.in_(linked_ids), Task.status == "completed"
        ).count()
        if linked_ids
        else 0
    )
    tasks_pending = (
        Task.query.filter(
            Task.worker_id.in_(linked_ids), Task.status == "pending"
        ).count()
        if linked_ids
        else 0
    )
    tasks_missed = (
        Task.query.filter(
            Task.worker_id.in_(linked_ids), Task.status == "missed"
        ).count()
        if linked_ids
        else 0
    )

    # Top 5 workers by score
    top_workers = []
    if linked_ids:
        scores_q = (
            db.session.query(
                ScoreLog.worker_id, func.sum(ScoreLog.change).label("total")
            )
            .filter(ScoreLog.worker_id.in_(linked_ids))
            .group_by(ScoreLog.worker_id)
            .order_by(func.sum(ScoreLog.change).desc())
            .limit(5)
            .all()
        )
        wmap = {
            w.id: w
            for w in Worker.query.filter(
                Worker.id.in_([r.worker_id for r in scores_q])
            ).all()
        }
        top_workers = [
            {"worker": wmap[r.worker_id], "score": float(r.total)}
            for r in scores_q
            if r.worker_id in wmap
        ]

    # Recent activity feed (last 10 score logs)
    recent_activity = []
    if linked_ids:
        logs = (
            ScoreLog.query.filter(ScoreLog.worker_id.in_(linked_ids))
            .order_by(ScoreLog.created_at.desc())
            .limit(10)
            .all()
        )
        wmap2 = {w.id: w for w in Worker.query.filter(Worker.id.in_(linked_ids)).all()}
        for log in logs:
            w = wmap2.get(log.worker_id)
            recent_activity.append(
                {
                    "worker_name": w.name if w else "Unknown",
                    "change": log.change,
                    "reason": log.reason or "",
                    "source": log.source or "",
                    "created_at": log.created_at,
                }
            )

    # Score trend chart (last 30 days, daily net)
    thirty_ago = datetime.utcnow() - timedelta(days=30)
    score_chart_labels = []
    score_chart_data = []
    if linked_ids:
        daily = (
            db.session.query(
                cast(ScoreLog.created_at, SADate).label("day"),
                func.sum(ScoreLog.change).label("total"),
            )
            .filter(
                ScoreLog.worker_id.in_(linked_ids), ScoreLog.created_at >= thirty_ago
            )
            .group_by(cast(ScoreLog.created_at, SADate))
            .order_by(cast(ScoreLog.created_at, SADate))
            .all()
        )
        for row in daily:
            score_chart_labels.append(str(row.day))
            score_chart_data.append(float(row.total))

    return render_template(
        "workspace_dashboard.html",
        org_name=org_name,
        role=role,
        total_workers=total_workers,
        total_tasks=total_tasks,
        total_members=total_members,
        anomaly_count=anomaly_count,
        tasks_completed=tasks_completed,
        tasks_pending=tasks_pending,
        tasks_missed=tasks_missed,
        top_workers=top_workers,
        recent_activity=recent_activity,
        score_chart_labels=score_chart_labels,
        score_chart_data=score_chart_data,
    )


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


@workspace_bp.route("/workers")
@ws_login_required
def workspace_workers():
    org_id = session["ws_org_id"]
    org_name = session["ws_org_name"]
    identities = WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()
    worker_ids = [i.worker_id for i in identities if i.worker_id]
    workers = Worker.query.filter(Worker.id.in_(worker_ids)).all() if worker_ids else []
    worker_map = {w.id: w for w in workers}
    rows = []
    for ident in identities:
        w = worker_map.get(ident.worker_id)
        rows.append(
            {
                "identity": ident,
                "worker": w,
                "task_count": Task.query.filter_by(worker_id=ident.worker_id).count()
                if ident.worker_id
                else 0,
                "score": w.score if w else None,
            }
        )
    return render_template("workspace_workers.html", rows=rows, org_name=org_name)


@workspace_bp.route("/workers/<int:worker_id>")
@ws_login_required
def workspace_worker_detail(worker_id):
    org_id = session["ws_org_id"]
    org_name = session["ws_org_name"]
    identity = WorkerIdentity.query.filter_by(
        org_id=org_id, worker_id=worker_id
    ).first()
    worker = db.session.get(Worker, worker_id)
    if not identity or not worker:
        return render_template(
            "workspace_worker_detail.html",
            error="Worker not found or not linked",
            org_name=org_name,
        )
    from ml.features import community_prior_for_worker

    tasks = (
        Task.query.filter_by(worker_id=worker_id)
        .order_by(Task.assigned_at.desc())
        .limit(50)
        .all()
    )
    scores = (
        ScoreLog.query.filter_by(worker_id=worker_id)
        .order_by(ScoreLog.created_at.desc())
        .limit(30)
        .all()
    )
    prior = None
    if identity.consent_community_prior and worker.discord_id:
        prior = community_prior_for_worker(worker.id)
    return render_template(
        "workspace_worker_detail.html",
        identity=identity,
        worker=worker,
        tasks=tasks,
        scores=scores,
        prior=prior,
        org_name=org_name,
        role=session.get("ws_member_role", "member"),
    )


# ---------------------------------------------------------------------------
# Worker Summary (Feature 2)
# ---------------------------------------------------------------------------


@workspace_bp.route("/workers/<int:worker_id>/summary")
@ws_login_required
def workspace_worker_summary(worker_id):
    """Auto-generated 30-day performance summary for a worker."""
    org_id = session["ws_org_id"]

    ident = WorkerIdentity.query.filter_by(
        org_id=org_id, worker_id=worker_id, is_active=True
    ).first_or_404()
    worker = db.session.get(Worker, worker_id)

    cutoff_30 = datetime.utcnow() - timedelta(days=30)
    cutoff_7 = datetime.utcnow() - timedelta(days=7)

    # Task stats
    tasks_30 = (
        Task.query.filter(
            Task.worker_id == worker_id,
            Task.assigned_at >= cutoff_30,
        ).all()
        if worker
        else []
    )
    completed = [t for t in tasks_30 if t.status == "completed"]
    missed = [t for t in tasks_30 if t.status == "missed"]
    pending = [t for t in tasks_30 if t.status == "pending"]
    on_time = [
        t
        for t in completed
        if t.due_at and t.completed_at and t.completed_at <= t.due_at
    ]
    completion_rate = round(len(completed) / max(len(tasks_30), 1) * 100, 1)
    on_time_rate = round(len(on_time) / max(len(completed), 1) * 100, 1)

    # Score trajectory
    score_logs_30 = (
        ScoreLog.query.filter(
            ScoreLog.worker_id == worker_id,
            ScoreLog.created_at >= cutoff_30,
        )
        .order_by(ScoreLog.created_at)
        .all()
        if worker
        else []
    )
    score_30d = sum(s.change for s in score_logs_30)
    score_7d = sum(s.change for s in score_logs_30 if s.created_at >= cutoff_7)

    # Score by week (4 weeks)
    now = datetime.utcnow()
    weekly_scores = []
    for week in range(4):
        w_start = now - timedelta(days=(4 - week) * 7)
        w_end = now - timedelta(days=(3 - week) * 7)
        w_score = sum(
            s.change for s in score_logs_30 if w_start <= s.created_at < w_end
        )
        weekly_scores.append(
            {
                "week": f"Week {week + 1}",
                "score": round(w_score, 1),
            }
        )

    # Trend direction
    if len(weekly_scores) >= 2:
        trend = (
            "improving"
            if weekly_scores[-1]["score"] >= weekly_scores[-2]["score"]
            else "declining"
        )
    else:
        trend = "stable"

    # Points breakdown by source
    points_by_source = {}
    for log in score_logs_30:
        src = log.source or "other"
        points_by_source[src] = points_by_source.get(src, 0) + log.change

    # Anomalies (only if consent given)
    anomaly_count = 0
    burnout_score = None
    if worker and ident.consent_community_prior and ident.discord_id:
        anomaly_count = BehavioralAnomaly.query.filter(
            BehavioralAnomaly.discord_id == ident.discord_id,
            BehavioralAnomaly.detected_at >= cutoff_30,
            BehavioralAnomaly.cleared_at == None,
        ).count()
        burnout = (
            BurnoutRisk.query.filter_by(discord_id=ident.discord_id)
            .order_by(BurnoutRisk.detected_at.desc())
            .first()
        )
        if burnout:
            burnout_score = burnout.score

    # Corrections in last 30d
    corrections_30 = (
        AdminCorrection.query.filter(
            AdminCorrection.worker_id == worker_id,
            AdminCorrection.created_at >= cutoff_30,
        ).count()
        if worker
        else 0
    )

    # Overall health rating
    health = "green"
    if len(missed) > 2 or score_30d < -20 or (burnout_score and burnout_score > 60):
        health = "red"
    elif len(missed) > 0 or score_30d < 0 or trend == "declining":
        health = "yellow"

    summary = {
        "worker": worker,
        "period": "Last 30 Days",
        "tasks": {
            "total": len(tasks_30),
            "completed": len(completed),
            "missed": len(missed),
            "pending": len(pending),
            "completion_rate": completion_rate,
            "on_time_rate": on_time_rate,
        },
        "score": {
            "total_30d": round(score_30d, 1),
            "total_7d": round(score_7d, 1),
            "weekly": weekly_scores,
            "by_source": points_by_source,
            "trend": trend,
        },
        "anomaly_count": anomaly_count,
        "burnout_score": burnout_score,
        "corrections": corrections_30,
        "health": health,
    }

    return render_template(
        "workspace_worker_summary.html",
        summary=summary,
        org_name=session["ws_org_name"],
        role=session.get("ws_member_role", "member"),
    )


# ---------------------------------------------------------------------------
# Task Creation (Feature 1)
# ---------------------------------------------------------------------------


@workspace_bp.route("/tasks/create", methods=["GET", "POST"])
@ws_login_required
def workspace_task_create():
    """HR/admin can manually create and assign a task to a linked worker."""
    org_id = session["ws_org_id"]
    role = session.get("ws_member_role", "member")
    if role not in ("admin", "hr"):
        return jsonify({"error": "Forbidden"}), 403

    identities = WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()
    linked_workers = []
    for ident in identities:
        if ident.worker_id:
            w = db.session.get(Worker, ident.worker_id)
            if w:
                linked_workers.append(
                    {
                        "id": w.id,
                        "name": w.name,
                        "display": ident.display_name or w.name,
                    }
                )

    if request.method == "POST":
        worker_id = request.form.get("worker_id", type=int)
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        priority = request.form.get("priority", "medium")
        due_days = request.form.get("due_days", type=int)

        if not worker_id or not title:
            flash("Worker and title are required.", "error")
            return render_template(
                "workspace_task_create.html",
                workers=linked_workers,
                org_name=session["ws_org_name"],
                role=role,
            )

        ident = WorkerIdentity.query.filter_by(
            org_id=org_id, worker_id=worker_id, is_active=True
        ).first()
        if not ident:
            return jsonify({"error": "Worker not in org"}), 403

        due_at = datetime.utcnow() + timedelta(days=due_days) if due_days else None

        task = Task(
            worker_id=worker_id,
            title=title,
            description=description,
            priority=priority,
            due_at=due_at,
            status="pending",
            source="manual",
            points_awarded=0.0,
        )
        db.session.add(task)
        db.session.commit()
        flash(f"Task '{title}' assigned successfully.", "success")
        return redirect(
            url_for("workspace.workspace_worker_detail", worker_id=worker_id)
        )

    return render_template(
        "workspace_task_create.html",
        workers=linked_workers,
        org_name=session["ws_org_name"],
        role=role,
    )


@workspace_bp.route("/tasks/<int:task_id>/update", methods=["POST"])
@ws_admin_required
def workspace_task_update(task_id):
    """Mark a task complete, missed, or cancelled. Awards/deducts points."""
    org_id = session["ws_org_id"]
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    ident = WorkerIdentity.query.filter_by(
        org_id=org_id, worker_id=task.worker_id, is_active=True
    ).first()
    if not ident:
        return jsonify({"error": "Forbidden"}), 403

    new_status = request.form.get("status")
    if new_status not in ("completed", "missed", "cancelled"):
        return jsonify({"error": "Invalid status"}), 400

    old_status = task.status
    task.status = new_status

    if new_status == "completed" and old_status != "completed":
        task.completed_at = datetime.utcnow()
        if task.due_at and datetime.utcnow() > task.due_at:
            pts = 5.0
            reason = f"Task completed late: {task.title}"
        else:
            pts = 10.0
            reason = f"Task completed on time: {task.title}"
        task.points_awarded = pts
        from scoring import award_points

        award_points(task.worker_id, pts, reason, source="work_engine")

    elif new_status == "missed" and old_status != "missed":
        pts = -15.0
        task.points_awarded = pts
        from scoring import award_points

        award_points(
            task.worker_id,
            pts,
            f"Task missed: {task.title}",
            source="work_engine",
        )

    db.session.commit()

    # Slack notification (fire-and-forget)
    worker = Worker.query.get(task.worker_id)
    worker_name = worker.name if worker else "Unknown"
    try:
        if new_status == "completed":
            on_time = not (
                task.due_at and task.completed_at and task.completed_at > task.due_at
            )
            notify_task_completed(
                worker_name=worker_name,
                task_title=task.title,
                pts=task.points_awarded,
                on_time=on_time,
                worker_id=task.worker_id,
            )
        elif new_status == "missed":
            notify_task_missed(
                worker_name=worker_name,
                task_title=task.title,
                worker_id=task.worker_id,
            )
    except Exception:
        pass  # never block on Slack failure

    return redirect(
        url_for("workspace.workspace_worker_detail", worker_id=task.worker_id)
    )


# ---------------------------------------------------------------------------
# Work Review — auto-judged task completion review
# ---------------------------------------------------------------------------


@workspace_bp.route("/work/review")
@ws_admin_required
def workspace_work_review():
    """Review auto-judged work_engine points entries.
    Shows pending (unreviewed) work_engine ScoreLog entries.
    Admin/HR can confirm or correct each auto-judgment."""
    org_id = session["ws_org_id"]
    org_name = session["ws_org_name"]
    role = session.get("ws_member_role", "member")

    linked_ids = [
        i.worker_id
        for i in WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()
        if i.worker_id
    ]

    entries = []
    total_pending = 0
    total_reviewed = 0
    total_corrected = 0

    if linked_ids:
        # Fetch unreviewed work_engine score logs
        raw = (
            ScoreLog.query.filter(
                ScoreLog.worker_id.in_(linked_ids),
                ScoreLog.source == "work_engine",
                ScoreLog.reviewed == False,
            )
            .order_by(ScoreLog.created_at.desc())
            .limit(50)
            .all()
        )

        # Stats
        total_pending = len(raw)
        total_reviewed = ScoreLog.query.filter(
            ScoreLog.worker_id.in_(linked_ids),
            ScoreLog.source == "work_engine",
            ScoreLog.reviewed == True,
        ).count()
        total_corrected = ScoreLog.query.filter(
            ScoreLog.worker_id.in_(linked_ids),
            ScoreLog.source == "work_engine",
            ScoreLog.admin_correction == True,
        ).count()

        wmap = {w.id: w for w in Worker.query.filter(Worker.id.in_(linked_ids)).all()}
        for log in raw:
            w = wmap.get(log.worker_id)
            entries.append(
                {
                    "log": log,
                    "worker_name": w.name if w else "Unknown",
                    "change_display": f"{'+' if log.change >= 0 else ''}{int(log.change)} pts",
                }
            )

    return render_template(
        "workspace_work_review.html",
        entries=entries,
        total_pending=total_pending,
        total_reviewed=total_reviewed,
        total_corrected=total_corrected,
        org_name=org_name,
        role=role,
    )


@workspace_bp.route("/work/review/<int:log_id>/action", methods=["POST"])
@ws_admin_required
def workspace_work_review_action(log_id):
    """Confirm or correct an auto-judged work_engine ScoreLog entry.
    Accepts JSON: {"action": "confirmed" | "corrected", "new_change": ..., "reason": ...}"""
    org_id = session["ws_org_id"]
    data = request.get_json(force=True)
    action = data.get("action")

    if action not in ("confirmed", "corrected"):
        return jsonify({"error": 'action must be "confirmed" or "corrected"'}), 400

    log = db.session.get(ScoreLog, log_id)
    if not log:
        return jsonify({"error": "ScoreLog entry not found"}), 404

    # Verify entry belongs to this org
    linked_ids = [
        i.worker_id
        for i in WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()
        if i.worker_id
    ]
    if log.worker_id not in linked_ids:
        return jsonify({"error": "Forbidden — worker not in your org"}), 403

    admin_name = session.get("ws_member_name", "Admin")

    if action == "confirmed":
        log.reviewed = True
        log.reviewed_at = datetime.utcnow()
        log.reviewed_by = admin_name
        db.session.commit()
        return jsonify({"ok": True, "action": "confirmed", "log_id": log_id})

    elif action == "corrected":
        new_change = data.get("new_change")
        reason = data.get("reason", "").strip()

        if new_change is None:
            return jsonify(
                {"error": "new_change is required for corrected entries"}
            ), 400

        # Use correct_case to record the correction (writes AdminCorrection, updates ScoreLog)
        result = correct_case(
            log_id,
            float(new_change),
            reason or "Admin correction via Work Review",
            admin_name,
        )

        if "error" in result:
            return jsonify({"error": result["error"]}), 400

        # Mark as reviewed and as admin_correction
        log.reviewed = True
        log.reviewed_at = datetime.utcnow()
        log.reviewed_by = admin_name
        log.admin_correction = True
        db.session.commit()

        # Fire retrain signal (fire and forget)
        try:
            api_key = os.getenv("API_KEY", "")
            requests.post(
                "http://localhost:5000/api/observer/ml/request-retrain",
                headers={"Authorization": f"Bearer {api_key}"},
                json={},
                timeout=2,
            )
        except Exception:
            pass

        return jsonify(
            {"ok": True, "action": "corrected", "log_id": log_id, "result": result}
        )

    return jsonify({"error": "Invalid action"}), 400


# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------


@workspace_bp.route("/identities")
@ws_login_required
def workspace_identities():
    org_id = session["ws_org_id"]
    org_name = session["ws_org_name"]
    role = session["ws_member_role"]
    identities = (
        WorkerIdentity.query.filter_by(org_id=org_id)
        .order_by(WorkerIdentity.linked_at.desc())
        .all()
    )
    unlinked_workers = (
        Worker.query.filter(
            ~Worker.id.in_(
                db.session.query(WorkerIdentity.worker_id).filter(
                    WorkerIdentity.org_id == org_id, WorkerIdentity.worker_id != None
                )
            )
        ).all()
        if role in ("admin", "hr")
        else []
    )
    return render_template(
        "workspace_identities.html",
        identities=identities,
        unlinked_workers=unlinked_workers,
        role=role,
        org_name=org_name,
    )


@workspace_bp.route("/identities/link", methods=["POST"])
@ws_admin_required
def workspace_link_identity():
    org_id = session["ws_org_id"]
    data = request.get_json(force=True)
    discord_id = data.get("discord_id", "").strip()
    worker_id = data.get("worker_id", type=int)
    org_employee_id = data.get("org_employee_id", "").strip()
    jira_account_id = data.get("jira_account_id", "").strip()
    display_name = data.get("display_name", "").strip()
    email = data.get("email", "").strip()

    if not discord_id and not worker_id:
        return jsonify({"error": "discord_id or worker_id required"}), 400

    # --- Duplicate checks within the same org ---

    # 1) discord_id uniqueness
    if discord_id:
        dup = WorkerIdentity.query.filter_by(
            org_id=org_id, discord_id=discord_id
        ).first()
        if dup and (not worker_id or dup.worker_id != worker_id):
            return jsonify(
                {
                    "error": f'discord_id "{discord_id}" already linked to worker {dup.worker_id}'
                }
            ), 409

    # 2) org_employee_id uniqueness
    if org_employee_id:
        dup = WorkerIdentity.query.filter_by(
            org_id=org_id, org_employee_id=org_employee_id
        ).first()
        if dup and (not worker_id or dup.worker_id != worker_id):
            return jsonify(
                {
                    "error": f'Employee ID "{org_employee_id}" already linked to worker {dup.worker_id}'
                }
            ), 409

    # 3) jira_account_id uniqueness
    if jira_account_id:
        dup = WorkerIdentity.query.filter_by(
            org_id=org_id, jira_account_id=jira_account_id
        ).first()
        if dup and (not worker_id or dup.worker_id != worker_id):
            return jsonify(
                {
                    "error": f'Jira account ID "{jira_account_id}" already linked to worker {dup.worker_id}'
                }
            ), 409

    # 4) worker_id uniqueness within org (one worker can only have one identity)
    if worker_id:
        dup = WorkerIdentity.query.filter_by(org_id=org_id, worker_id=worker_id).first()
        if dup and (not discord_id or dup.discord_id != discord_id):
            return jsonify(
                {
                    "error": f'Worker {worker_id} already linked to discord_id "{dup.discord_id or "?"}"'
                }
            ), 409

    # --- Update or create ---
    existing = (
        WorkerIdentity.query.filter_by(org_id=org_id, discord_id=discord_id).first()
        if discord_id
        else None
    )
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
        existing.linked_by = session["ws_member_name"]
    else:
        # Populate member_email from the current user (inviting member)
        member_email_from_session = None
        try:
            from database import OrgMember

            inviting_member = OrgMember.query.get(session.get("ws_member_id"))
            if inviting_member:
                member_email_from_session = inviting_member.email
        except Exception:
            pass

        identity = WorkerIdentity(
            org_id=org_id,
            worker_id=worker_id,
            discord_id=discord_id or None,
            org_employee_id=org_employee_id or None,
            jira_account_id=jira_account_id or None,
            display_name=display_name or None,
            email=email or None,
            member_email=member_email_from_session,
            linked_by=session["ws_member_name"],
        )
        db.session.add(identity)
    db.session.commit()

    # Slack notification for new identity (fire-and-forget)
    if not existing and worker_id and discord_id:
        try:
            worker_obj = Worker.query.get(worker_id)
            org_obj = Organisation.query.get(org_id)
            if worker_obj and discord_id:
                notify_worker_linked(
                    worker_name=worker_obj.name,
                    discord_id=discord_id,
                    org_name=org_obj.name
                    if org_obj
                    else session.get("ws_org_name", ""),
                    worker_id=worker_id,
                )
        except Exception:
            pass

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------


@workspace_bp.route("/leaderboard")
@ws_login_required
def workspace_leaderboard():
    org_id = session["ws_org_id"]
    org_name = session["ws_org_name"]
    role = session.get("ws_member_role", "member")

    period = request.args.get("period", "30d")
    cutoff = None
    now = datetime.utcnow()
    if period == "7d":
        cutoff = now - timedelta(days=7)
    elif period == "30d":
        cutoff = now - timedelta(days=30)
    # "all" -> no cutoff

    linked_ids = [
        i.worker_id
        for i in WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()
        if i.worker_id
    ]

    leaderboard = []
    if linked_ids:
        score_q = db.session.query(
            ScoreLog.worker_id, func.sum(ScoreLog.change).label("total")
        ).filter(ScoreLog.worker_id.in_(linked_ids))
        if cutoff:
            score_q = score_q.filter(ScoreLog.created_at >= cutoff)
        score_q = (
            score_q.group_by(ScoreLog.worker_id)
            .order_by(func.sum(ScoreLog.change).desc())
            .all()
        )

        # Score delta for 7d trend (always computed)
        cutoff_7d = now - timedelta(days=7)

        wmap = {w.id: w for w in Worker.query.filter(Worker.id.in_(linked_ids)).all()}
        for i, row in enumerate(score_q):
            w = wmap.get(row.worker_id)
            if w:
                # Task counts filtered by same period
                task_q = Task.query.filter_by(worker_id=row.worker_id)
                if cutoff:
                    task_q = task_q.filter(Task.assigned_at >= cutoff)
                all_tasks = task_q.all()
                done = sum(1 for t in all_tasks if t.status == "completed")
                missed = sum(1 for t in all_tasks if t.status == "missed")

                # 7d score delta for trend arrow
                delta_7d = (
                    db.session.query(func.sum(ScoreLog.change))
                    .filter(
                        ScoreLog.worker_id == row.worker_id,
                        ScoreLog.created_at >= cutoff_7d,
                    )
                    .scalar()
                    or 0
                )
                delta_7d = float(delta_7d)

                trend = "up" if delta_7d > 0 else ("down" if delta_7d < 0 else "stable")

                leaderboard.append(
                    {
                        "worker": w,
                        "score": float(row.total),
                        "score_delta_7d": round(delta_7d, 1),
                        "tasks_done": done,
                        "tasks_missed": missed,
                        "trend": trend,
                        "rank": i + 1,
                    }
                )

    total_corrections = AdminCorrection.query.count()

    return render_template(
        "workspace_leaderboard.html",
        leaderboard=leaderboard,
        total_corrections=total_corrections,
        org_name=org_name,
        role=role,
        period=period,
    )


# ---------------------------------------------------------------------------
# Team Health (Feature 4)
# ---------------------------------------------------------------------------


@workspace_bp.route("/team-health")
@ws_login_required
def workspace_team_health():
    """Traffic-light health view for the whole team."""
    org_id = session["ws_org_id"]
    role = session.get("ws_member_role", "member")
    cutoff_30 = datetime.utcnow() - timedelta(days=30)
    cutoff_7 = datetime.utcnow() - timedelta(days=7)

    identities = WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()

    team = []
    green_count = yellow_count = red_count = 0

    for ident in identities:
        if not ident.worker_id:
            continue
        worker = db.session.get(Worker, ident.worker_id)
        if not worker:
            continue

        # Score last 30d and last 7d
        logs_30 = ScoreLog.query.filter(
            ScoreLog.worker_id == ident.worker_id, ScoreLog.created_at >= cutoff_30
        ).all()
        score_30d = sum(s.change for s in logs_30)
        score_7d = sum(s.change for s in logs_30 if s.created_at >= cutoff_7)

        # Tasks
        tasks_30 = Task.query.filter(
            Task.worker_id == ident.worker_id, Task.assigned_at >= cutoff_30
        ).all()
        missed = sum(1 for t in tasks_30 if t.status == "missed")
        completed = sum(1 for t in tasks_30 if t.status == "completed")
        total = len(tasks_30)
        completion_rate = round(completed / max(total, 1) * 100)

        # Anomalies and burnout (consent-gated)
        anomaly_count = 0
        burnout_score = 0
        if ident.consent_community_prior and ident.discord_id:
            anomaly_count = BehavioralAnomaly.query.filter(
                BehavioralAnomaly.discord_id == ident.discord_id,
                BehavioralAnomaly.detected_at >= cutoff_30,
                BehavioralAnomaly.cleared_at == None,
            ).count()
            br = (
                BurnoutRisk.query.filter_by(discord_id=ident.discord_id)
                .order_by(BurnoutRisk.detected_at.desc())
                .first()
            )
            burnout_score = br.score if br else 0

        # Health rating
        red_flags = (
            missed > 2
            or score_30d < -20
            or burnout_score > 60
            or (total > 0 and completion_rate < 40)
        )
        yellow_flags = (
            missed > 0
            or score_30d < 0
            or score_7d < -5
            or burnout_score > 30
            or anomaly_count > 2
        )

        if red_flags:
            health = "red"
            red_count += 1
        elif yellow_flags:
            health = "yellow"
            yellow_count += 1
        else:
            health = "green"
            green_count += 1

        # Attention reasons (plain English)
        reasons = []
        if missed > 0:
            reasons.append(f"{missed} missed task{'s' if missed > 1 else ''}")
        if score_30d < 0:
            reasons.append(f"Score down {abs(round(score_30d, 1))} pts this month")
        if burnout_score > 30:
            reasons.append(f"Burnout risk {int(burnout_score)}%")
        if anomaly_count > 0:
            reasons.append(
                f"{anomaly_count} active anomal{'ies' if anomaly_count > 1 else 'y'}"
            )

        team.append(
            {
                "worker": worker,
                "worker_id": ident.worker_id,
                "health": health,
                "score_30d": round(score_30d, 1),
                "score_7d": round(score_7d, 1),
                "completion_rate": completion_rate,
                "tasks_total": total,
                "tasks_missed": missed,
                "burnout_score": int(burnout_score),
                "anomaly_count": anomaly_count,
                "reasons": reasons,
                "has_community_data": bool(
                    ident.consent_community_prior and ident.discord_id
                ),
            }
        )

    # Sort: red first, then yellow, then green; within each group by score desc
    team.sort(
        key=lambda x: (
            {"red": 0, "yellow": 1, "green": 2}[x["health"]],
            -x["score_30d"],
        )
    )

    return render_template(
        "workspace_team_health.html",
        team=team,
        org_name=session["ws_org_name"],
        role=role,
        summary={
            "green": green_count,
            "yellow": yellow_count,
            "red": red_count,
        },
    )


# ---------------------------------------------------------------------------
# Overrides (anomalies + score corrections)
# ---------------------------------------------------------------------------


@workspace_bp.route("/overrides")
@ws_admin_required
def workspace_overrides():
    org_id = session["ws_org_id"]
    org_name = session["ws_org_name"]
    role = session.get("ws_member_role", "member")

    identities_all = WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()
    linked_ids = [i.worker_id for i in identities_all if i.worker_id]
    discord_ids = [i.discord_id for i in identities_all if i.discord_id]

    # Active anomalies for org workers
    anomalies = []
    if discord_ids:
        raw = (
            BehavioralAnomaly.query.filter(
                BehavioralAnomaly.discord_id.in_(discord_ids),
                BehavioralAnomaly.cleared_at == None,
            )
            .order_by(BehavioralAnomaly.detected_at.desc())
            .limit(50)
            .all()
        )
        did_map = {
            i.discord_id: i for i in WorkerIdentity.query.filter_by(org_id=org_id).all()
        }
        wid_map = (
            {w.id: w for w in Worker.query.filter(Worker.id.in_(linked_ids)).all()}
            if linked_ids
            else {}
        )
        for a in raw:
            ident = did_map.get(a.discord_id)
            w = wid_map.get(ident.worker_id) if ident and ident.worker_id else None
            anomalies.append(
                {
                    "anomaly": a,
                    "worker_name": w.name
                    if w
                    else (ident.display_name if ident else a.discord_id),
                }
            )

    # Recent admin corrections
    corrections_raw = (
        (
            AdminCorrection.query.filter(AdminCorrection.worker_id.in_(linked_ids))
            .order_by(AdminCorrection.created_at.desc())
            .limit(20)
            .all()
        )
        if linked_ids
        else []
    )
    wid_map2 = (
        {
            w.id: w
            for w in Worker.query.filter(
                Worker.id.in_([c.worker_id for c in corrections_raw])
            ).all()
        }
        if corrections_raw
        else {}
    )
    recent_corrections = [
        {
            "correction": c,
            "worker_name": wid_map2[c.worker_id].name
            if c.worker_id in wid_map2
            else "Unknown",
        }
        for c in corrections_raw
    ]

    # Correctable score logs (last 100 for org workers)
    score_logs = []
    if linked_ids:
        logs = (
            ScoreLog.query.filter(ScoreLog.worker_id.in_(linked_ids))
            .order_by(ScoreLog.created_at.desc())
            .limit(100)
            .all()
        )
        wmap3 = {w.id: w for w in Worker.query.filter(Worker.id.in_(linked_ids)).all()}
        for log in logs:
            w = wmap3.get(log.worker_id)
            score_logs.append({"log": log, "worker_name": w.name if w else "Unknown"})

    # ML accuracy from model status
    ml_accuracy = None
    total_corrections_used = 0
    try:
        from ml import engine as ml_engine

        status = ml_engine.get_model_status()
        acc = status.get("accuracy_metrics", {}).get("forecast", {})
        ml_accuracy = acc.get("accuracy_pct")
        total_corrections_used = AdminCorrection.query.count()
    except Exception:
        total_corrections_used = AdminCorrection.query.count()

    return render_template(
        "workspace_overrides.html",
        anomalies=anomalies,
        recent_corrections=recent_corrections,
        score_logs=score_logs,
        ml_accuracy=ml_accuracy,
        total_corrections_used=total_corrections_used,
        org_name=org_name,
        role=role,
    )


@workspace_bp.route("/overrides/correct", methods=["POST"])
@ws_admin_required
def workspace_override_correct():
    org_id = session["ws_org_id"]
    data = request.get_json(force=True)
    case_id = data.get("case_id")
    new_change = data.get("new_change")
    reason = data.get("reason", "").strip()
    admin_name = session.get("ws_member_name", "Admin")

    if not case_id or new_change is None:
        return jsonify({"error": "case_id and new_change required"}), 400

    log = db.session.get(ScoreLog, case_id)
    if not log:
        return jsonify({"error": "Score log not found"}), 404

    linked_ids = [
        i.worker_id
        for i in WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()
        if i.worker_id
    ]
    if log.worker_id not in linked_ids:
        return jsonify({"error": "Not authorized"}), 403

    result = correct_case(
        case_id, float(new_change), reason or "Admin correction", admin_name
    )

    # Slack notification (fire-and-forget)
    try:
        if "error" not in result:
            notify_score_corrected(
                worker_name=result.get("worker", "Unknown"),
                original=float(result.get("original", 0)),
                corrected=float(result.get("new_change", 0)),
                reason=reason,
                corrected_by=admin_name,
                worker_id=log.worker_id,
            )
    except Exception:
        pass

    # Fire retrain signal (fire and forget)
    try:
        api_key = os.getenv("API_KEY", "")
        requests.post(
            "http://localhost:5000/api/observer/ml/request-retrain",
            headers={"Authorization": f"Bearer {api_key}"},
            json={},
            timeout=2,
        )
    except Exception:
        pass

    return jsonify({"ok": True, "result": result})


@workspace_bp.route("/overrides/retrain", methods=["POST"])
@ws_admin_required
def workspace_override_retrain():
    try:
        api_key = os.getenv("API_KEY", "")
        r = requests.post(
            "http://localhost:5000/api/observer/ml/retrain",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"trigger": "workspace_admin"},
            timeout=120,
        )
        return jsonify({"ok": True, "status": r.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@workspace_bp.route("/members")
@ws_admin_required
def workspace_members():
    org_id = session["ws_org_id"]
    org_name = session["ws_org_name"]
    role = session.get("ws_member_role", "member")

    members = (
        OrgMember.query.filter_by(org_id=org_id, is_active=True)
        .order_by(OrgMember.created_at)
        .all()
    )

    # For each member, find their linked worker identity by member_email first, then email, then display_name
    member_data = []
    for m in members:
        ident = WorkerIdentity.query.filter(
            WorkerIdentity.org_id == org_id,
            WorkerIdentity.is_active == True,
            db.or_(
                WorkerIdentity.member_email == m.email,
                WorkerIdentity.email == m.email,
                WorkerIdentity.display_name == m.name,
            ),
        ).first()
        worker = None
        score = None
        if ident and ident.worker_id:
            worker = db.session.get(Worker, ident.worker_id)
            if worker:
                score = (
                    db.session.query(func.sum(ScoreLog.change))
                    .filter(ScoreLog.worker_id == ident.worker_id)
                    .scalar()
                    or 0
                )
                score = round(float(score), 1)

        member_data.append(
            {
                "member": m,
                "worker": worker,
                "score": score,
                "identity": ident,
            }
        )

    return render_template(
        "workspace_members.html",
        member_data=member_data,
        current_member_id=session["ws_member_id"],
        org_name=org_name,
        role=role,
    )


@workspace_bp.route("/members/invite", methods=["POST"])
@ws_strict_admin_required
def workspace_members_invite():
    org_id = session["ws_org_id"]
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "member")
    password = request.form.get("password", "")
    if not all([name, email, password]):
        return redirect(
            url_for("workspace.workspace_members") + "?error=All+fields+required"
        )
    if OrgMember.query.filter_by(org_id=org_id, email=email).first():
        return redirect(
            url_for("workspace.workspace_members") + "?error=Email+already+exists"
        )
    member = OrgMember(
        org_id=org_id,
        email=email,
        name=name,
        role=role if role in ("admin", "hr", "member") else "member",
    )
    member.set_password(password)
    db.session.add(member)
    db.session.commit()
    return redirect(url_for("workspace.workspace_members"))


@workspace_bp.route("/members/<int:member_id>/role", methods=["POST"])
@ws_strict_admin_required
def workspace_members_role(member_id):
    org_id = session["ws_org_id"]
    data = request.get_json(force=True)
    new_role = data.get("role")
    if new_role not in ("admin", "hr", "member"):
        return jsonify({"error": "Invalid role"}), 400
    member = OrgMember.query.filter_by(id=member_id, org_id=org_id).first()
    if not member:
        return jsonify({"error": "Not found"}), 404
    # Prevent self-demotion if only admin
    if member.id == session["ws_member_id"] and new_role != "admin":
        admin_count = OrgMember.query.filter_by(
            org_id=org_id, role="admin", is_active=True
        ).count()
        if admin_count <= 1:
            return jsonify({"error": "Cannot remove the only admin"}), 400
    member.role = new_role
    db.session.commit()
    return jsonify({"ok": True})


@workspace_bp.route("/members/<int:member_id>/remove", methods=["POST"])
@ws_strict_admin_required
def workspace_members_remove(member_id):
    org_id = session["ws_org_id"]
    if member_id == session["ws_member_id"]:
        return jsonify({"error": "Cannot remove yourself"}), 400
    member = OrgMember.query.filter_by(id=member_id, org_id=org_id).first()
    if not member:
        return jsonify({"error": "Not found"}), 404
    member.is_active = False
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@workspace_bp.route("/settings", methods=["GET", "POST"])
@ws_admin_required
def workspace_settings():
    org_id = session["ws_org_id"]
    org = db.session.get(Organisation, org_id)
    org_name = session["ws_org_name"]
    if request.method == "POST":
        action = request.form.get("action") or ""
        if action == "test_slack":
            from services.slack import notify_team_health_summary

            sent = notify_team_health_summary(
                org_name=session.get("ws_org_name", "Test Org"),
                green=3,
                yellow=1,
                red=0,
            )
            if sent:
                flash("Test Slack message sent successfully.", "success")
            else:
                flash(
                    "Slack not configured or webhook failed. "
                    "Set SLACK_WEBHOOK_URL in your .env file.",
                    "error",
                )
            return redirect(url_for("workspace.workspace_settings"))
        data = request.get_json(force=True)
        org.share_feature_vectors = data.get(
            "share_feature_vectors", org.share_feature_vectors
        )
        org.share_anomaly_types = data.get(
            "share_anomaly_types", org.share_anomaly_types
        )
        org.store_task_content = data.get("store_task_content", org.store_task_content)
        db.session.commit()
        return jsonify({"ok": True})
    slack_configured = bool(os.environ.get("SLACK_WEBHOOK_URL", ""))
    return render_template(
        "workspace_settings.html",
        org=org,
        org_name=org_name,
        slack_configured=slack_configured,
    )


@workspace_bp.route("/settings/jira", methods=["POST"])
@ws_strict_admin_required
def workspace_settings_jira():
    org_id = session["ws_org_id"]
    data = request.get_json(force=True)
    org = db.session.get(Organisation, org_id)
    org.jira_url = data.get("jira_url", "").strip() or None
    if org.jira_url:
        from work_engine.connector_jira import _validate_jira_url

        url_ok, url_reason = _validate_jira_url(org.jira_url)
        if not url_ok:
            return jsonify(
                {"ok": False, "error": f"Invalid Jira URL: {url_reason}"}
            ), 400
    org.jira_email = data.get("jira_email", "").strip() or None
    if data.get("jira_api_token", "").strip():
        from database import encrypt_token

        org.jira_api_token = encrypt_token(data.get("jira_api_token", "").strip())
    org.jira_project = data.get("jira_project", "").strip() or None
    db.session.commit()
    return jsonify({"ok": True})


@workspace_bp.route("/settings/regen-key", methods=["POST"])
@ws_strict_admin_required
def workspace_settings_regen_key():
    org_id = session["ws_org_id"]
    org = db.session.get(Organisation, org_id)
    org.api_key = secrets.token_urlsafe(32)
    db.session.commit()
    return jsonify({"ok": True, "api_key": org.api_key})


@workspace_bp.route("/settings/sync", methods=["POST"])
@ws_admin_required
def workspace_settings_sync():
    """Trigger a Jira poll and sync for this org using org-level credentials."""
    try:
        from work_engine.connector_jira import poll_and_sync_for_org

        org_id = session["ws_org_id"]
        org = db.session.get(Organisation, org_id)
        if not org:
            return jsonify({"error": "Organisation not found"}), 404
        result = poll_and_sync_for_org(org)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@workspace_bp.route("/workers/<int:worker_id>/burnout")
@ws_admin_required
def workspace_worker_burnout(worker_id):
    """Return work burnout risk for a worker (task-data only)."""
    from ml.work_anomaly import detect_work_burnout

    org_id = session["ws_org_id"]
    result = detect_work_burnout(worker_id, org_id)
    return jsonify(result)


@workspace_bp.route("/scan/anomalies", methods=["POST"])
@ws_admin_required
def workspace_scan_anomalies():
    """Trigger a work anomaly scan for this org."""
    from ml.work_anomaly import run_org_scan

    org_id = session["ws_org_id"]
    results = run_org_scan(org_id)
    return jsonify({"ok": True, "anomalies": results})
