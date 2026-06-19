from flask import Blueprint, render_template
from database import db, Worker, Task, ScoreLog, AdminCorrection, MessageRecord, GuildInfo, GuildRole, GuildMember
from sqlalchemy import func

dashboard_bp = Blueprint('dashboard', __name__)

@dashboard_bp.route('/')
def index():
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

    # Guild scan overview
    guilds = GuildInfo.query.order_by(GuildInfo.name).all()
    total_guilds = len(guilds)
    total_members_tracked = sum(g.member_count for g in guilds)
    total_online_members = sum(g.online_count for g in guilds)
    total_staff_tracked = GuildMember.query.filter_by(is_staff=True, is_bot=False).count()

    return render_template('dashboard.html',
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
    )

@dashboard_bp.route('/worker/<int:worker_id>')
def worker_detail(worker_id):
    worker = Worker.query.get_or_404(worker_id)
    logs = ScoreLog.query.filter_by(worker_id=worker_id).order_by(ScoreLog.created_at.desc()).all()
    tasks = Task.query.filter_by(worker_id=worker_id).order_by(Task.assigned_at.desc()).all()

    # Behavioral data for this worker (if they have a discord_id)
    behavior = None
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

    return render_template('worker.html', worker=worker, logs=logs, tasks=tasks, behavior=behavior)


@dashboard_bp.route('/guild/<guild_id>')
def guild_detail(guild_id):
    guild = GuildInfo.query.filter_by(guild_id=guild_id).first_or_404()
    roles = db.session.query(GuildRole).filter_by(guild_id=guild_id).order_by(GuildRole.position.desc()).all()
    members = db.session.query(GuildMember).filter_by(guild_id=guild_id, is_bot=False).order_by(GuildMember.top_role_position.desc(), GuildMember.name).all()
    staff = [m for m in members if m.is_staff]
    non_staff = [m for m in members if not m.is_staff]
    return render_template('guild.html', guild=guild, roles=roles, staff=staff, members=non_staff)
