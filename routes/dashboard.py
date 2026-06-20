from flask import Blueprint, render_template, session, redirect, url_for, request
from database import db, Worker, Task, ScoreLog, AdminCorrection, MessageRecord, GuildInfo, GuildRole, GuildMember, BehavioralAnomaly, MentionRecord, AutoModRule, PingJoinEvent, VoiceActivity
from datetime import datetime, timedelta
from sqlalchemy import func

dashboard_bp = Blueprint('dashboard', __name__)

CLIENT_ID = '1513743115364597790'
BOT_PERMISSIONS = 1099780156550
BOT_INVITE_URL = f'https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&permissions={BOT_PERMISSIONS}&scope=bot%20applications.commands'


def require_auth():
    """Redirect to login if not authenticated."""
    if 'user' not in session:
        return redirect(url_for('auth.login'))
    return None


def get_accessible_guild_ids():
    """Return list of guild IDs the logged-in user can access (only those the bot is also in)."""
    return [g['id'] for g in session.get('accessible_guilds', [])]


@dashboard_bp.route('/')
def index():
    user = session.get('user')

    # Public landing page when not logged in
    if not user:
        return render_template('dashboard.html',
            user=None, accessible_guilds=[],
            invite_url=BOT_INVITE_URL, logged_out=True)

    accessible_ids = get_accessible_guild_ids()
    workers = Worker.query.order_by(Worker.score.desc()).all()
    total_workers = len(workers)
    total_tasks = Task.query.count()
    total_corrections = AdminCorrection.query.count()
    total_moderation_actions = ScoreLog.query.filter_by(source='discord').count()
    recent_logs = ScoreLog.query.order_by(ScoreLog.created_at.desc()).limit(10).all()

    # Behavioral analytics
    total_messages_logged = MessageRecord.query.count()
    unique_users_tracked = db.session.query(MessageRecord.discord_id).distinct().count()
    total_voice_sessions = VoiceActivity.query.count()
    total_voice_hours = round((db.session.query(func.sum(VoiceActivity.duration_seconds)).scalar() or 0) / 3600, 1)
    most_active = db.session.query(
        MessageRecord.name, MessageRecord.discord_id, func.count(MessageRecord.id).label('count')
    ).group_by(MessageRecord.name, MessageRecord.discord_id).order_by(func.count(MessageRecord.id).desc()).limit(5).all()

    # Hourly activity for charting (aggregate)
    hourly_data = db.session.query(
        MessageRecord.hour_of_day, func.count(MessageRecord.id).label('count')
    ).group_by(MessageRecord.hour_of_day).order_by(MessageRecord.hour_of_day).all()
    hourly_activity = {str(h): 0 for h in range(24)}
    for h, c in hourly_data:
        hourly_activity[str(h)] = c

    # Per-guild hourly activity for comparative chart
    guild_hourly_raw = db.session.query(
        MessageRecord.guild_id, MessageRecord.hour_of_day, func.count(MessageRecord.id).label('count')
    ).filter(MessageRecord.guild_id.in_(accessible_ids)).group_by(MessageRecord.guild_id, MessageRecord.hour_of_day).order_by(MessageRecord.guild_id, MessageRecord.hour_of_day).all() if accessible_ids else []
    guild_hourly = {}
    for guild_id, hour, count in guild_hourly_raw:
        if guild_id not in guild_hourly:
            guild_hourly[guild_id] = {str(h): 0 for h in range(24)}
        guild_hourly[guild_id][str(hour)] = count

    # Per-guild message totals
    guild_msg_counts_raw = db.session.query(
        MessageRecord.guild_id, func.count(MessageRecord.id).label('count')
    ).filter(MessageRecord.guild_id.in_(accessible_ids)).group_by(MessageRecord.guild_id).all() if accessible_ids else []
    guild_msg_counts = {g: c for g, c in guild_msg_counts_raw}

    # Message volume last 7 days
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    daily_vol = db.session.query(
        func.date(MessageRecord.created_at).label('day'), func.count(MessageRecord.id).label('count')
    ).filter(MessageRecord.created_at >= seven_days_ago).group_by(func.date(MessageRecord.created_at)).order_by(func.date(MessageRecord.created_at)).all()
    daily_volume = {str(d.day): d.count for d in daily_vol}

    # Score source breakdown
    source_data = db.session.query(
        ScoreLog.source, func.count(ScoreLog.id).label('count')
    ).group_by(ScoreLog.source).all()
    score_sources = {s.source: s.count for s in source_data}

    # Guild scan overview — only guilds the user can access AND the bot is in
    guilds = GuildInfo.query.filter(GuildInfo.guild_id.in_(accessible_ids)).order_by(GuildInfo.name).all() if accessible_ids else []
    total_guilds = len(guilds)
    total_members_tracked = sum(g.member_count for g in guilds)
    total_online_members = sum(g.online_count for g in guilds)
    total_staff_tracked = GuildMember.query.filter(
        GuildMember.is_staff == True,
        GuildMember.is_bot == False,
        GuildMember.guild_id.in_(accessible_ids)
    ).count() if accessible_ids else 0

    # Behavioral anomalies (last 24h)
    recent_anomalies = BehavioralAnomaly.query.filter(
        BehavioralAnomaly.cleared_at == None,
        BehavioralAnomaly.detected_at > datetime.utcnow() - timedelta(hours=24)
    ).order_by(BehavioralAnomaly.severity.desc()).limit(10).all() if accessible_ids else []

    return render_template('dashboard.html',
        user=user,
        accessible_guilds=session.get('accessible_guilds', []),
        invite_url=BOT_INVITE_URL, logged_out=False,
        workers=workers,
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
    )

@dashboard_bp.route('/worker/<int:worker_id>')
def worker_detail(worker_id):
    redirect_resp = require_auth()
    if redirect_resp:
        return redirect_resp

    worker = Worker.query.get_or_404(worker_id)
    logs = ScoreLog.query.filter_by(worker_id=worker_id).order_by(ScoreLog.created_at.desc()).all()
    tasks = Task.query.filter_by(worker_id=worker_id).order_by(Task.assigned_at.desc()).all()

    # Behavioral data for this worker (if they have a discord_id)
    behavior = None
    anomalies = []
    worker_hourly_activity = None
    cumulative_score_data = None
    if worker.discord_id:
        stats = MessageRecord.query.filter_by(discord_id=worker.discord_id)
        total = stats.count()
        avg_len = db.session.query(func.avg(MessageRecord.message_length)).filter_by(discord_id=worker.discord_id).scalar()
        channels = db.session.query(MessageRecord.channel_name, func.count(MessageRecord.id).label('c')).filter_by(discord_id=worker.discord_id).group_by(MessageRecord.channel_name).order_by(func.count(MessageRecord.id).desc()).all()
        behavior = {
            'total_messages': total,
            'avg_length': round(avg_len or 0, 1),
            'channels': {c.channel_name: c.c for c in channels},
        }
        anomalies = BehavioralAnomaly.query.filter_by(discord_id=worker.discord_id, cleared_at=None).order_by(BehavioralAnomaly.severity.desc()).all()

        # Worker hourly activity for charting
        wh_data = db.session.query(
            MessageRecord.hour_of_day, func.count(MessageRecord.id).label('count')
        ).filter(MessageRecord.discord_id == worker.discord_id).group_by(MessageRecord.hour_of_day).order_by(MessageRecord.hour_of_day).all()
        worker_hourly_activity = {str(h): 0 for h in range(24)}
        for h, c in wh_data:
            worker_hourly_activity[str(h)] = c

        # Cumulative score over time for line chart
        cum_logs = ScoreLog.query.filter_by(worker_id=worker_id).order_by(ScoreLog.created_at.asc()).all()
        running = 0
        cum_data = []
        for log in cum_logs:
            running += log.change
            cum_data.append({'date': log.created_at.strftime('%b %d'), 'score': running, 'change': log.change})
        cumulative_score_data = cum_data

        # Mention analytics
        mentions_received = MentionRecord.query.filter_by(mentioned_id=worker.discord_id).count()
        mentions_sent = MentionRecord.query.filter_by(mentioner_id=worker.discord_id).count()
        avg_reply = db.session.query(func.avg(MentionRecord.reply_time_seconds)).filter(
            MentionRecord.mentioned_id == worker.discord_id,
            MentionRecord.reply_time_seconds != None
        ).scalar()
        mention_stats = {
            'received': mentions_received,
            'sent': mentions_sent,
            'avg_reply_seconds': round(avg_reply or 0, 1),
        }

        # Voice activity analytics
        voice = VoiceActivity.query.filter_by(discord_id=worker.discord_id)
        total_voice_sessions = voice.count()
        total_voice_time = db.session.query(func.sum(VoiceActivity.duration_seconds)).filter_by(discord_id=worker.discord_id).scalar() or 0
        avg_voice_duration = db.session.query(func.avg(VoiceActivity.duration_seconds)).filter_by(discord_id=worker.discord_id).scalar() or 0
        voice_channels = db.session.query(VoiceActivity.channel_name, func.count(VoiceActivity.id).label('c'), func.sum(VoiceActivity.duration_seconds).label('total')).filter_by(discord_id=worker.discord_id).group_by(VoiceActivity.channel_name).order_by(func.sum(VoiceActivity.duration_seconds).desc()).all()
        voice_top_channels = [{'name': v.channel_name, 'sessions': v.c, 'total_seconds': round(v.total or 0, 1)} for v in voice_channels[:5]]
        voice_hourly_data = db.session.query(VoiceActivity.hour_of_day, func.count(VoiceActivity.id).label('count')).filter_by(discord_id=worker.discord_id).filter(VoiceActivity.hour_of_day != None).group_by(VoiceActivity.hour_of_day).order_by(VoiceActivity.hour_of_day).all()
        voice_hourly = {str(h): 0 for h in range(24)}
        for h, c in voice_hourly_data:
            voice_hourly[str(h)] = c
        voice_stats = {
            'total_sessions': total_voice_sessions,
            'total_hours': round(total_voice_time / 3600, 1),
            'avg_minutes': round(avg_voice_duration / 60, 1),
            'top_channels': voice_top_channels,
            'hourly': voice_hourly,
        }
    else:
        mention_stats = None
        voice_stats = None

    return render_template('worker.html',
        user=session.get('user'),
        accessible_guilds=session.get('accessible_guilds', []),
        worker=worker, logs=logs, tasks=tasks, behavior=behavior, anomalies=anomalies, mention_stats=mention_stats,
        voice_stats=voice_stats,
        worker_hourly_activity=worker_hourly_activity, cumulative_score_data=cumulative_score_data)


@dashboard_bp.route('/guild/<guild_id>')
def guild_detail(guild_id):
    redirect_resp = require_auth()
    if redirect_resp:
        return redirect_resp

    accessible_ids = get_accessible_guild_ids()
    if guild_id not in accessible_ids:
        return redirect(url_for('dashboard.index'))

    guild = GuildInfo.query.filter_by(guild_id=guild_id).first_or_404()
    roles = db.session.query(GuildRole).filter_by(guild_id=guild_id).order_by(GuildRole.position.desc()).all()
    members = db.session.query(GuildMember).filter_by(guild_id=guild_id, is_bot=False).order_by(GuildMember.top_role_position.desc(), GuildMember.name).all()
    staff = [m for m in members if m.is_staff]
    non_staff = [m for m in members if not m.is_staff]
    automod_rules = AutoModRule.query.filter_by(guild_id=guild_id, enabled=True).order_by(AutoModRule.created_at.desc()).all()
    ping_events = PingJoinEvent.query.filter_by(guild_id=guild_id).order_by(PingJoinEvent.created_at.desc()).limit(20).all()

    # Per-guild hourly activity for guild detail chart
    gh_data = db.session.query(
        MessageRecord.hour_of_day, func.count(MessageRecord.id).label('count')
    ).filter(MessageRecord.guild_id == guild_id).group_by(MessageRecord.hour_of_day).order_by(MessageRecord.hour_of_day).all()
    guild_hourly_chart = {str(h): 0 for h in range(24)}
    for h, c in gh_data:
        guild_hourly_chart[str(h)] = c
    guild_msg_total = MessageRecord.query.filter_by(guild_id=guild_id).count()

    # Per-guild voice stats
    guild_voice_sessions = VoiceActivity.query.filter_by(guild_id=guild_id).count()
    guild_voice_hours = round((db.session.query(func.sum(VoiceActivity.duration_seconds)).filter_by(guild_id=guild_id).scalar() or 0) / 3600, 1)
    guild_voice_channels = db.session.query(VoiceActivity.channel_name, func.count(VoiceActivity.id).label('c'), func.sum(VoiceActivity.duration_seconds).label('total')).filter_by(guild_id=guild_id).group_by(VoiceActivity.channel_name).order_by(func.sum(VoiceActivity.duration_seconds).desc()).limit(5).all()
    guild_voice_top_channels = [{'name': v.channel_name, 'sessions': v.c, 'hours': round((v.total or 0) / 3600, 1)} for v in guild_voice_channels]
    guild_voice_hourly_raw = db.session.query(
        VoiceActivity.hour_of_day, func.count(VoiceActivity.id).label('count')
    ).filter_by(guild_id=guild_id).filter(VoiceActivity.hour_of_day != None).group_by(VoiceActivity.hour_of_day).order_by(VoiceActivity.hour_of_day).all()
    guild_voice_hourly = {str(h): 0 for h in range(24)}
    for h, c in guild_voice_hourly_raw:
        guild_voice_hourly[str(h)] = c
    # Voice users per guild
    guild_voice_users = db.session.query(
        VoiceActivity.name, VoiceActivity.discord_id,
        func.count(VoiceActivity.id).label('sessions'),
        func.sum(VoiceActivity.duration_seconds).label('total_time')
    ).filter_by(guild_id=guild_id).group_by(VoiceActivity.name, VoiceActivity.discord_id).order_by(func.sum(VoiceActivity.duration_seconds).desc()).limit(20).all()
    guild_voice_users_list = [{'name': u.name, 'discord_id': u.discord_id, 'sessions': u.sessions, 'hours': round((u.total_time or 0) / 3600, 1)} for u in guild_voice_users]

    return render_template('guild.html',
        user=session.get('user'),
        accessible_guilds=session.get('accessible_guilds', []),
        guild=guild, roles=roles, staff=staff, members=non_staff, automod_rules=automod_rules,
        ping_events=ping_events, guild_hourly_chart=guild_hourly_chart, guild_msg_total=guild_msg_total,
        guild_voice_sessions=guild_voice_sessions, guild_voice_hours=guild_voice_hours,
        guild_voice_top_channels=guild_voice_top_channels, guild_voice_hourly=guild_voice_hourly,
        guild_voice_users=guild_voice_users_list)


@dashboard_bp.route('/search', methods=['GET'])
def message_search():
    """Search message content across trusted guilds."""
    redirect_resp = require_auth()
    if redirect_resp:
        return redirect_resp

    accessible_ids = get_accessible_guild_ids()
    q = request.args.get('q', '').strip()
    guild_filter = request.args.get('guild', '').strip()
    channel_filter = request.args.get('channel', '').strip()
    days = request.args.get('days', '7')
    try:
        days = int(days)
    except ValueError:
        days = 7
    days = max(1, min(90, days))

    base_query = MessageRecord.query.filter(
        MessageRecord.guild_id.in_(accessible_ids),
        MessageRecord.message_content != None,
        MessageRecord.message_content != '',
    )

    if q:
        base_query = base_query.filter(MessageRecord.message_content.ilike(f'%{q}%'))
    if guild_filter:
        base_query = base_query.filter(MessageRecord.guild_id == guild_filter)
    if channel_filter:
        base_query = base_query.filter(MessageRecord.channel_name.ilike(f'%{channel_filter}%'))
    if days:
        cutoff = datetime.utcnow() - timedelta(days=days)
        base_query = base_query.filter(MessageRecord.created_at >= cutoff)

    messages = base_query.order_by(MessageRecord.created_at.desc()).limit(200).all()
    total_results = base_query.count()

    # Guilds for filter dropdown
    guilds = GuildInfo.query.filter(GuildInfo.guild_id.in_(accessible_ids)).order_by(GuildInfo.name).all()

    return render_template('search.html',
        user=session.get('user'),
        accessible_guilds=session.get('accessible_guilds', []),
        messages=messages, total_results=total_results,
        q=q, guild_filter=guild_filter, channel_filter=channel_filter,
        days=days, guilds=guilds)
