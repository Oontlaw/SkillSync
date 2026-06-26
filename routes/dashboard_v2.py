import json
import os
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from database import (
    AdminCorrection,
    BehavioralAnomaly,
    BurnoutRisk,
    GuildChannel,
    GuildInfo,
    GuildMember,
    GuildRole,
    MemberJoinLeave,
    MessageRecord,
    ScoreLog,
    Task,
    VoiceActivity,
    Worker,
    db,
)
from ml import engine as ml_engine

dashboard_v2_bp = Blueprint("dashboard_v2", __name__)


def get_accessible_guild_ids():
    """Return list of guild IDs the logged-in user can access."""
    _refresh_accessible_guilds()
    current = session.get("accessible_guilds", [])
    return [g["id"] for g in current if isinstance(g, dict)]


def _refresh_accessible_guilds():
    """Cross-reference session guilds with current bot GuildInfo."""
    bot_guild_ids = set(
        g.guild_id for g in GuildInfo.query.with_entities(GuildInfo.guild_id).all()
    )
    current = session.get("accessible_guilds", [])
    filtered = [
        g for g in current if isinstance(g, dict) and g.get("id") in bot_guild_ids
    ]
    if filtered != current:
        session["accessible_guilds"] = filtered
        session.modified = True


@dashboard_v2_bp.route("/")
def index():
    """Render the v2 dashboard with workspace styling — mirrors main dashboard data."""
    user = session.get("user")

    if not user:
        return redirect(url_for("auth.login"))

    accessible_ids = get_accessible_guild_ids()

    # Guild-scoped filter for ScoreLog
    if accessible_ids:
        scorelog_filter = db.or_(
            ScoreLog.guild_id.in_(accessible_ids), ScoreLog.guild_id == None
        )
    else:
        scorelog_filter = None

    # Per-guild scores for leaderboard
    per_guild_scores_query = db.session.query(
        ScoreLog.worker_id, ScoreLog.guild_id, func.sum(ScoreLog.change).label("score")
    )
    if scorelog_filter is not None:
        per_guild_scores_query = per_guild_scores_query.filter(scorelog_filter)
    per_guild_scores = (
        per_guild_scores_query.group_by(ScoreLog.worker_id, ScoreLog.guild_id)
        .order_by(func.sum(ScoreLog.change).desc())
        .all()
    )

    worker_ids = {pg.worker_id for pg in per_guild_scores}
    guild_ids = {pg.guild_id for pg in per_guild_scores if pg.guild_id}
    workers = {w.id: w for w in Worker.query.filter(Worker.id.in_(worker_ids)).all()}
    guild_info_map = {
        g.guild_id: g
        for g in GuildInfo.query.filter(GuildInfo.guild_id.in_(guild_ids)).all()
    }

    leaderboard_data = []
    for pg in per_guild_scores:
        worker = workers.get(pg.worker_id)
        guild_name = "Unknown"
        if pg.guild_id:
            guild = guild_info_map.get(pg.guild_id)
            if guild:
                guild_name = guild.name
        if worker:
            leaderboard_data.append(
                {
                    "worker": worker,
                    "guild_id": pg.guild_id or "",
                    "guild_name": guild_name,
                    "score": pg.score,
                }
            )

    total_workers = Worker.query.count()
    total_tasks = Task.query.count()
    total_corrections = AdminCorrection.query.count()

    # Work Engine stats
    work_external_tasks = Task.query.filter(Task.source != None).count()
    work_sources = (
        db.session.query(Task.source).filter(Task.source != None).distinct().count()
    )
    work_recent_syncs = (
        Task.query.filter(Task.source != None)
        .order_by(Task.assigned_at.desc())
        .limit(10)
        .options(selectinload(Task.worker))
        .all()
    )
    for t in work_recent_syncs:
        t.worker_name = t.worker.name if t.worker else "Unknown"

    total_moderation_actions = ScoreLog.query.filter(ScoreLog.source == "discord")
    if scorelog_filter is not None:
        total_moderation_actions = total_moderation_actions.filter(scorelog_filter)
    total_moderation_actions = total_moderation_actions.count()

    # Recent logs
    recent_logs_query = ScoreLog.query
    if scorelog_filter is not None:
        recent_logs_query = recent_logs_query.filter(scorelog_filter)
    recent_logs = (
        recent_logs_query.order_by(ScoreLog.created_at.desc())
        .limit(10)
        .options(selectinload(ScoreLog.worker))
        .all()
    )
    recent_logs = [log for log in recent_logs if log.worker]
    log_guild_ids = {log.guild_id for log in recent_logs if log.guild_id}
    log_guild_info_map = {
        g.guild_id: g
        for g in GuildInfo.query.filter(GuildInfo.guild_id.in_(log_guild_ids)).all()
    }
    for log in recent_logs:
        log.guild_name = None
        if log.guild_id:
            g = log_guild_info_map.get(log.guild_id)
            if g:
                log.guild_name = g.name

    # Common guild filter for message/voice queries
    guild_filter = (
        MessageRecord.guild_id.in_(accessible_ids) if accessible_ids else None
    )
    voice_guild_filter = (
        VoiceActivity.guild_id.in_(accessible_ids) if accessible_ids else None
    )

    # Behavioral analytics
    msg_base = MessageRecord.query
    if guild_filter is not None:
        msg_base = msg_base.filter(guild_filter)
    total_messages_logged = msg_base.count()
    unique_users_tracked = (
        msg_base.with_entities(MessageRecord.discord_id).distinct().count()
    )

    voice_base = VoiceActivity.query
    if voice_guild_filter is not None:
        voice_base = voice_base.filter(voice_guild_filter)
    total_voice_sessions = voice_base.count()
    total_voice_hours = round(
        (
            voice_base.with_entities(func.sum(VoiceActivity.duration_seconds)).scalar()
            or 0
        )
        / 3600,
        1,
    )

    most_active = (
        msg_base.with_entities(
            MessageRecord.name,
            MessageRecord.discord_id,
            func.count(MessageRecord.id).label("count"),
        )
        .group_by(MessageRecord.name, MessageRecord.discord_id)
        .order_by(func.count(MessageRecord.id).desc())
        .limit(5)
        .all()
    )

    # Hourly activity
    hourly_data = (
        msg_base.with_entities(
            MessageRecord.hour_of_day, func.count(MessageRecord.id).label("count")
        )
        .group_by(MessageRecord.hour_of_day)
        .order_by(MessageRecord.hour_of_day)
        .all()
    )
    hourly_activity = {str(h): 0 for h in range(24)}
    for h, c in hourly_data:
        hourly_activity[str(h)] = c

    # Per-guild hourly
    guild_hourly_raw = (
        msg_base.with_entities(
            MessageRecord.guild_id,
            MessageRecord.hour_of_day,
            func.count(MessageRecord.id).label("count"),
        )
        .group_by(MessageRecord.guild_id, MessageRecord.hour_of_day)
        .order_by(MessageRecord.guild_id, MessageRecord.hour_of_day)
        .all()
    )
    guild_hourly = {}
    for guild_id, hour, count in guild_hourly_raw:
        if guild_id not in guild_hourly:
            guild_hourly[guild_id] = {str(h): 0 for h in range(24)}
        guild_hourly[guild_id][str(hour)] = count

    # Per-guild message totals
    guild_msg_counts_raw = (
        msg_base.with_entities(
            MessageRecord.guild_id, func.count(MessageRecord.id).label("count")
        )
        .group_by(MessageRecord.guild_id)
        .all()
    )
    guild_msg_counts = {g: c for g, c in guild_msg_counts_raw}

    # Daily volume (7 days)
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    daily_vol = (
        msg_base.filter(MessageRecord.created_at >= seven_days_ago)
        .with_entities(
            func.date(MessageRecord.created_at).label("day"),
            func.count(MessageRecord.id).label("count"),
        )
        .group_by(func.date(MessageRecord.created_at))
        .order_by(func.date(MessageRecord.created_at))
        .all()
    )
    daily_volume = {str(d.day): d.count for d in daily_vol}

    # Score sources
    source_query = db.session.query(
        ScoreLog.source, func.count(ScoreLog.id).label("count")
    )
    if scorelog_filter is not None:
        source_query = source_query.filter(scorelog_filter)
    source_data = source_query.group_by(ScoreLog.source).all()
    score_sources = {s.source: s.count for s in source_data}

    # Guild overview
    guilds = (
        GuildInfo.query.filter(GuildInfo.guild_id.in_(accessible_ids))
        .order_by(GuildInfo.name)
        .all()
        if accessible_ids
        else []
    )
    total_guilds = len(guilds)

    guild_member_count_map = (
        dict(
            db.session.query(GuildMember.guild_id, func.count(GuildMember.id))
            .filter(
                GuildMember.guild_id.in_(accessible_ids),
                GuildMember.is_bot == False,
            )
            .group_by(GuildMember.guild_id)
            .all()
        )
        if accessible_ids
        else {}
    )
    total_members_tracked = sum(guild_member_count_map.values())

    guild_online_map = {}
    for g in guilds:
        guild_online_map[g.guild_id] = (
            g.online_count if g.online_count is not None else 0
        )
    total_online_members = sum(guild_online_map.values())

    total_staff_tracked = (
        GuildMember.query.filter(
            GuildMember.is_staff == True,
            GuildMember.is_bot == False,
            GuildMember.guild_id.in_(accessible_ids),
        ).count()
        if accessible_ids
        else 0
    )

    # Anomalies (last 48h)
    recent_anomalies = BehavioralAnomaly.query.filter(
        BehavioralAnomaly.cleared_at == None,
        BehavioralAnomaly.detected_at > datetime.utcnow() - timedelta(hours=48),
    )
    if accessible_ids:
        recent_anomalies = recent_anomalies.filter(
            db.or_(
                BehavioralAnomaly.guild_id == None,
                BehavioralAnomaly.guild_id.in_(accessible_ids),
            )
        )
    recent_anomalies = (
        recent_anomalies.order_by(BehavioralAnomaly.severity.desc()).limit(10).all()
    )

    # Burnout risks
    burnout_risks = BurnoutRisk.query.order_by(BurnoutRisk.score.desc()).limit(5).all()

    # Join/leave stats (7d)
    cutoff = datetime.utcnow() - timedelta(days=7)
    join_leave_query = MemberJoinLeave.query.filter(
        MemberJoinLeave.created_at >= cutoff
    )
    if accessible_ids:
        join_leave_query = join_leave_query.filter(
            MemberJoinLeave.guild_id.in_(accessible_ids)
        )

    total_joins_7d = join_leave_query.filter(
        MemberJoinLeave.event_type == "join"
    ).count()
    total_leaves_7d = join_leave_query.filter(
        MemberJoinLeave.event_type == "leave"
    ).count()

    hourly_joins_7d = {str(h): 0 for h in range(24)}
    hourly_leaves_7d = {str(h): 0 for h in range(24)}

    hourly_joins_data = db.session.query(
        MemberJoinLeave.hour_of_day, func.count(MemberJoinLeave.id)
    ).filter(
        MemberJoinLeave.created_at >= cutoff,
        MemberJoinLeave.event_type == "join",
        MemberJoinLeave.hour_of_day != None,
    )
    if accessible_ids:
        hourly_joins_data = hourly_joins_data.filter(
            MemberJoinLeave.guild_id.in_(accessible_ids)
        )
    hourly_joins_data = hourly_joins_data.group_by(MemberJoinLeave.hour_of_day).all()
    for h, c in hourly_joins_data:
        hourly_joins_7d[str(h)] = c

    hourly_leaves_data = db.session.query(
        MemberJoinLeave.hour_of_day, func.count(MemberJoinLeave.id)
    ).filter(
        MemberJoinLeave.created_at >= cutoff,
        MemberJoinLeave.event_type == "leave",
        MemberJoinLeave.hour_of_day != None,
    )
    if accessible_ids:
        hourly_leaves_data = hourly_leaves_data.filter(
            MemberJoinLeave.guild_id.in_(accessible_ids)
        )
    hourly_leaves_data = hourly_leaves_data.group_by(MemberJoinLeave.hour_of_day).all()
    for h, c in hourly_leaves_data:
        hourly_leaves_7d[str(h)] = c

    # ML model status
    ml_status = ml_engine.get_model_status()
    ml_last_train = None
    ml_corrector_stats = None
    summary_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "ml",
        "models",
        "training_summary.json",
    )
    if os.path.exists(summary_path):
        try:
            with open(summary_path) as f:
                summary = json.load(f)
                ml_last_train = summary.get("trained_at", "")
                ml_corrector_stats = summary.get("corrector")
        except Exception:
            pass

    anomaly_precision = ml_status.get("anomaly_precision", {})
    burnout_precision = ml_status.get("burnout_precision", {})
    model_health = ml_status.get("health", {})
    health_drift_detected = model_health.get("drift_detected", False)
    health_drift_reasons = model_health.get("drift_reasons", [])
    growth_status = ml_status.get("growth", {})

    return render_template(
        "dashboard_v2.html",
        user=user,
        accessible_guilds=session.get("accessible_guilds", []),
        leaderboard_data=leaderboard_data,
        total_workers=total_workers,
        total_tasks=total_tasks,
        total_corrections=total_corrections,
        total_moderation_actions=total_moderation_actions,
        recent_logs=recent_logs,
        total_messages_logged=total_messages_logged,
        unique_users_tracked=unique_users_tracked,
        most_active=most_active,
        guilds=guilds,
        total_guilds=total_guilds,
        total_members_tracked=total_members_tracked,
        total_online_members=total_online_members,
        total_staff_tracked=total_staff_tracked,
        total_voice_sessions=total_voice_sessions,
        total_voice_hours=total_voice_hours,
        anomalies=recent_anomalies,
        hourly_activity=hourly_activity,
        daily_volume=daily_volume,
        score_sources=score_sources,
        guild_hourly=guild_hourly,
        guild_msg_counts=guild_msg_counts,
        guild_name_map={g.guild_id: g.name for g in guilds},
        guild_online_map=guild_online_map,
        guild_member_count_map=guild_member_count_map,
        burnout_risks=burnout_risks,
        ml_status=ml_status,
        ml_last_train=ml_last_train,
        ml_corrector_stats=ml_corrector_stats,
        work_external_tasks=work_external_tasks,
        work_sources=work_sources,
        work_recent_syncs=work_recent_syncs,
        total_joins_7d=total_joins_7d,
        total_leaves_7d=total_leaves_7d,
        hourly_joins_7d=hourly_joins_7d,
        hourly_leaves_7d=hourly_leaves_7d,
        anomaly_precision=anomaly_precision,
        burnout_precision=burnout_precision,
        health_drift_detected=health_drift_detected,
        health_drift_reasons=health_drift_reasons,
        growth_status=growth_status,
    )


@dashboard_v2_bp.route("/anomaly-feedback", methods=["POST"])
def v2_anomaly_feedback():
    """Proxy endpoint for admin feedback on anomaly predictions."""
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401

    data = request.json or {}
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    if not data.get("anomaly_id") or not data.get("feedback"):
        return jsonify({"error": "Missing required fields"}), 400

    feedback = data["feedback"]
    if feedback not in ("confirmed", "dismissed"):
        return jsonify({"error": 'feedback must be "confirmed" or "dismissed"'}), 400

    accessible_ids = get_accessible_guild_ids()
    anomaly = BehavioralAnomaly.query.get_or_404(int(data["anomaly_id"]))
    if anomaly.guild_id and accessible_ids and anomaly.guild_id not in accessible_ids:
        return jsonify({"error": "Unauthorized"}), 403

    anomaly.feedback = feedback
    anomaly.feedback_at = datetime.utcnow()
    db.session.commit()

    return jsonify({"status": "ok", "feedback": feedback, "anomaly_id": anomaly.id})


@dashboard_v2_bp.route("/burnout-feedback", methods=["POST"])
def v2_burnout_feedback():
    """Proxy endpoint for admin feedback on burnout predictions."""
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401

    data = request.json or {}
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    if not data.get("risk_id") or not data.get("feedback"):
        return jsonify({"error": "Missing required fields"}), 400

    feedback = data["feedback"]
    if feedback not in ("confirmed", "dismissed"):
        return jsonify({"error": 'feedback must be "confirmed" or "dismissed"'}), 400

    risk = BurnoutRisk.query.get_or_404(int(data["risk_id"]))
    risk.feedback = feedback
    risk.feedback_at = datetime.utcnow()
    db.session.commit()

    return jsonify({"status": "ok", "feedback": feedback, "risk_id": risk.id})


@dashboard_v2_bp.route("/_live")
def v2_live():
    """JSON — live online counts per guild (polled by JS)."""
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401

    accessible_ids = get_accessible_guild_ids()
    if not accessible_ids:
        return jsonify(
            {
                "total_online_members": 0,
                "total_members_tracked": 0,
                "guild_online_map": {},
            }
        )

    guilds = GuildInfo.query.filter(GuildInfo.guild_id.in_(accessible_ids)).all()
    guild_online_map = {}
    guild_member_count_map = {}
    for g in guilds:
        guild_online_map[g.guild_id] = (
            g.online_count if g.online_count is not None else 0
        )
        guild_member_count_map[g.guild_id] = GuildMember.query.filter(
            GuildMember.guild_id == g.guild_id, GuildMember.is_bot == False
        ).count()

    return jsonify(
        {
            "total_online_members": sum(guild_online_map.values()),
            "total_members_tracked": sum(guild_member_count_map.values()),
            "guild_online_map": guild_online_map,
            "guild_member_count_map": guild_member_count_map,
        }
    )
