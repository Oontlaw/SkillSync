import secrets
from functools import wraps

from flask import jsonify, redirect, request, session, url_for
from sqlalchemy import false

from database import GuildInfo, GuildMember, OrgMember, Worker, WorkerIdentity

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def ensure_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf():
    if request.method in SAFE_METHODS:
        return None
    if request.blueprint in {"observer", "work"}:
        return None
    if request.endpoint in {
        "auth.callback",
        "workspace.workspace_login",
        "workspace.workspace_register",
    }:
        return None
    if not session.get("user") and not session.get("ws_member_id"):
        return None

    supplied = (
        request.headers.get("X-CSRF-Token")
        or request.headers.get("X-CSRFToken")
        or request.form.get("_csrf_token")
    )
    expected = session.get("_csrf_token")
    if not supplied or not expected or not secrets.compare_digest(supplied, expected):
        return jsonify({"error": "Invalid CSRF token"}), 403
    return None


def accessible_guild_ids():
    current = session.get("accessible_guilds", [])
    requested = {
        str(g.get("id")) for g in current if isinstance(g, dict) and g.get("id")
    }
    if not requested:
        return []
    present = {
        row.guild_id
        for row in GuildInfo.query.with_entities(GuildInfo.guild_id)
        .filter(GuildInfo.guild_id.in_(requested))
        .all()
    }
    filtered = [
        g for g in current if isinstance(g, dict) and str(g.get("id")) in present
    ]
    if filtered != current:
        session["accessible_guilds"] = filtered
        session.modified = True
    return [str(g["id"]) for g in filtered]


def accessible_worker_ids(guild_ids=None):
    guild_ids = accessible_guild_ids() if guild_ids is None else list(guild_ids)
    if not guild_ids:
        return []
    rows = (
        Worker.query.with_entities(Worker.id)
        .join(GuildMember, GuildMember.member_id == Worker.discord_id)
        .filter(GuildMember.guild_id.in_(guild_ids))
        .distinct()
        .all()
    )
    return [row.id for row in rows]


def accessible_discord_ids(guild_ids=None):
    guild_ids = accessible_guild_ids() if guild_ids is None else list(guild_ids)
    if not guild_ids:
        return []
    rows = (
        GuildMember.query.with_entities(GuildMember.member_id)
        .filter(GuildMember.guild_id.in_(guild_ids))
        .distinct()
        .all()
    )
    return [row.member_id for row in rows]


def accessible_guilds_for_worker(worker_id, guild_ids=None):
    guild_ids = accessible_guild_ids() if guild_ids is None else list(guild_ids)
    if not guild_ids:
        return []
    worker = (
        Worker.query.with_entities(Worker.discord_id).filter_by(id=worker_id).first()
    )
    if not worker or not worker.discord_id:
        return []
    rows = (
        GuildMember.query.with_entities(GuildMember.guild_id)
        .filter(
            GuildMember.member_id == worker.discord_id,
            GuildMember.guild_id.in_(guild_ids),
        )
        .distinct()
        .all()
    )
    return [row.guild_id for row in rows]


def guild_scope(column, guild_ids=None):
    guild_ids = accessible_guild_ids() if guild_ids is None else list(guild_ids)
    return column.in_(guild_ids) if guild_ids else false()


def worker_scope(column, worker_ids=None):
    worker_ids = accessible_worker_ids() if worker_ids is None else list(worker_ids)
    return column.in_(worker_ids) if worker_ids else false()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)

    return decorated


def discord_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Authentication required"}), 401
        if not accessible_guild_ids():
            return jsonify({"error": "Guild administrator access required"}), 403
        return f(*args, **kwargs)

    return decorated


def current_workspace_member(require_admin=False):
    member_id = session.get("ws_member_id")
    org_id = session.get("ws_org_id")
    if not member_id or not org_id:
        return None
    member = OrgMember.query.filter_by(
        id=member_id,
        org_id=org_id,
        is_active=True,
    ).first()
    if not member or (require_admin and member.role not in ("admin", "hr")):
        return None
    session["ws_member_role"] = member.role
    session["ws_member_name"] = member.name
    session["ws_org_name"] = member.organisation.name
    session["ws_org_slug"] = member.organisation.slug
    return member


def workspace_worker_ids(org_id=None, active_only=True):
    org_id = org_id or session.get("ws_org_id")
    if not org_id:
        return []
    query = WorkerIdentity.query.with_entities(WorkerIdentity.worker_id).filter(
        WorkerIdentity.org_id == org_id,
        WorkerIdentity.worker_id.isnot(None),
    )
    if active_only:
        query = query.filter(WorkerIdentity.is_active.is_(True))
    return [row.worker_id for row in query.distinct().all()]


def clear_workspace_session():
    for key in (
        "ws_org_id",
        "ws_member_id",
        "ws_member_role",
        "ws_member_name",
        "ws_org_name",
        "ws_org_slug",
    ):
        session.pop(key, None)


def workspace_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_workspace_member():
            clear_workspace_session()
            return redirect(url_for("workspace.workspace_login"))
        return f(*args, **kwargs)

    return decorated


def workspace_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("ws_member_id"):
            return redirect(url_for("workspace.workspace_login"))
        if not current_workspace_member(require_admin=True):
            return jsonify({"error": "Admin or HR role required"}), 403
        return f(*args, **kwargs)

    return decorated
