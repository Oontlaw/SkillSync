import os
import secrets
from datetime import datetime, timedelta
from functools import wraps

import requests
from flask import (
    Blueprint,
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
    Organisation,
    OrgMember,
    ScoreLog,
    Task,
    Worker,
    WorkerIdentity,
    db,
)
from scoring import correct_case

workspace_bp = Blueprint("workspace", __name__, url_prefix="/workspace")


# ---------------------------------------------------------------------------
# Auth decorators
# ---------------------------------------------------------------------------


def ws_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("ws_member_id"):
            return redirect(url_for("workspace.workspace_login"))
        return f(*args, **kwargs)

    return decorated


def ws_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("ws_member_id"):
            return redirect(url_for("workspace.workspace_login"))
        if session.get("ws_member_role") not in ("admin", "hr"):
            return jsonify({"error": "Admin or HR role required"}), 403
        return f(*args, **kwargs)

    return decorated


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@workspace_bp.route("/login", methods=["GET", "POST"])
def workspace_login():
    if request.method == "POST":
        slug = request.form.get("slug", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
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
            return render_template("workspace_login.html", error="Invalid password")
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
        .order_by(Task.created_at.desc())
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
    )


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
        identity = WorkerIdentity(
            org_id=org_id,
            worker_id=worker_id,
            discord_id=discord_id or None,
            org_employee_id=org_employee_id or None,
            jira_account_id=jira_account_id or None,
            display_name=display_name or None,
            email=email or None,
            linked_by=session["ws_member_name"],
        )
        db.session.add(identity)
    db.session.commit()
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

    linked_ids = [
        i.worker_id
        for i in WorkerIdentity.query.filter_by(org_id=org_id, is_active=True).all()
        if i.worker_id
    ]

    leaderboard = []
    if linked_ids:
        scores_q = (
            db.session.query(
                ScoreLog.worker_id, func.sum(ScoreLog.change).label("total")
            )
            .filter(ScoreLog.worker_id.in_(linked_ids))
            .group_by(ScoreLog.worker_id)
            .order_by(func.sum(ScoreLog.change).desc())
            .all()
        )
        wmap = {w.id: w for w in Worker.query.filter(Worker.id.in_(linked_ids)).all()}
        for i, row in enumerate(scores_q):
            w = wmap.get(row.worker_id)
            if w:
                done = Task.query.filter_by(
                    worker_id=row.worker_id, status="completed"
                ).count()
                missed = Task.query.filter_by(
                    worker_id=row.worker_id, status="missed"
                ).count()
                leaderboard.append(
                    {
                        "worker": w,
                        "score": float(row.total),
                        "tasks_done": done,
                        "tasks_missed": missed,
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
    return render_template(
        "workspace_members.html",
        members=members,
        current_member_id=session["ws_member_id"],
        org_name=org_name,
        role=role,
    )


@workspace_bp.route("/members/invite", methods=["POST"])
@ws_admin_required
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
@ws_admin_required
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
@ws_admin_required
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
    return render_template("workspace_settings.html", org=org, org_name=org_name)


@workspace_bp.route("/settings/jira", methods=["POST"])
@ws_admin_required
def workspace_settings_jira():
    org_id = session["ws_org_id"]
    data = request.get_json(force=True)
    org = db.session.get(Organisation, org_id)
    org.jira_url = data.get("jira_url", "").strip() or None
    org.jira_email = data.get("jira_email", "").strip() or None
    if data.get("jira_api_token", "").strip():
        org.jira_api_token = data.get("jira_api_token", "").strip()
    org.jira_project = data.get("jira_project", "").strip() or None
    db.session.commit()
    return jsonify({"ok": True})


@workspace_bp.route("/settings/regen-key", methods=["POST"])
@ws_admin_required
def workspace_settings_regen_key():
    org_id = session["ws_org_id"]
    org = db.session.get(Organisation, org_id)
    org.api_key = secrets.token_urlsafe(32)
    db.session.commit()
    return jsonify({"ok": True, "api_key": org.api_key})


@workspace_bp.route("/settings/sync", methods=["POST"])
@ws_admin_required
def workspace_settings_sync():
    try:
        return jsonify(
            {"ok": True, "message": "Sync triggered — bot will poll Jira on next cycle"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Magic login (development helper — kept as-is)
# ---------------------------------------------------------------------------


@workspace_bp.route("/magic-login")
def magic_login():
    slug = request.args.get("slug", "").strip()
    email = request.args.get("email", "").strip().lower()
    if not slug or not email:
        return jsonify({"error": "slug and email required"}), 400
    org = Organisation.query.filter_by(slug=slug, is_active=True).first()
    if not org:
        return jsonify({"error": "Organisation not found"}), 404
    member = OrgMember.query.filter_by(
        org_id=org.id, email=email, is_active=True
    ).first()
    if not member:
        return jsonify({"error": "Member not found"}), 404
    member.last_login = datetime.utcnow()
    db.session.commit()
    session["ws_org_id"] = org.id
    session["ws_member_id"] = member.id
    session["ws_member_role"] = member.role
    session["ws_member_name"] = member.name
    session["ws_org_name"] = org.name
    session["ws_org_slug"] = org.slug
    return redirect(url_for("workspace.workspace_dashboard"))
