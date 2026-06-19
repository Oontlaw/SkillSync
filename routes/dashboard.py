from flask import Blueprint, render_template, session, redirect, url_for
from database import db, Worker, Task, ScoreLog, AdminCorrection, MessageRecord, GuildInfo, GuildRole, GuildMember, BehavioralAnomaly
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
    most_active = db.session.query(
        MessageRecord.name, MessageRecord.discord_id, func.count(MessageRecord.id).label('count')
    ).group_by(MessageRecord.name, MessageRecord.discord_id).order_by(func.count(MessageRecord.id).desc()).limit(5).all()

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
        anomalies=recent_anomalies,
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

    return render_template('worker.html',
        user=session.get('user'),
        accessible_guilds=session.get('accessible_guilds', []),
        worker=worker, logs=logs, tasks=tasks, behavior=behavior, anomalies=anomalies)


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
    return render_template('guild.html',
        user=session.get('user'),
        accessible_guilds=session.get('accessible_guilds', []),
        guild=guild, roles=roles, staff=staff, members=non_staff)
