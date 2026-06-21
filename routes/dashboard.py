import os, json
import requests
from flask import Blueprint, render_template, session, redirect, url_for, request
from database import db, Worker, Task, ScoreLog, AdminCorrection, MessageRecord, GuildInfo, GuildRole, GuildMember, BehavioralAnomaly, MentionRecord, AutoModRule, AutoModTrigger, PingJoinEvent, VoiceActivity, BurnoutRisk
from datetime import datetime, timedelta
from sqlalchemy import func
import statistics
from ml import engine as ml_engine
from ml import forecast as ml_forecast
from ml import burnout as ml_burnout

dashboard_bp = Blueprint('dashboard', __name__)

DISCORD_API = 'https://discord.com/api/v10'
PERM_ADMINISTRATOR = 1 << 3
PERM_MANAGE_GUILD = 1 << 5

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
    refresh_accessible_guilds()
    return [g['id'] for g in session.get('accessible_guilds', [])]


def refresh_accessible_guilds():
    """Always re-fetch the user's guilds from Discord API and cross-reference with bot's GuildInfo.
    Falls back to cross-referencing session guilds with GuildInfo if the API call fails."""
    bot_guild_ids = set(
        g.guild_id for g in GuildInfo.query.with_entities(GuildInfo.guild_id).all()
    )

    token = session.get('discord_token')
    if token:
        try:
            resp = requests.get(f'{DISCORD_API}/users/@me/guilds',
                headers={'Authorization': f'Bearer {token}'}, timeout=10)
            if resp.ok:
                user_guilds = resp.json()
                accessible = []
                for g in user_guilds:
                    perms = int(g.get('permissions', '0'))
                    has_perm = perms & PERM_ADMINISTRATOR or perms & PERM_MANAGE_GUILD
                    if has_perm and g['id'] in bot_guild_ids:
                        accessible.append({'id': g['id'], 'name': g['name']})
                session['accessible_guilds'] = accessible
                session.modified = True
                return
        except Exception as e:
            print(f'[Dashboard] Guild refresh failed: {e}')

    # Fallback: cross-reference current session guilds with GuildInfo
    current = session.get('accessible_guilds', [])
    filtered = [g for g in current if g['id'] in bot_guild_ids]
    if filtered != current:
        session['accessible_guilds'] = filtered
        session.modified = True


@dashboard_bp.route('/')
def index():
    user = session.get('user')

    # Public landing page when not logged in
    if not user:
        return render_template('landing.html',
            user=None, invite_url=BOT_INVITE_URL, logged_out=True)

    accessible_ids = get_accessible_guild_ids()

    # Guild-scoped filter for ScoreLog (include legacy rows with NULL guild_id)
    if accessible_ids:
        scorelog_filter = db.or_(ScoreLog.guild_id.in_(accessible_ids), ScoreLog.guild_id == None)
    else:
        scorelog_filter = None

    # Per-guild scores for leaderboard
    per_guild_scores_query = db.session.query(
        ScoreLog.worker_id,
        ScoreLog.guild_id,
        func.sum(ScoreLog.change).label('score')
    ).group_by(ScoreLog.worker_id, ScoreLog.guild_id)
    if scorelog_filter is not None:
        per_guild_scores_query = per_guild_scores_query.filter(scorelog_filter)
    per_guild_scores = per_guild_scores_query.order_by(func.sum(ScoreLog.change).desc()).all()

    leaderboard_data = []
    for pg in per_guild_scores:
        worker = Worker.query.get(pg.worker_id)
        guild = None
        guild_name = 'Unknown'
        if pg.guild_id:
            guild = GuildInfo.query.filter_by(guild_id=pg.guild_id).first()
            if guild:
                guild_name = guild.name
        if worker:
            leaderboard_data.append({
                'worker': worker,
                'guild_id': pg.guild_id or '',
                'guild_name': guild_name,
                'score': pg.score
            })

    workers = Worker.query.order_by(Worker.score.desc()).all()
    total_workers = len(workers)
    total_tasks = Task.query.count()
    total_corrections = AdminCorrection.query.count()
    total_moderation_actions = ScoreLog.query.filter(ScoreLog.source == 'discord')
    if scorelog_filter is not None:
        total_moderation_actions = total_moderation_actions.filter(scorelog_filter)
    total_moderation_actions = total_moderation_actions.count()

    recent_logs = ScoreLog.query
    if scorelog_filter is not None:
        recent_logs = recent_logs.filter(scorelog_filter)
    recent_logs = recent_logs.order_by(ScoreLog.created_at.desc()).limit(10).all()
    # Filter out orphaned logs (worker deleted)
    recent_logs = [log for log in recent_logs if log.worker]
    # Attach guild name to each log
    for log in recent_logs:
        log.guild_name = None
        if log.guild_id:
            g = GuildInfo.query.filter_by(guild_id=log.guild_id).first()
            if g:
                log.guild_name = g.name

    # Common guild filter for message/voice queries
    guild_filter = MessageRecord.guild_id.in_(accessible_ids) if accessible_ids else None
    voice_guild_filter = VoiceActivity.guild_id.in_(accessible_ids) if accessible_ids else None

    # Behavioral analytics
    msg_base = MessageRecord.query
    if guild_filter is not None:
        msg_base = msg_base.filter(guild_filter)
    total_messages_logged = msg_base.count()
    unique_users_tracked = msg_base.with_entities(MessageRecord.discord_id).distinct().count()

    voice_base = VoiceActivity.query
    if voice_guild_filter is not None:
        voice_base = voice_base.filter(voice_guild_filter)
    total_voice_sessions = voice_base.count()
    total_voice_hours = round((voice_base.with_entities(func.sum(VoiceActivity.duration_seconds)).scalar() or 0) / 3600, 1)

    most_active = msg_base.with_entities(
        MessageRecord.name, MessageRecord.discord_id, func.count(MessageRecord.id).label('count')
    ).group_by(MessageRecord.name, MessageRecord.discord_id).order_by(func.count(MessageRecord.id).desc()).limit(5).all()

    # Hourly activity for charting (aggregate)
    hourly_data = msg_base.with_entities(
        MessageRecord.hour_of_day, func.count(MessageRecord.id).label('count')
    ).group_by(MessageRecord.hour_of_day).order_by(MessageRecord.hour_of_day).all()
    hourly_activity = {str(h): 0 for h in range(24)}
    for h, c in hourly_data:
        hourly_activity[str(h)] = c
    # Per-guild hourly activity for comparative chart
    guild_hourly_raw = msg_base.with_entities(
        MessageRecord.guild_id, MessageRecord.hour_of_day, func.count(MessageRecord.id).label('count')
    ).group_by(MessageRecord.guild_id, MessageRecord.hour_of_day).order_by(MessageRecord.guild_id, MessageRecord.hour_of_day).all()
    guild_hourly = {}
    for guild_id, hour, count in guild_hourly_raw:
        if guild_id not in guild_hourly:
            guild_hourly[guild_id] = {str(h): 0 for h in range(24)}
        guild_hourly[guild_id][str(hour)] = count

    # Per-guild message totals
    guild_msg_counts_raw = msg_base.with_entities(
        MessageRecord.guild_id, func.count(MessageRecord.id).label('count')
    ).group_by(MessageRecord.guild_id).all()
    guild_msg_counts = {g: c for g, c in guild_msg_counts_raw}

    # Message volume last 7 days
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    daily_vol = msg_base.filter(MessageRecord.created_at >= seven_days_ago).with_entities(
        func.date(MessageRecord.created_at).label('day'), func.count(MessageRecord.id).label('count')
    ).group_by(func.date(MessageRecord.created_at)).order_by(func.date(MessageRecord.created_at)).all()
    daily_volume = {str(d.day): d.count for d in daily_vol}

    # Score source breakdown (per-guild scoped)
    source_query = db.session.query(
        ScoreLog.source, func.count(ScoreLog.id).label('count')
    )
    if scorelog_filter is not None:
        source_query = source_query.filter(scorelog_filter)
    source_data = source_query.group_by(ScoreLog.source).all()
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
        BehavioralAnomaly.detected_at > datetime.utcnow() - timedelta(hours=48)
    )
    if accessible_ids:
        recent_anomalies = recent_anomalies.filter(
            db.or_(BehavioralAnomaly.guild_id == None, BehavioralAnomaly.guild_id.in_(accessible_ids))
        )
    recent_anomalies = recent_anomalies.order_by(BehavioralAnomaly.severity.desc()).limit(10).all()

    # Burnout risks (#2)
    burnout_risks = BurnoutRisk.query.order_by(BurnoutRisk.score.desc()).limit(5).all()

    # ML model status
    ml_status = ml_engine.get_model_status()
    ml_last_train = None
    summary_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'ml', 'models', 'training_summary.json')
    if os.path.exists(summary_path):
        try:
            with open(summary_path) as f:
                ml_last_train = json.load(f).get('trained_at', '')
        except Exception:
            pass

    return render_template('dashboard.html',
        user=user,
        accessible_guilds=session.get('accessible_guilds', []),
        invite_url=BOT_INVITE_URL, logged_out=False,
        workers=workers,
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
        burnout_risks=burnout_risks,
        ml_status=ml_status,
        ml_last_train=ml_last_train,
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

        # Activity Consistency & Trend (#1)
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        daily_counts = db.session.query(
            func.date(MessageRecord.created_at).label('day'),
            func.count(MessageRecord.id).label('c')
        ).filter(
            MessageRecord.discord_id == worker.discord_id,
            MessageRecord.created_at >= thirty_days_ago
        ).group_by(func.date(MessageRecord.created_at)).order_by(func.date(MessageRecord.created_at)).all()
        daily_vals = [d.c for d in daily_counts]

        consistency_score = 0
        trend = 'flat'
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
            recent = db.session.query(func.count(MessageRecord.id)).filter(
                MessageRecord.discord_id == worker.discord_id,
                MessageRecord.created_at >= seven_days_ago
            ).scalar() or 0
            prior = db.session.query(func.count(MessageRecord.id)).filter(
                MessageRecord.discord_id == worker.discord_id,
                MessageRecord.created_at >= fourteen_days_ago,
                MessageRecord.created_at < seven_days_ago
            ).scalar() or 0
            recent_total = recent
            prior_total = prior
            if prior > 0:
                change_pct = (recent - prior) / prior * 100
                if change_pct > 20:
                    trend = 'up'
                elif change_pct < -20:
                    trend = 'down'
                else:
                    trend = 'flat'

            # Off-hours ratio (outside 09:00-17:00)
            total_msgs = recent + prior
            off_hours = db.session.query(func.count(MessageRecord.id)).filter(
                MessageRecord.discord_id == worker.discord_id,
                MessageRecord.created_at >= fourteen_days_ago,
                ~MessageRecord.hour_of_day.between(9, 16)
            ).scalar() or 0
            off_hours_ratio = round(off_hours / total_msgs * 100, 1) if total_msgs > 0 else 0

        activity_consistency = {
            'score': consistency_score,
            'trend': trend,
            'off_hours_ratio': off_hours_ratio,
            'recent_total': recent_total,
            'prior_total': prior_total,
        }

        # Moderation Quality Score (#4)
        mod_logs = ScoreLog.query.filter_by(worker_id=worker_id, source='discord').all()
        total_actions = len(mod_logs)
        total_warns = sum(1 for l in mod_logs if 'warn' in l.reason.lower())
        total_bans = sum(1 for l in mod_logs if 'ban' in l.reason.lower())
        total_kicks = sum(1 for l in mod_logs if 'kick' in l.reason.lower())
        total_timeouts = sum(1 for l in mod_logs if 'timeout' in l.reason.lower())
        punitive_actions = total_bans + total_kicks + total_timeouts

        reversal_count = ScoreLog.query.filter_by(worker_id=worker_id, source='discord').filter(
            ScoreLog.change < 0, ScoreLog.reason.ilike('%reversal%')
        ).count()

        action_warn_ratio = round(punitive_actions / max(total_warns, 1), 1) if total_warns > 0 else 0
        reversal_rate = round(reversal_count / max(total_actions, 1) * 100, 1)
        q_score = 100
        if total_actions > 0:
            q_score -= reversal_rate * 2
            q_score = max(0, min(100, round(q_score, 1)))

        mod_quality = {
            'total_actions': total_actions,
            'total_warns': total_warns,
            'action_warn_ratio': action_warn_ratio,
            'reversal_rate': reversal_rate,
            'quality_score': q_score,
        }
    else:
        mention_stats = None
        voice_stats = None
        activity_consistency = None
        mod_quality = None

    return render_template('worker.html',
        user=session.get('user'),
        accessible_guilds=session.get('accessible_guilds', []),
        worker=worker, logs=logs, tasks=tasks, behavior=behavior, anomalies=anomalies, mention_stats=mention_stats,
        voice_stats=voice_stats,
        worker_hourly_activity=worker_hourly_activity, cumulative_score_data=cumulative_score_data,
        activity_consistency=activity_consistency, mod_quality=mod_quality)


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

    # Peak Hour Staffing Overlay (#3) — community vs staff hourly activity
    staff_ids = [m.member_id for m in staff]
    community_hourly = {str(h): 0 for h in range(24)}
    staff_hourly = {str(h): 0 for h in range(24)}
    if staff_ids:
        for is_staff_list, target in [(staff_ids, staff_hourly), (None, community_hourly)]:
            q = db.session.query(
                MessageRecord.hour_of_day, func.count(MessageRecord.id).label('count')
            ).filter(MessageRecord.guild_id == guild_id)
            if is_staff_list:
                q = q.filter(MessageRecord.discord_id.in_(staff_ids))
            else:
                q = q.filter(~MessageRecord.discord_id.in_(staff_ids)) if staff_ids else q
            for h, c in q.group_by(MessageRecord.hour_of_day).order_by(MessageRecord.hour_of_day).all():
                target[str(h)] = c

    # Chart data using actual DB counts
    online_count = GuildMember.query.filter_by(guild_id=guild_id, is_bot=False, is_online=True).count()
    tracked_chatted = GuildMember.query.filter_by(guild_id=guild_id, is_bot=False).filter(GuildMember.last_message_at != None).count()
    tracked_offline = GuildMember.query.filter_by(guild_id=guild_id, is_bot=False, is_online=False).filter(GuildMember.last_message_at == None).count()
    community_count = GuildMember.query.filter_by(guild_id=guild_id, is_bot=False, is_staff=False).count()
    human_count = GuildMember.query.filter_by(guild_id=guild_id, is_bot=False).count()
    bot_count = GuildMember.query.filter_by(guild_id=guild_id, is_bot=True).count()

    # AutoMod triggers
    automod_triggers = AutoModTrigger.query.filter_by(guild_id=guild_id).order_by(AutoModTrigger.created_at.desc()).limit(30).all()

    # ML forecast for this guild
    forecast_preds = ml_forecast.predict_next_24h(guild_id)
    forecast_data = None
    if forecast_preds is not None:
        forecast_data = forecast_preds.tolist()

    return render_template('guild.html',
        user=session.get('user'),
        accessible_guilds=session.get('accessible_guilds', []),
        guild=guild, roles=roles, staff=staff, members=non_staff, automod_rules=automod_rules, automod_triggers=automod_triggers,
        ping_events=ping_events, guild_hourly_chart=guild_hourly_chart, guild_msg_total=guild_msg_total,
        guild_voice_sessions=guild_voice_sessions, guild_voice_hours=guild_voice_hours,
        guild_voice_top_channels=guild_voice_top_channels, guild_voice_hourly=guild_voice_hourly,
        guild_voice_users=guild_voice_users_list,
        community_hourly=community_hourly, staff_hourly=staff_hourly,
        online_count=online_count, tracked_offline=tracked_offline, tracked_chatted=tracked_chatted,
        community_count=community_count, human_count=human_count, bot_count=bot_count,
        forecast_data=forecast_data)



