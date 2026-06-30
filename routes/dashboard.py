import json
import os
import statistics
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
    AutoModRule,
    AutoModTrigger,
    BehavioralAnomaly,
    BurnoutRisk,
    GuildChannel,
    GuildInfo,
    GuildMember,
    GuildRole,
    MemberJoinLeave,
    MentionRecord,
    MessageRecord,
    PingJoinEvent,
    RoleChangeLog,
    ScoreLog,
    Task,
    VoiceActivity,
    Worker,
    db,
)
from ml import burnout as ml_burnout
from ml import engine as ml_engine
from ml import forecast as ml_forecast

dashboard_bp = Blueprint("dashboard", __name__)

PERM_ADMINISTRATOR = 1 << 3
PERM_MANAGE_GUILD = 1 << 5

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "1513743115364597790")
BOT_PERMISSIONS = 1099780156550
BOT_INVITE_URL = f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&permissions={BOT_PERMISSIONS}&scope=bot%20applications.commands"


def require_auth():
    """Redirect to login if not authenticated."""
    if "user" not in session:
        return redirect(url_for("auth.login"))
    return None


def get_accessible_guild_ids():
    """Return list of guild IDs the logged-in user can access (only those the bot is also in)."""
    refresh_accessible_guilds()
    current = session.get("accessible_guilds", [])
    return [g["id"] for g in current if isinstance(g, dict)]


def refresh_accessible_guilds():
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


@dashboard_bp.route("/")
def index(template_name="dashboard.html"):
    user = session.get("user")

    # Public landing page when not logged in
    if not user:
        return render_template(
            "landing.html", user=None, invite_url=BOT_INVITE_URL, logged_out=True
        )

    accessible_ids = get_accessible_guild_ids()

    # Guild selector — ?guild_id scopes all queries to that guild
    selected_guild_id = request.args.get("guild_id")
    if selected_guild_id and selected_guild_id not in accessible_ids:
        selected_guild_id = None

    # When a specific guild is selected, narrow accessible_ids to just that guild
    # so all downstream queries auto-filter. Keep the full list for the dropdown.
    if selected_guild_id:
        guild_filter_ids = [selected_guild_id]
    else:
        guild_filter_ids = accessible_ids

    # Guild-scoped filter for ScoreLog (include legacy rows with NULL guild_id)
    if accessible_ids:
        scorelog_filter = db.or_(
            ScoreLog.guild_id.in_(guild_filter_ids), ScoreLog.guild_id == None
        )
    else:
        scorelog_filter = None

    # Per-guild scores for leaderboard (use eager loading for workers and guild info)
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

    # Batch fetch all workers and guild infos to avoid N+1 queries
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

    # Recent logs with eager loading and guild info
    recent_logs_query = ScoreLog.query
    if scorelog_filter is not None:
        recent_logs_query = recent_logs_query.filter(scorelog_filter)
    recent_logs = (
        recent_logs_query.order_by(ScoreLog.created_at.desc())
        .limit(10)
        .options(selectinload(ScoreLog.worker))
        .all()
    )
    # Filter out orphaned logs (worker deleted)
    recent_logs = [log for log in recent_logs if log.worker]
    # Attach guild name to each log using batch fetched guilds
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
        MessageRecord.guild_id.in_(guild_filter_ids) if guild_filter_ids else None
    )
    voice_guild_filter = (
        VoiceActivity.guild_id.in_(guild_filter_ids) if guild_filter_ids else None
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

    # Hourly activity for charting (aggregate)
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
    # Per-guild hourly activity for comparative chart
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

    # Message volume last 7 days
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

    base_date = datetime.utcnow().date() - timedelta(days=6)
    daily_volume_dates = [
        (base_date + timedelta(days=i)).strftime("%b %d") for i in range(7)
    ]
    daily_volume_counts = [
        daily_volume.get(str(base_date + timedelta(days=i)), 0) for i in range(7)
    ]

    # Score source breakdown (per-guild scoped)
    source_query = db.session.query(
        ScoreLog.source, func.count(ScoreLog.id).label("count")
    )
    if scorelog_filter is not None:
        source_query = source_query.filter(scorelog_filter)
    source_data = source_query.group_by(ScoreLog.source).all()
    score_sources = {s.source: s.count for s in source_data}

    # Community vs staff message breakdown
    community_msg_q = msg_base.filter(MessageRecord.is_public_channel == True)
    staff_msg_q = (
        community_msg_q.filter(
            MessageRecord.discord_id.in_(
                db.session.query(GuildMember.member_id).filter(
                    GuildMember.is_staff == True,
                    GuildMember.is_bot == False,
                    GuildMember.guild_id.in_(guild_filter_ids)
                    if guild_filter_ids
                    else True,
                )
            )
        )
        if accessible_ids
        else community_msg_q.filter(db.false())
    )
    community_messages_total = community_msg_q.count()
    staff_messages_total = staff_msg_q.count()

    # Score source percentages for doughnut chart (actual source keys: discord, system, jira, etc.)
    total_score_events = sum(score_sources.values()) or 1
    source_discord_pct = round(
        score_sources.get("discord", 0) / total_score_events * 100, 1
    )
    source_system_pct = round(
        score_sources.get("system", 0) / total_score_events * 100, 1
    )
    source_jira_pct = round(score_sources.get("jira", 0) / total_score_events * 100, 1)
    source_manual_pct = round(
        score_sources.get("manual", 0) / total_score_events * 100, 1
    )
    source_other_pct = round(
        (
            total_score_events
            - score_sources.get("discord", 0)
            - score_sources.get("system", 0)
            - score_sources.get("jira", 0)
            - score_sources.get("manual", 0)
        )
        / total_score_events
        * 100,
        1,
    )
    # Keep legacy aliases for template compatibility
    source_msg_pct = source_discord_pct
    source_voice_pct = source_system_pct
    source_mod_pct = source_jira_pct
    source_task_pct = source_manual_pct

    # Guild scan overview — only guilds the user can access AND the bot is in
    guilds = (
        GuildInfo.query.filter(GuildInfo.guild_id.in_(guild_filter_ids))
        .order_by(GuildInfo.name)
        .all()
        if guild_filter_ids
        else []
    )
    total_guilds = len(guilds)
    # Live counts from GuildMember (matches guild page data)
    guild_member_count_map = (
        dict(
            db.session.query(GuildMember.guild_id, func.count(GuildMember.id))
            .filter(
                GuildMember.guild_id.in_(guild_filter_ids),
                GuildMember.is_bot == False,
            )
            .group_by(GuildMember.guild_id)
            .all()
        )
        if guild_filter_ids
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
            GuildMember.guild_id.in_(guild_filter_ids),
        ).count()
        if guild_filter_ids
        else 0
    )

    # Behavioral anomalies (last 24h)
    recent_anomalies = BehavioralAnomaly.query.filter(
        BehavioralAnomaly.cleared_at == None,
        BehavioralAnomaly.detected_at > datetime.utcnow() - timedelta(hours=48),
    )
    if guild_filter_ids:
        recent_anomalies = recent_anomalies.filter(
            db.or_(
                BehavioralAnomaly.guild_id == None,
                BehavioralAnomaly.guild_id.in_(guild_filter_ids),
            )
        )
    recent_anomalies = (
        recent_anomalies.order_by(BehavioralAnomaly.severity.desc()).limit(10).all()
    )

    # Burnout risks (#2)
    burnout_risks = BurnoutRisk.query.order_by(BurnoutRisk.score.desc()).limit(5).all()

    # Join/Leave stats for growth analysis
    cutoff = datetime.utcnow() - timedelta(days=7)
    join_leave_query = MemberJoinLeave.query.filter(
        MemberJoinLeave.created_at >= cutoff
    )
    if guild_filter_ids:
        join_leave_query = join_leave_query.filter(
            MemberJoinLeave.guild_id.in_(guild_filter_ids)
        )

    total_joins_7d = join_leave_query.filter(
        MemberJoinLeave.event_type == "join"
    ).count()
    total_leaves_7d = join_leave_query.filter(
        MemberJoinLeave.event_type == "leave"
    ).count()

    # Hourly join/leave breakdown for last 7 days
    hourly_joins_7d = {str(h): 0 for h in range(24)}
    hourly_leaves_7d = {str(h): 0 for h in range(24)}
    hourly_joins_data = db.session.query(
        MemberJoinLeave.hour_of_day, func.count(MemberJoinLeave.id)
    ).filter(
        MemberJoinLeave.created_at >= cutoff,
        MemberJoinLeave.event_type == "join",
        MemberJoinLeave.hour_of_day != None,
    )
    if guild_filter_ids:
        hourly_joins_data = hourly_joins_data.filter(
            MemberJoinLeave.guild_id.in_(guild_filter_ids)
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
    if guild_filter_ids:
        hourly_leaves_data = hourly_leaves_data.filter(
            MemberJoinLeave.guild_id.in_(guild_filter_ids)
        )
    hourly_leaves_data = hourly_leaves_data.group_by(MemberJoinLeave.hour_of_day).all()
    for h, c in hourly_leaves_data:
        hourly_leaves_7d[str(h)] = c

    # Daily join/leave data for charts
    daily_joins_data = db.session.query(
        func.date(MemberJoinLeave.created_at).label("day"),
        func.count(MemberJoinLeave.id),
    ).filter(MemberJoinLeave.created_at >= cutoff, MemberJoinLeave.event_type == "join")
    if guild_filter_ids:
        daily_joins_data = daily_joins_data.filter(
            MemberJoinLeave.guild_id.in_(guild_filter_ids)
        )
    daily_joins_dict = {
        str(d[0]): d[1]
        for d in daily_joins_data.group_by(func.date(MemberJoinLeave.created_at)).all()
    }

    daily_leaves_data = db.session.query(
        func.date(MemberJoinLeave.created_at).label("day"),
        func.count(MemberJoinLeave.id),
    ).filter(
        MemberJoinLeave.created_at >= cutoff, MemberJoinLeave.event_type == "leave"
    )
    if guild_filter_ids:
        daily_leaves_data = daily_leaves_data.filter(
            MemberJoinLeave.guild_id.in_(guild_filter_ids)
        )
    daily_leaves_dict = {
        str(d[0]): d[1]
        for d in daily_leaves_data.group_by(func.date(MemberJoinLeave.created_at)).all()
    }

    daily_growth_dates = daily_volume_dates
    daily_joins = [
        daily_joins_dict.get(str(base_date + timedelta(days=i)), 0) for i in range(7)
    ]
    daily_leaves = [
        daily_leaves_dict.get(str(base_date + timedelta(days=i)), 0) for i in range(7)
    ]

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

    # Pre-compute federated history for template (no Jinja multiply filter)
    if isinstance(ml_status, dict):
        fed = ml_status.get("federated", {})
        if fed and fed.get("history"):
            fed["history_accuracy_pct"] = [
                round(h.get("mean_global_accuracy", 0) * 100, 1) for h in fed["history"]
            ]
            fed["history_rounds"] = [
                h.get("round", i + 1) for i, h in enumerate(fed["history"])
            ]

    # Detect empty state for guild-scoped view (brand new guild, no data yet)
    is_empty = bool(selected_guild_id) and (
        total_messages_logged == 0
        and total_workers == 0
        and total_moderation_actions == 0
        and total_voice_sessions == 0
    )

    return render_template(
        template_name,
        user=user,
        selected_guild_id=selected_guild_id,
        accessible_guilds=session.get("accessible_guilds", []),
        invite_url=BOT_INVITE_URL,
        logged_out=False,
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
        daily_volume_dates=daily_volume_dates,
        daily_volume_counts=daily_volume_counts,
        daily_growth_dates=daily_growth_dates,
        daily_joins=daily_joins,
        daily_leaves=daily_leaves,
        score_sources=score_sources,
        guild_hourly=guild_hourly,
        guild_msg_counts=guild_msg_counts,
        guild_name_map={g.guild_id: g.name for g in guilds},
        guild_names_map={g.guild_id: g.name for g in guilds},
        community_messages_total=community_messages_total,
        staff_messages_total=staff_messages_total,
        source_msg_pct=source_msg_pct,
        source_voice_pct=source_voice_pct,
        source_mod_pct=source_mod_pct,
        source_task_pct=source_task_pct,
        source_manual_pct=source_manual_pct,
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
        is_empty=is_empty,
    )


@dashboard_bp.route("/_live")
def dashboard_live():
    auth = require_auth()
    if auth:
        return jsonify({"error": "unauthorized"}), 401
    accessible_ids = get_accessible_guild_ids()
    if not accessible_ids:
        return jsonify(
            {
                "total_online_members": 0,
                "total_members_tracked": 0,
                "total_online_pct": 0,
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
    total_online = sum(guild_online_map.values())
    total_members = sum(guild_member_count_map.values())
    total_online_pct = (
        round((total_online / total_members) * 100, 1) if total_members else 0
    )
    return jsonify(
        {
            "total_online_members": total_online,
            "total_members_tracked": total_members,
            "total_online_pct": total_online_pct,
            "guild_online_map": guild_online_map,
            "guild_member_count_map": guild_member_count_map,
        }
    )


@dashboard_bp.route("/worker/<int:worker_id>")
def worker_detail(worker_id):
    redirect_resp = require_auth()
    if redirect_resp:
        return redirect_resp

    worker = Worker.query.get_or_404(worker_id)
    worker.score = (
        db.session.query(func.sum(ScoreLog.change))
        .filter(ScoreLog.worker_id == worker_id)
        .scalar()
        or 0.0
    )
    logs = (
        ScoreLog.query.filter_by(worker_id=worker_id)
        .order_by(ScoreLog.created_at.desc())
        .all()
    )
    tasks = (
        Task.query.filter_by(worker_id=worker_id)
        .order_by(Task.assigned_at.desc())
        .all()
    )

    # Behavioral data for this worker (if they have a discord_id)
    behavior = None
    anomalies = []
    worker_hourly_activity = None
    cumulative_score_data = None
    mention_stats = None
    voice_stats = None
    activity_consistency = None
    mod_quality = None
    if worker.discord_id:
        stats = MessageRecord.query.filter_by(discord_id=worker.discord_id)
        total = stats.count()
        avg_len = (
            db.session.query(func.avg(MessageRecord.message_length))
            .filter_by(discord_id=worker.discord_id)
            .scalar()
        )
        channels = (
            db.session.query(
                MessageRecord.channel_name, func.count(MessageRecord.id).label("c")
            )
            .filter_by(discord_id=worker.discord_id)
            .group_by(MessageRecord.channel_name)
            .order_by(func.count(MessageRecord.id).desc())
            .all()
        )
        behavior = {
            "total_messages": total,
            "avg_length": round(avg_len or 0, 1),
            "channels": {c.channel_name: c.c for c in channels},
        }
        anomalies = (
            BehavioralAnomaly.query.filter_by(
                discord_id=worker.discord_id, cleared_at=None
            )
            .order_by(BehavioralAnomaly.severity.desc())
            .all()
        )

        # Worker hourly activity for charting
        wh_data = (
            db.session.query(
                MessageRecord.hour_of_day, func.count(MessageRecord.id).label("count")
            )
            .filter(MessageRecord.discord_id == worker.discord_id)
            .group_by(MessageRecord.hour_of_day)
            .order_by(MessageRecord.hour_of_day)
            .all()
        )
        worker_hourly_activity = {str(h): 0 for h in range(24)}
        for h, c in wh_data:
            worker_hourly_activity[str(h)] = c

        # Cumulative score over time for line chart
        cum_logs = (
            ScoreLog.query.filter_by(worker_id=worker_id)
            .order_by(ScoreLog.created_at.asc())
            .all()
        )
        running = 0
        cum_data = []
        for log in cum_logs:
            running += log.change
            cum_data.append(
                {
                    "date": log.created_at.strftime("%b %d"),
                    "score": running,
                    "change": log.change,
                }
            )
        cumulative_score_data = cum_data

        # Mention analytics
        mentions_received = MentionRecord.query.filter_by(
            mentioned_id=worker.discord_id
        ).count()
        mentions_sent = MentionRecord.query.filter_by(
            mentioner_id=worker.discord_id
        ).count()
        avg_reply = (
            db.session.query(func.avg(MentionRecord.reply_time_seconds))
            .filter(
                MentionRecord.mentioned_id == worker.discord_id,
                MentionRecord.reply_time_seconds != None,
            )
            .scalar()
        )
        mention_stats = {
            "received": mentions_received,
            "sent": mentions_sent,
            "avg_reply_seconds": round(avg_reply or 0, 1),
        }

        # Voice activity analytics
        voice = VoiceActivity.query.filter_by(discord_id=worker.discord_id)
        total_voice_sessions = voice.count()
        total_voice_time = (
            db.session.query(func.sum(VoiceActivity.duration_seconds))
            .filter_by(discord_id=worker.discord_id)
            .scalar()
            or 0
        )
        avg_voice_duration = (
            db.session.query(func.avg(VoiceActivity.duration_seconds))
            .filter_by(discord_id=worker.discord_id)
            .scalar()
            or 0
        )
        voice_channels = (
            db.session.query(
                VoiceActivity.channel_name,
                func.count(VoiceActivity.id).label("c"),
                func.sum(VoiceActivity.duration_seconds).label("total"),
            )
            .filter_by(discord_id=worker.discord_id)
            .group_by(VoiceActivity.channel_name)
            .order_by(func.sum(VoiceActivity.duration_seconds).desc())
            .all()
        )
        voice_top_channels = [
            {
                "name": v.channel_name,
                "sessions": v.c,
                "total_seconds": round(v.total or 0, 1),
            }
            for v in voice_channels[:5]
        ]
        voice_hourly_data = (
            db.session.query(
                VoiceActivity.hour_of_day, func.count(VoiceActivity.id).label("count")
            )
            .filter_by(discord_id=worker.discord_id)
            .filter(VoiceActivity.hour_of_day != None)
            .group_by(VoiceActivity.hour_of_day)
            .order_by(VoiceActivity.hour_of_day)
            .all()
        )
        voice_hourly = {str(h): 0 for h in range(24)}
        for h, c in voice_hourly_data:
            voice_hourly[str(h)] = c
        voice_stats = {
            "total_sessions": total_voice_sessions,
            "total_hours": round(total_voice_time / 3600, 1),
            "avg_minutes": round(avg_voice_duration / 60, 1),
            "top_channels": voice_top_channels,
            "hourly": voice_hourly,
        }

        # Activity Consistency & Trend (#1)
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        daily_counts = (
            db.session.query(
                func.date(MessageRecord.created_at).label("day"),
                func.count(MessageRecord.id).label("c"),
            )
            .filter(
                MessageRecord.discord_id == worker.discord_id,
                MessageRecord.created_at >= thirty_days_ago,
            )
            .group_by(func.date(MessageRecord.created_at))
            .order_by(func.date(MessageRecord.created_at))
            .all()
        )
        daily_vals = [d.c for d in daily_counts]

        consistency_score = 0
        trend = "flat"
        off_hours_ratio = 0
        recent_total = 0
        prior_total = 0

        if daily_vals:
            mean = sum(daily_vals) / len(daily_vals)
            if mean > 0:
                stdev = statistics.pstdev(daily_vals)
                cv = stdev / mean
                consistency_score = round(max(0, min(100, (1 - cv) * 100)), 1)

            # Trend: compare last 7 days vs prior 7 days
            seven_days_ago = datetime.utcnow() - timedelta(days=7)
            fourteen_days_ago = datetime.utcnow() - timedelta(days=14)
            recent = (
                db.session.query(func.count(MessageRecord.id))
                .filter(
                    MessageRecord.discord_id == worker.discord_id,
                    MessageRecord.created_at >= seven_days_ago,
                )
                .scalar()
                or 0
            )
            prior = (
                db.session.query(func.count(MessageRecord.id))
                .filter(
                    MessageRecord.discord_id == worker.discord_id,
                    MessageRecord.created_at >= fourteen_days_ago,
                    MessageRecord.created_at < seven_days_ago,
                )
                .scalar()
                or 0
            )
            recent_total = recent
            prior_total = prior
            if prior > 0:
                change_pct = (recent - prior) / prior * 100
                if change_pct > 20:
                    trend = "up"
                elif change_pct < -20:
                    trend = "down"
                else:
                    trend = "flat"

            # Off-hours ratio (outside 09:00-17:00)
            total_msgs = recent + prior
            off_hours = (
                db.session.query(func.count(MessageRecord.id))
                .filter(
                    MessageRecord.discord_id == worker.discord_id,
                    MessageRecord.created_at >= fourteen_days_ago,
                    ~MessageRecord.hour_of_day.between(9, 16),
                )
                .scalar()
                or 0
            )
            off_hours_ratio = (
                round(off_hours / total_msgs * 100, 1) if total_msgs > 0 else 0
            )

        activity_consistency = {
            "score": consistency_score,
            "trend": trend,
            "off_hours_ratio": off_hours_ratio,
            "recent_total": recent_total,
            "prior_total": prior_total,
        }

        # Moderation Quality Score (#4)
        mod_logs = ScoreLog.query.filter_by(worker_id=worker_id, source="discord").all()
        total_actions = len(mod_logs)
        total_warns = sum(1 for l in mod_logs if "warn" in l.reason.lower())
        total_bans = sum(1 for l in mod_logs if "ban" in l.reason.lower())
        total_kicks = sum(1 for l in mod_logs if "kick" in l.reason.lower())
        total_timeouts = sum(1 for l in mod_logs if "timeout" in l.reason.lower())
        punitive_actions = total_bans + total_kicks + total_timeouts

        reversal_count = (
            ScoreLog.query.filter_by(worker_id=worker_id, source="discord")
            .filter(ScoreLog.change < 0, ScoreLog.reason.ilike("%reversal%"))
            .count()
        )

        action_warn_ratio = (
            round(punitive_actions / max(total_warns, 1), 1) if total_warns > 0 else 0
        )
        reversal_rate = round(reversal_count / max(total_actions, 1) * 100, 1)
        q_score = 100
        if total_actions > 0:
            q_score -= reversal_rate * 2
            q_score = max(0, min(100, round(q_score, 1)))

        mod_quality = {
            "total_actions": total_actions,
            "total_warns": total_warns,
            "action_warn_ratio": action_warn_ratio,
            "reversal_rate": reversal_rate,
            "quality_score": q_score,
        }

    # Role change history
    role_changes = []
    if worker.discord_id:
        role_changes = (
            RoleChangeLog.query.filter_by(member_id=worker.discord_id)
            .order_by(RoleChangeLog.created_at.desc())
            .limit(20)
            .all()
        )

    return render_template(
        "worker.html",
        user=session.get("user"),
        accessible_guilds=session.get("accessible_guilds", []),
        worker=worker,
        logs=logs,
        tasks=tasks,
        behavior=behavior,
        anomalies=anomalies,
        mention_stats=mention_stats,
        voice_stats=voice_stats,
        worker_hourly_activity=worker_hourly_activity,
        cumulative_score_data=cumulative_score_data,
        activity_consistency=activity_consistency,
        mod_quality=mod_quality,
        role_changes=role_changes,
    )


@dashboard_bp.route("/guild/<guild_id>")
def guild_detail(guild_id):
    redirect_resp = require_auth()
    if redirect_resp:
        return redirect_resp

    accessible_ids = get_accessible_guild_ids()
    if guild_id not in accessible_ids:
        return redirect(url_for("dashboard.index"))

    guild = GuildInfo.query.filter_by(guild_id=guild_id).first_or_404()
    roles = (
        db.session.query(GuildRole)
        .filter_by(guild_id=guild_id)
        .order_by(GuildRole.position.desc())
        .all()
    )
    members = (
        db.session.query(GuildMember)
        .filter_by(guild_id=guild_id, is_bot=False)
        .order_by(GuildMember.top_role_position.desc(), GuildMember.name)
        .all()
    )
    staff = [m for m in members if m.is_staff]
    non_staff = [m for m in members if not m.is_staff]
    automod_rules = (
        AutoModRule.query.filter_by(guild_id=guild_id, enabled=True)
        .order_by(AutoModRule.created_at.desc())
        .all()
    )
    ping_events = (
        PingJoinEvent.query.filter_by(guild_id=guild_id)
        .order_by(PingJoinEvent.created_at.desc())
        .limit(20)
        .all()
    )

    # Per-guild hourly activity for guild detail chart
    gh_data = (
        db.session.query(
            MessageRecord.hour_of_day, func.count(MessageRecord.id).label("count")
        )
        .filter(MessageRecord.guild_id == guild_id)
        .group_by(MessageRecord.hour_of_day)
        .order_by(MessageRecord.hour_of_day)
        .all()
    )
    guild_hourly_chart = {str(h): 0 for h in range(24)}
    for h, c in gh_data:
        guild_hourly_chart[str(h)] = c
    guild_msg_total = MessageRecord.query.filter_by(guild_id=guild_id).count()

    # Per-guild voice stats
    guild_voice_sessions = VoiceActivity.query.filter_by(guild_id=guild_id).count()
    guild_voice_hours = round(
        (
            db.session.query(func.sum(VoiceActivity.duration_seconds))
            .filter_by(guild_id=guild_id)
            .scalar()
            or 0
        )
        / 3600,
        1,
    )
    guild_voice_channels = (
        db.session.query(
            VoiceActivity.channel_name,
            func.count(VoiceActivity.id).label("c"),
            func.sum(VoiceActivity.duration_seconds).label("total"),
        )
        .filter_by(guild_id=guild_id)
        .group_by(VoiceActivity.channel_name)
        .order_by(func.sum(VoiceActivity.duration_seconds).desc())
        .limit(5)
        .all()
    )
    guild_voice_top_channels = [
        {
            "name": v.channel_name,
            "sessions": v.c,
            "hours": round((v.total or 0) / 3600, 1),
        }
        for v in guild_voice_channels
    ]
    guild_voice_hourly_raw = (
        db.session.query(
            VoiceActivity.hour_of_day, func.count(VoiceActivity.id).label("count")
        )
        .filter_by(guild_id=guild_id)
        .filter(VoiceActivity.hour_of_day != None)
        .group_by(VoiceActivity.hour_of_day)
        .order_by(VoiceActivity.hour_of_day)
        .all()
    )
    guild_voice_hourly = {str(h): 0 for h in range(24)}
    for h, c in guild_voice_hourly_raw:
        guild_voice_hourly[str(h)] = c
    # Voice users per guild
    guild_voice_users = (
        db.session.query(
            VoiceActivity.name,
            VoiceActivity.discord_id,
            func.count(VoiceActivity.id).label("sessions"),
            func.sum(VoiceActivity.duration_seconds).label("total_time"),
        )
        .filter_by(guild_id=guild_id)
        .group_by(VoiceActivity.name, VoiceActivity.discord_id)
        .order_by(func.sum(VoiceActivity.duration_seconds).desc())
        .limit(20)
        .all()
    )
    guild_voice_users_list = [
        {
            "name": u.name,
            "discord_id": u.discord_id,
            "sessions": u.sessions,
            "hours": round((u.total_time or 0) / 3600, 1),
        }
        for u in guild_voice_users
    ]

    # Peak Hour Staffing Overlay (#3) — community vs staff hourly activity
    staff_ids = [m.member_id for m in staff]
    community_hourly = {str(h): 0 for h in range(24)}
    staff_hourly = {str(h): 0 for h in range(24)}
    if staff_ids:
        for is_staff_list, target in [
            (staff_ids, staff_hourly),
            (None, community_hourly),
        ]:
            q = db.session.query(
                MessageRecord.hour_of_day, func.count(MessageRecord.id).label("count")
            ).filter(MessageRecord.guild_id == guild_id)
            if is_staff_list:
                q = q.filter(MessageRecord.discord_id.in_(staff_ids))
            else:
                q = (
                    q.filter(~MessageRecord.discord_id.in_(staff_ids))
                    if staff_ids
                    else q
                )
            for h, c in (
                q.group_by(MessageRecord.hour_of_day)
                .order_by(MessageRecord.hour_of_day)
                .all()
            ):
                target[str(h)] = c

    # Chart data using actual DB counts
    guild_info = GuildInfo.query.filter_by(guild_id=guild_id).first()
    online_count = (
        guild_info.online_count
        if guild_info
        else GuildMember.query.filter_by(
            guild_id=guild_id, is_bot=False, is_online=True
        ).count()
    )
    tracked_chatted = (
        GuildMember.query.filter_by(guild_id=guild_id, is_bot=False)
        .filter(GuildMember.last_message_at != None)
        .count()
    )
    tracked_offline = (
        GuildMember.query.filter_by(guild_id=guild_id, is_bot=False, is_online=False)
        .filter(GuildMember.last_message_at == None)
        .count()
    )
    community_count = GuildMember.query.filter_by(
        guild_id=guild_id, is_bot=False, is_staff=False
    ).count()
    human_count = GuildMember.query.filter_by(guild_id=guild_id, is_bot=False).count()
    bot_count = GuildMember.query.filter_by(guild_id=guild_id, is_bot=True).count()

    # AutoMod triggers
    automod_triggers = (
        AutoModTrigger.query.filter_by(guild_id=guild_id)
        .order_by(AutoModTrigger.created_at.desc())
        .limit(30)
        .all()
    )

    # ML forecast for this guild (read-only — does not log prediction)
    forecast_preds = ml_forecast.predict_next_24h(guild_id, log_prediction=False)
    forecast_data = None
    if forecast_preds is not None:
        forecast_data = forecast_preds
    # Guild-specific all-time daily volume accuracy
    forecast_metrics = ml_forecast.get_accuracy_metrics(guild_id=guild_id, days=None)

    # 30-day hourly average for forecast comparison chart
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    hourly_rows = (
        db.session.query(
            MessageRecord.hour_of_day, func.count(MessageRecord.id).label("cnt")
        )
        .filter(
            MessageRecord.guild_id == guild_id,
            MessageRecord.created_at >= thirty_days_ago,
            MessageRecord.hour_of_day != None,
        )
        .group_by(MessageRecord.hour_of_day)
        .all()
    )
    guild_hourly_avg_30d = {str(h): 0.0 for h in range(24)}
    for h, cnt in hourly_rows:
        guild_hourly_avg_30d[str(h)] = round(cnt / 30, 1)

    # Behavioral anomalies for this guild
    guild_anomalies = (
        BehavioralAnomaly.query.filter(
            BehavioralAnomaly.cleared_at == None,
            BehavioralAnomaly.detected_at > datetime.utcnow() - timedelta(hours=48),
            db.or_(
                BehavioralAnomaly.guild_id == None,
                BehavioralAnomaly.guild_id == guild_id,
            ),
        )
        .order_by(BehavioralAnomaly.severity.desc())
        .limit(10)
        .all()
    )

    # Role change history for this guild
    role_changes = (
        RoleChangeLog.query.filter_by(guild_id=guild_id)
        .order_by(RoleChangeLog.created_at.desc())
        .limit(30)
        .all()
    )

    return render_template(
        "guild.html",
        user=session.get("user"),
        accessible_guilds=session.get("accessible_guilds", []),
        guild=guild,
        roles=roles,
        staff=staff,
        members=non_staff,
        automod_rules=automod_rules,
        automod_triggers=automod_triggers,
        ping_events=ping_events,
        guild_hourly_chart=guild_hourly_chart,
        guild_msg_total=guild_msg_total,
        guild_voice_sessions=guild_voice_sessions,
        guild_voice_hours=guild_voice_hours,
        guild_voice_top_channels=guild_voice_top_channels,
        guild_voice_hourly=guild_voice_hourly,
        guild_voice_users=guild_voice_users_list,
        community_hourly=community_hourly,
        staff_hourly=staff_hourly,
        online_count=online_count,
        tracked_offline=tracked_offline,
        tracked_chatted=tracked_chatted,
        community_count=community_count,
        human_count=human_count,
        bot_count=bot_count,
        forecast_data=forecast_data,
        forecast_metrics=forecast_metrics,
        guild_hourly_avg_30d=guild_hourly_avg_30d,
        guild_anomalies=guild_anomalies,
        role_changes=role_changes,
    )


# Dashboard ML proxy endpoints (session-authenticated)
@dashboard_bp.route("/dashboard/ml/anomaly-feedback", methods=["POST"])
def dashboard_ml_anomaly_feedback():
    """Proxy endpoint for admin feedback on anomaly predictions."""
    redirect_resp = require_auth()
    if redirect_resp:
        return redirect_resp

    data = request.json or {}
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    # Validate required fields
    if not data.get("anomaly_id") or not data.get("feedback"):
        return jsonify({"error": "Missing required fields"}), 400

    feedback = data["feedback"]
    if feedback not in ("confirmed", "dismissed"):
        return jsonify({"error": 'feedback must be "confirmed" or "dismissed"'}), 400

    # Get the anomaly
    anomaly = BehavioralAnomaly.query.get_or_404(int(data["anomaly_id"]))

    # Check if the anomaly belongs to a guild the user can access
    accessible_ids = get_accessible_guild_ids()
    if anomaly.guild_id and accessible_ids and anomaly.guild_id not in accessible_ids:
        return jsonify({"error": "Unauthorized"}), 403

    # Update the anomaly
    anomaly.feedback = feedback
    anomaly.feedback_at = datetime.utcnow()
    db.session.commit()

    return jsonify({"status": "ok", "feedback": feedback, "anomaly_id": anomaly.id})


@dashboard_bp.route("/dashboard/ml/burnout-feedback", methods=["POST"])
def dashboard_ml_burnout_feedback():
    """Proxy endpoint for admin feedback on burnout predictions."""
    redirect_resp = require_auth()
    if redirect_resp:
        return redirect_resp

    data = request.json or {}
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    # Validate required fields
    if not data.get("risk_id") or not data.get("feedback"):
        return jsonify({"error": "Missing required fields"}), 400

    feedback = data["feedback"]
    if feedback not in ("confirmed", "dismissed"):
        return jsonify({"error": 'feedback must be "confirmed" or "dismissed"'}), 400

    # Get the burnout risk
    from database import BurnoutRisk

    risk = BurnoutRisk.query.get_or_404(int(data["risk_id"]))

    # Update the burnout risk
    risk.feedback = feedback
    risk.feedback_at = datetime.utcnow()
    db.session.commit()

    return jsonify({"status": "ok", "feedback": feedback, "risk_id": risk.id})


@dashboard_bp.route("/dashboard/ml/retrain", methods=["POST"])
def dashboard_ml_retrain():
    """Proxy endpoint for ML model retraining."""
    redirect_resp = require_auth()
    if redirect_resp:
        return redirect_resp

    data = request.json or {}
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    # Validate required fields
    if not data.get("trigger"):
        return jsonify({"error": "Missing required field: trigger"}), 400

    # Use the ML engine to retrain
    try:
        result = ml_engine.train_all(days=data.get("days", 30))
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"error": f"Retrain failed: {str(e)}"}), 500


@dashboard_bp.route("/dashboard/ml/federated-train", methods=["POST"])
def dashboard_ml_federated_train():
    """Proxy endpoint for federated learning training."""
    redirect_resp = require_auth()
    if redirect_resp:
        return redirect_resp

    data = request.json or {}
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    # Validate required fields
    if not data.get("days"):
        return jsonify({"error": "Missing required field: days"}), 400

    # Use the ML federated module to train
    try:
        from ml import federated

        result = federated.train_federated(days=data.get("days", 30))
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"error": f"Federated training failed: {str(e)}"}), 500
