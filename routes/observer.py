import json
import os
import uuid
import math
from collections import defaultdict
from functools import wraps
from flask import Blueprint, request, jsonify
from database import db, Worker, ScoreLog, MessageRecord, GuildInfo, GuildRole, GuildMember, GuildChannel, BehavioralAnomaly, MentionRecord, AutoModRule, AutoModTrigger, PingJoinEvent, VoiceActivity, BurnoutRisk, PendingBan, PendingTimeout, RoleChangeLog, MemberJoinLeave
from datetime import datetime, timedelta
from sqlalchemy import func
from ml import engine as ml_engine
from ml import anomaly as ml_anomaly
from ml import burnout as ml_burnout
from ml import forecast as ml_forecast
from ml import federated as ml_federated

observer_bp = Blueprint('observer', __name__)

# ── Retrain-on-correction flag (file-based, survives reloader forks) ──
_MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'ml', 'models')
_RETRAIN_FLAG_FILE = os.path.join(_MODELS_DIR, '.retrain_requested')

def _set_retrain_flag():
    os.makedirs(_MODELS_DIR, exist_ok=True)
    with open(_RETRAIN_FLAG_FILE, 'w') as f:
        f.write(datetime.utcnow().isoformat())

def consume_retrain_request():
    if os.path.exists(_RETRAIN_FLAG_FILE):
        try:
            with open(_RETRAIN_FLAG_FILE) as f:
                ts = f.read().strip()
            os.remove(_RETRAIN_FLAG_FILE)
            return ts
        except Exception:
            return None
    return None

# ── API Key Authentication ──
API_KEY = os.getenv('API_KEY')
if not API_KEY:
    raise RuntimeError('API_KEY environment variable is not set — all requests would be rejected')

def require_api_key(f):
    """Requires Bearer token matching API_KEY env var."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer ') or auth.split(' ', 1)[1] != API_KEY:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


def validate_payload(data, required_fields):
    """Check required fields exist and are non-empty."""
    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        return False, f'Missing required fields: {", ".join(missing)}'
    return True, None


def sanitize_str(value, max_length=200):
    """Strip and truncate string inputs."""
    if value is None:
        return None
    return str(value).strip()[:max_length]


def paginate_query(query, page=1, per_page=20):
    """Paginate a SQLAlchemy query and return results with metadata."""
    page = max(1, int(page))
    per_page = max(1, min(100, int(per_page)))
    total = query.count()
    total_pages = math.ceil(total / per_page) if total > 0 else 1
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return {
        'items': items,
        'page': page,
        'per_page': per_page,
        'total': total,
        'total_pages': total_pages,
        'has_next': page < total_pages,
        'has_prev': page > 1
    }


VALID_ACTIONS = {
    'ban_issued', 'kick_issued', 'timeout_issued', 'warn_issued',
    'ban_confirmed', 'ban_reversed', 'timeout_reversed',
    'presence_change', 'member_join'
}

# ── In-memory store for staff activity (aggregated per session) ──
# { discord_id: { 'message_count': int, 'channels': set, 'last_seen': datetime } }
staff_activity = {}


@observer_bp.route('/observer/action', methods=['POST'])
@require_api_key
def log_action():
    """
    Receives a staff moderation action (ban, kick, timeout).
    Logs it and gives the staff member points for taking action.
    """
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400

    ok, err = validate_payload(data, ['discord_id', 'staff_name', 'action_type', 'guild'])
    if not ok:
        return jsonify({'error': err}), 400

    action_type = data.get('action_type', '')
    if action_type not in VALID_ACTIONS:
        return jsonify({'error': f'Invalid action_type: {action_type}'}), 400

    discord_id = sanitize_str(data.get('discord_id'), 50)
    staff_name = sanitize_str(data.get('staff_name'), 100)
    guild = sanitize_str(data.get('guild'), 100)
    target = sanitize_str(data.get('target'), 100)
    reason = sanitize_str(data.get('reason', 'No reason given'), 300)

    # Find the worker by discord_id, create if doesn't exist
    worker = Worker.query.filter_by(discord_id=discord_id).first()
    if not worker:
        worker = Worker(
            name=staff_name or 'Unknown',
            email=f'worker.{uuid.uuid4().hex[:12]}@discord.local',
            discord_id=discord_id,
            role='admin',
            score=0.0
        )
        db.session.add(worker)
        db.session.commit()

    points_map = {
        'ban_issued': 8,
        'kick_issued': 5,
        'timeout_issued': 4,
        'warn_issued': 3,
    }
    points = points_map.get(action_type, 0)

    if worker and points:
        log = ScoreLog(
            worker_id=worker.id,
            change=points,
            reason=f'[Discord] {action_type.replace("_", " ").title()} on {target} in {guild}',
            source='discord',
            admin_correction=False,
            guild_id=sanitize_str(data.get('guild_id'), 50),
        )
        db.session.add(log)

        # Persist pending state for reversal tracking
        gid = sanitize_str(data.get('guild_id'), 50)
        if action_type == 'ban_issued' and discord_id and gid:
            target_id = sanitize_str(data.get('target_id', ''), 50)
            existing = PendingBan.query.filter_by(guild_id=gid, user_id=target_id).first()
            if not existing:
                db.session.add(PendingBan(
                    guild_id=gid,
                    user_id=sanitize_str(data.get('target_id', ''), 50),
                    banner_id=discord_id,
                    banner_name=staff_name,
                    user_name=target or 'Unknown',
                    reason=reason,
                ))
        elif action_type == 'timeout_issued' and discord_id and gid:
            duration_minutes = int(data.get('duration_minutes', 60))
            db.session.add(PendingTimeout(
                guild_id=gid,
                user_id=sanitize_str(data.get('target_id', ''), 50),
                mod_id=discord_id,
                mod_name=staff_name,
                until=datetime.utcnow() + timedelta(minutes=duration_minutes) if data.get('duration_minutes') else None,
            ))
        elif action_type == 'ban_confirmed':
            PendingBan.query.filter_by(guild_id=gid, user_id=sanitize_str(data.get('target_id', ''), 50)).delete()
        db.session.commit()

    print(f'[Observer API] Action logged: {action_type} by {staff_name} on {target}')
    return jsonify({'message': 'Action logged', 'points_awarded': points}), 201


@observer_bp.route('/observer/flag', methods=['POST'])
@require_api_key
def log_flag():
    """
    Receives a flagged event (ban reversed, timeout reversed early).
    Deducts points if flagged=True.
    """
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400

    ok, err = validate_payload(data, ['discord_id', 'staff_name', 'action_type', 'guild'])
    if not ok:
        return jsonify({'error': err}), 400

    action_type = data.get('action_type', '')
    if action_type not in VALID_ACTIONS:
        return jsonify({'error': f'Invalid action_type: {action_type}'}), 400

    discord_id = sanitize_str(data.get('discord_id'), 50)
    staff_name = sanitize_str(data.get('staff_name'), 100)
    guild = sanitize_str(data.get('guild'), 100)
    target = sanitize_str(data.get('target'), 100)
    flagged = data.get('flagged', False)
    flag_reason = sanitize_str(data.get('flag_reason', ''), 300)
    hours = data.get('hours_until_reversal')

    # Find the worker by discord_id, create if doesn't exist
    worker = Worker.query.filter_by(discord_id=discord_id).first()
    if not worker:
        worker = Worker(
            name=staff_name,
            email=f'worker.{uuid.uuid4().hex[:12]}@discord.local',
            discord_id=discord_id,
            role='admin',
            score=0.0
        )
        db.session.add(worker)
        db.session.commit()

    if flagged and worker:
        # Deduct points for reversed/wrongful actions
        deduction_map = {
            'ban_reversed': -15,
            'timeout_reversed': -8,
        }
        points = deduction_map.get(action_type, -5)

        note = flag_reason
        if hours:
            note += f' ({hours:.1f} hours later)'

        # Clean up pending state on reversal
        gid = sanitize_str(data.get('guild_id'), 50)
        if action_type == 'ban_reversed' and gid:
            PendingBan.query.filter_by(guild_id=gid).delete()
        elif action_type == 'timeout_reversed' and gid:
            PendingTimeout.query.filter_by(guild_id=gid).delete()

        log = ScoreLog(
            worker_id=worker.id,
            change=points,
            reason=f'[Discord] FLAG: {note} | Target: {target} in {guild}',
            source='discord',
            admin_correction=False,
            guild_id=gid,
        )
        db.session.add(log)
        db.session.commit()

        print(f'[Observer API] Flagged action: {action_type} by {staff_name} — {points} pts')

    return jsonify({'message': 'Flag logged', 'flagged': flagged}), 201


@observer_bp.route('/observer/warn', methods=['POST'])
@require_api_key
def log_warn():
    """
    Receives a warn/infraction parsed from a mod bot embed.
    Awards small points to the staff member for issuing a warn.
    """
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400

    mod_name = sanitize_str(data.get('mod_name'), 100)
    target_name = sanitize_str(data.get('target_name'), 100)
    reason = sanitize_str(data.get('reason'), 300)
    source_bot = sanitize_str(data.get('source_bot'), 100)
    channel = sanitize_str(data.get('channel'), 100)
    guild = sanitize_str(data.get('guild'), 100)

    # Try to find worker by name (since we only have name from embed parsing)
    worker = Worker.query.filter(
        Worker.name.ilike(f'%{mod_name}%')
    ).first() if mod_name else None

    if worker:
        log = ScoreLog(
            worker_id=worker.id,
            change=3,
            reason=f'[Discord] Warn issued to {target_name} via {source_bot} in #{channel} | {guild}',
            source='discord',
            admin_correction=False,
            guild_id=sanitize_str(data.get('guild_id'), 50),
        )
        db.session.add(log)
        db.session.commit()

    print(f'[Observer API] Warn logged: {mod_name} warned {target_name} — reason: {reason}')
    return jsonify({'message': 'Warn logged'}), 201


@observer_bp.route('/observer/activity', methods=['POST'])
@require_api_key
def log_activity():
    """
    Receives passive staff message activity.
    Aggregated in memory — not logged per-message to avoid spam.
    Every 20 messages = 1 activity point.
    """
    data = request.json
    discord_id = data.get('discord_id')
    staff_name = data.get('staff_name')
    channel = data.get('channel')
    guild = data.get('guild')

    if discord_id not in staff_activity:
        staff_activity[discord_id] = {
            'name': staff_name,
            'message_count': 0,
            'channels': set(),
            'guild': guild
        }

    staff_activity[discord_id]['message_count'] += 1
    staff_activity[discord_id]['channels'].add(channel)
    staff_activity[discord_id]['last_seen'] = datetime.utcnow().isoformat()

    # Every 20 messages, award 1 activity point
    count = staff_activity[discord_id]['message_count']
    if count % 20 == 0:
        worker = Worker.query.filter_by(discord_id=discord_id).first()
        if worker:
            log = ScoreLog(
                worker_id=worker.id,
                change=1,
                reason=f'[Discord] Active in {len(staff_activity[discord_id]["channels"])} channel(s) in {guild}',
                source='discord',
                admin_correction=False,
                guild_id=sanitize_str(data.get('guild_id'), 50),
            )
            db.session.add(log)
            db.session.commit()

    return jsonify({'message': 'Activity recorded', 'total_messages': count}), 200


@observer_bp.route('/observer/confirm', methods=['POST'])
@require_api_key
def confirm_action():
    """
    Confirms a ban stood for 48+ hours (valid moderation action).
    Removes the matching PendingBan row from the database.
    """
    data = request.json or {}
    guild_id = sanitize_str(data.get('guild_id'), 50)
    target_id = sanitize_str(data.get('target_id'), 50)
    if guild_id and target_id:
        deleted = PendingBan.query.filter_by(guild_id=guild_id, user_id=target_id).delete()
        db.session.commit()
        if deleted:
            print(f'[Observer API] PendingBan deleted: {target_id} in {guild_id}')
    print(f'[Observer API] Confirmed: {data.get("action_type")} by {data.get("staff_name")} on {data.get("target")}')
    return jsonify({'message': 'Action confirmed as valid'}), 200


@observer_bp.route('/observer/automod-trigger', methods=['POST'])
@require_api_key
def log_automod_trigger():
    """Log an AutoMod trigger event from alert channel parsing.
    Content snippet is only stored if the guild has store_content enabled."""
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    gid = sanitize_str(data.get('guild_id'), 50)
    guild = GuildInfo.query.filter_by(guild_id=gid).first() if gid else None
    store_ok = guild is not None and guild.store_content

    trigger = AutoModTrigger(
        guild_id=gid,
        rule_id=sanitize_str(data.get('rule_id'), 50),
        rule_name=sanitize_str(data.get('rule_name'), 200),
        user_id=sanitize_str(data.get('user_id'), 50),
        user_name=sanitize_str(data.get('user_name'), 100),
        channel_id=sanitize_str(data.get('channel_id'), 50),
        channel_name=sanitize_str(data.get('channel_name'), 100),
        content_snippet=sanitize_str(data.get('content_snippet'), 500) if store_ok else None,
        action_taken=sanitize_str(data.get('action_taken'), 100),
    )
    db.session.add(trigger)
    db.session.commit()
    print(f'[Observer API] AutoMod trigger: {trigger.rule_name} -> {trigger.user_name} in #{trigger.channel_name}')
    return jsonify({'message': 'AutoMod trigger logged'}), 201


@observer_bp.route('/observer/staff-activity', methods=['GET'])
@require_api_key
def get_staff_activity():
    """Returns current in-session staff activity summary."""
    summary = {
        discord_id: {
            'name': v['name'],
            'message_count': v['message_count'],
            'channels_active': list(v['channels']),
            'guild': v['guild'],
            'last_seen': v.get('last_seen')
        }
        for discord_id, v in staff_activity.items()
    }
    return jsonify(summary)


@observer_bp.route('/observer/pending-state', methods=['GET'])
@require_api_key
def get_pending_state():
    """Return all pending bans and timeouts for bot startup recovery."""
    bans = PendingBan.query.all()
    timeouts = PendingTimeout.query.all()
    return jsonify({
        'pending_bans': [{
            'guild_id': b.guild_id,
            'user_id': b.user_id,
            'banner_id': b.banner_id,
            'banner_name': b.banner_name,
            'user_name': b.user_name,
            'reason': b.reason,
            'timestamp': b.created_at.isoformat() if b.created_at else None
        } for b in bans],
        'pending_timeouts': [{
            'guild_id': t.guild_id,
            'user_id': t.user_id,
            'mod_id': t.mod_id,
            'mod_name': t.mod_name,
            'until': t.until.isoformat() if t.until else None,
            'timestamp': t.created_at.isoformat() if t.created_at else None
        } for t in timeouts]
    })


# ─────────────────────────────────────────────
# BEHAVIORAL MESSAGE LOGGING (Community Engine)
# ─────────────────────────────────────────────

@observer_bp.route('/observer/messages', methods=['POST'])
@require_api_key
def log_messages():
    """
    Receives a batch of message records from the bot.
    Also updates GuildMember.last_message_at for each unique author.
    """
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400

    messages = data if isinstance(data, list) else [data]

    # Determine which guilds allow content storage
    guild_ids = set(sanitize_str(m.get('guild_id', ''), 50) for m in messages if m.get('guild_id'))
    content_ok = set()
    if guild_ids:
        rows = GuildInfo.query.with_entities(GuildInfo.guild_id, GuildInfo.store_content).filter(GuildInfo.guild_id.in_(guild_ids)).all()
        content_ok = {r.guild_id for r in rows if r.store_content}

    for msg in messages:
        ok, err = validate_payload(msg, ['discord_id'])
        if not ok:
            continue

        gid = sanitize_str(msg.get('guild_id', ''), 50)
        content = sanitize_str(msg.get('content'), 2000) if msg.get('content') and gid in content_ok and msg.get('is_public', True) else None

        record = MessageRecord(
            discord_id=sanitize_str(msg['discord_id'], 50),
            name=sanitize_str(msg.get('name', 'Unknown'), 100),
            guild_id=gid,
            channel_name=sanitize_str(msg.get('channel', 'unknown'), 100),
            message_length=msg.get('length', 0),
            is_public_channel=msg.get('is_public', True),
            message_content=content,
            hour_of_day=msg.get('hour'),
            day_of_week=msg.get('day'),
        )
        db.session.add(record)

    # Update GuildMember.last_message_at for each unique author
    # Create GuildMember row if it doesn't exist yet (common in large guilds)
    now = datetime.utcnow()
    seen = set()
    for msg in messages:
        guild_id = sanitize_str(msg.get('guild_id', ''), 50)
        discord_id = sanitize_str(msg.get('discord_id', ''), 50)
        if not guild_id or not discord_id:
            continue
        key = (guild_id, discord_id)
        if key not in seen:
            seen.add(key)
            member = GuildMember.query.filter_by(guild_id=guild_id, member_id=discord_id).first()
            if member:
                member.last_message_at = now
            else:
                member = GuildMember(
                    guild_id=guild_id,
                    member_id=discord_id,
                    name=sanitize_str(msg.get('name', 'Unknown'), 200),
                    is_online=True,
                    status='online',
                    last_seen_online=now,
                    last_message_at=now,
                )
                db.session.add(member)

    db.session.commit()
    return jsonify({'message': f'{len(messages)} messages logged'}), 201


@observer_bp.route('/observer/mentions', methods=['POST'])
@require_api_key
def log_mentions():
    """Receives a batch of mention records from the bot."""
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400

    records = data if isinstance(data, list) else [data]
    for rec in records:
        ok, err = validate_payload(rec, ['mentioner_id', 'mentioned_id', 'guild_id'])
        if not ok:
            continue
        entry = MentionRecord(
            mentioner_id=sanitize_str(rec['mentioner_id'], 50),
            mentioner_name=sanitize_str(rec.get('mentioner_name'), 100),
            mentioned_id=sanitize_str(rec['mentioned_id'], 50),
            mentioned_name=sanitize_str(rec.get('mentioned_name'), 100),
            guild_id=sanitize_str(rec['guild_id'], 50),
            channel_name=sanitize_str(rec.get('channel_name'), 100),
            reply_time_seconds=rec.get('reply_time_seconds'),
        )
        db.session.add(entry)

    db.session.commit()
    return jsonify({'message': f'{len(records)} mentions logged'}), 201


@observer_bp.route('/observer/mentions/<discord_id>', methods=['GET'])
@require_api_key
def user_mention_analytics(discord_id):
    """Returns mention analytics for a specific user (who mentioned them)."""
    mentions_received = MentionRecord.query.filter_by(mentioned_id=discord_id).count()
    mentions_sent = MentionRecord.query.filter_by(mentioner_id=discord_id).count()

    avg_reply_time = db.session.query(func.avg(MentionRecord.reply_time_seconds)).filter(
        MentionRecord.mentioned_id == discord_id,
        MentionRecord.reply_time_seconds != None
    ).scalar()

    slowest_reply = MentionRecord.query.filter_by(mentioned_id=discord_id).filter(
        MentionRecord.reply_time_seconds != None
    ).order_by(MentionRecord.reply_time_seconds.desc()).first()

    return jsonify({
        'discord_id': discord_id,
        'mentions_received': mentions_received,
        'mentions_sent': mentions_sent,
        'avg_reply_time_seconds': round(avg_reply_time or 0, 1),
        'slowest_reply_seconds': round(slowest_reply.reply_time_seconds or 0, 1) if slowest_reply else None,
    })


@observer_bp.route('/observer/analytics/<discord_id>', methods=['GET'])
@require_api_key
def user_analytics(discord_id):
    """Returns behavioral analytics for a specific user."""
    stats = db.session.query(
        func.count(MessageRecord.id).label('total_messages'),
        func.avg(MessageRecord.message_length).label('avg_length'),
        func.min(func.date(MessageRecord.created_at)).label('first_seen'),
        func.max(func.date(MessageRecord.created_at)).label('last_seen'),
    ).filter(MessageRecord.discord_id == discord_id).first()

    hourly = db.session.query(
        MessageRecord.hour_of_day,
        func.count(MessageRecord.id).label('count')
    ).filter(MessageRecord.discord_id == discord_id).group_by(MessageRecord.hour_of_day).order_by(MessageRecord.hour_of_day).all()

    channels = db.session.query(
        MessageRecord.channel_name,
        func.count(MessageRecord.id).label('count')
    ).filter(MessageRecord.discord_id == discord_id).group_by(MessageRecord.channel_name).order_by(func.count(MessageRecord.id).desc()).all()

    return jsonify({
        'discord_id': discord_id,
        'total_messages': stats.total_messages or 0,
        'avg_message_length': round(stats.avg_length or 0, 1),
        'first_seen': str(stats.first_seen or ''),
        'last_seen': str(stats.last_seen or ''),
        'hourly_activity': {str(h.hour_of_day): h.count for h in hourly},
        'channels': {c.channel_name: c.count for c in channels},
    })


@observer_bp.route('/observer/messages', methods=['GET'])
@require_api_key
def get_messages():
    """Paginated endpoint to get message records."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    guild_id = request.args.get('guild_id')
    discord_id = request.args.get('discord_id')
    
    query = MessageRecord.query.order_by(MessageRecord.created_at.desc())
    
    if guild_id:
        query = query.filter(MessageRecord.guild_id == guild_id)
    if discord_id:
        query = query.filter(MessageRecord.discord_id == discord_id)
    
    paginated = paginate_query(query, page, per_page)
    
    return jsonify({
        'page': paginated['page'],
        'per_page': paginated['per_page'],
        'total': paginated['total'],
        'total_pages': paginated['total_pages'],
        'has_next': paginated['has_next'],
        'has_prev': paginated['has_prev'],
        'items': [{
            'id': m.id,
            'discord_id': m.discord_id,
            'name': m.name,
            'guild_id': m.guild_id,
            'channel_name': m.channel_name,
            'is_public_channel': m.is_public_channel,
            'message_length': m.message_length,
            'message_content': m.message_content,
            'hour_of_day': m.hour_of_day,
            'day_of_week': m.day_of_week,
            'created_at': m.created_at.isoformat() if m.created_at else None
        } for m in paginated['items']]
    })


@observer_bp.route('/observer/guilds/<guild_id>/members', methods=['GET'])
@require_api_key
def get_guild_members(guild_id):
    """Paginated endpoint to get guild members with filters."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    is_staff = request.args.get('is_staff')
    is_bot = request.args.get('is_bot')
    is_online = request.args.get('is_online')
    
    query = GuildMember.query.filter(GuildMember.guild_id == guild_id).order_by(GuildMember.top_role_position.desc(), GuildMember.name)
    
    if is_staff is not None:
        query = query.filter(GuildMember.is_staff == (is_staff.lower() == 'true'))
    if is_bot is not None:
        query = query.filter(GuildMember.is_bot == (is_bot.lower() == 'true'))
    if is_online is not None:
        query = query.filter(GuildMember.is_online == (is_online.lower() == 'true'))
    
    paginated = paginate_query(query, page, per_page)
    
    return jsonify({
        'page': paginated['page'],
        'per_page': paginated['per_page'],
        'total': paginated['total'],
        'total_pages': paginated['total_pages'],
        'has_next': paginated['has_next'],
        'has_prev': paginated['has_prev'],
        'items': [{
            'id': m.id,
            'guild_id': m.guild_id,
            'member_id': m.member_id,
            'name': m.name,
            'display_name': m.display_name,
            'is_bot': m.is_bot,
            'is_owner': m.is_owner,
            'is_staff': m.is_staff,
            'is_online': m.is_online,
            'status': m.status,
            'activity_name': m.activity_name,
            'last_seen_online': m.last_seen_online.isoformat() if m.last_seen_online else None,
            'last_message_at': m.last_message_at.isoformat() if m.last_message_at else None,
            'created_at': m.created_at.isoformat() if m.created_at else None
        } for m in paginated['items']]
    })


@observer_bp.route('/observer/analytics', methods=['GET'])
@require_api_key
def all_analytics():
    """Returns overall behavioral analytics, optionally filtered by guild_id."""
    guild_ids = request.args.getlist('guild_id')
    base = MessageRecord.query
    if guild_ids:
        base = base.filter(MessageRecord.guild_id.in_(guild_ids))

    total_messages = base.count()
    unique_users = base.with_entities(MessageRecord.discord_id).distinct().count()

    top_users = base.with_entities(
        MessageRecord.discord_id,
        MessageRecord.name,
        func.count(MessageRecord.id).label('count')
    ).group_by(MessageRecord.discord_id, MessageRecord.name).order_by(func.count(MessageRecord.id).desc()).limit(10).all()

    hourly_all = base.with_entities(
        MessageRecord.hour_of_day,
        func.count(MessageRecord.id).label('count')
    ).group_by(MessageRecord.hour_of_day).order_by(MessageRecord.hour_of_day).all()

    return jsonify({
        'total_messages': total_messages,
        'unique_users': unique_users,
        'top_users': [{'id': u.discord_id, 'name': u.name, 'messages': u.count} for u in top_users],
        'hourly_activity': {str(h.hour_of_day): h.count for h in hourly_all},
    })


# ─────────────────────────────────────────────
# GUILD SCANNING — Role & Staff Analysis
# ─────────────────────────────────────────────

@observer_bp.route('/observer/guild-scan', methods=['POST'])
@require_api_key
def receive_guild_scan():
    """
    Receives a full guild scan from the bot.
    Stores guild info, roles, and members with staff flags.
    """
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400

    ok, err = validate_payload(data, ['guild_id', 'name'])
    if not ok:
        return jsonify({'error': err}), 400

    guild_id = sanitize_str(data.get('guild_id'), 50)

    # Upsert GuildInfo
    guild = GuildInfo.query.filter_by(guild_id=guild_id).first()
    if not guild:
        guild = GuildInfo(guild_id=guild_id)
    guild.name = sanitize_str(data.get('name'), 200)
    guild.owner_id = sanitize_str(data.get('owner_id'), 50)
    guild.owner_name = sanitize_str(data.get('owner_name'), 200)
    guild.member_count = data.get('member_count', 0)
    guild.online_count = data.get('online_count', 0)
    guild.staff_count = data.get('staff_count', 0)
    guild.bot_count = data.get('bot_count', 0)
    guild.role_count = data.get('role_count', 0)
    guild.prefix = data.get('prefix', '["!ss "]')
    guild.scanned_at = datetime.utcnow()
    # Preserve content trust on re-scan
    if not guild.store_content:
        guild.store_content = data.get('store_content', False)
    db.session.add(guild)
    db.session.flush()

    # Upsert roles
    GuildRole.query.filter_by(guild_id=guild_id).delete()
    for r in data.get('roles', []):
        role = GuildRole(
            guild_id=guild_id,
            role_id=sanitize_str(r.get('role_id', ''), 50),
            name=sanitize_str(r.get('name', ''), 200),
            position=r.get('position', 0),
            color=r.get('color'),
            is_admin=r.get('is_admin', False),
            can_ban=r.get('can_ban', False),
            can_kick=r.get('can_kick', False),
            can_manage_messages=r.get('can_manage_messages', False),
            can_manage_guild=r.get('can_manage_guild', False),
            can_manage_roles=r.get('can_manage_roles', False),
            is_mod=r.get('is_mod', False),
            member_count=r.get('member_count', 0),
        )
        db.session.add(role)

    # Upsert members — preserve existing rows, update in place
    existing_members = {m.member_id: m for m in GuildMember.query.filter_by(guild_id=guild_id).all()}
    scanned_ids = set()
    for m in data.get('members', []):
        if not m.get('member_id') or not m.get('name'):
            continue
        member_id = m['member_id']
        scanned_ids.add(member_id)
        if member_id in existing_members:
            member = existing_members[member_id]
            member.name = sanitize_str(m.get('name', ''), 200)
            member.display_name = sanitize_str(m.get('display_name'), 200)
            member.is_bot = m.get('is_bot', False)
            member.is_owner = m.get('is_owner', False)
            member.is_staff = m.get('is_staff', False)
            member.role_ids = m.get('role_ids')
            member.top_role_position = m.get('top_role_position', 0)
            member.is_online = m.get('is_online', False)
            member.status = m.get('status', 'offline')
            member.activity_name = m.get('activity_name')
            member.activity_type = m.get('activity_type')
            if m.get('joined_at'):
                member.joined_at = datetime.fromisoformat(m['joined_at'])
        else:
            member = GuildMember(
                guild_id=guild_id,
                member_id=member_id,
                name=sanitize_str(m.get('name', ''), 200),
                display_name=sanitize_str(m.get('display_name'), 200),
                joined_at=datetime.fromisoformat(m['joined_at']) if m.get('joined_at') else None,
                is_bot=m.get('is_bot', False),
                is_owner=m.get('is_owner', False),
                is_staff=m.get('is_staff', False),
                role_ids=m.get('role_ids'),
                top_role_position=m.get('top_role_position', 0),
                is_online=m.get('is_online', False),
                status=m.get('status', 'offline'),
                activity_name=m.get('activity_name'),
                activity_type=m.get('activity_type'),
            )
        db.session.add(member)

    # Upsert channels
    GuildChannel.query.filter_by(guild_id=guild_id).delete()
    for ch in data.get('channels', []):
        if not ch.get('channel_id'):
            continue
        channel = GuildChannel(
            guild_id=guild_id,
            channel_id=sanitize_str(ch.get('channel_id', ''), 50),
            name=sanitize_str(ch.get('name', ''), 200),
            topic=sanitize_str(ch.get('topic'), 500),
            channel_type=sanitize_str(ch.get('channel_type', 'text'), 50),
            category=sanitize_str(ch.get('category'), 200),
            position=ch.get('position', 0),
            is_public=ch.get('is_public', True),
        )
        db.session.add(channel)

    # Upsert AutoMod rules
    AutoModRule.query.filter_by(guild_id=guild_id).delete()
    for ar in data.get('automod_rules', []):
        if not ar.get('rule_id'):
            continue
        rule = AutoModRule(
            guild_id=guild_id,
            rule_id=sanitize_str(ar.get('rule_id', ''), 50),
            name=sanitize_str(ar.get('name', ''), 200),
            creator_id=sanitize_str(ar.get('creator_id'), 50),
            creator_name=sanitize_str(ar.get('creator_name'), 100),
            trigger_type=sanitize_str(ar.get('trigger_type', 'unknown'), 50),
            trigger_text=sanitize_str(ar.get('trigger_text'), 500),
            action_type=sanitize_str(ar.get('action_type', 'unknown'), 50),
            enabled=ar.get('enabled', True),
            exempt_roles=sanitize_str(ar.get('exempt_roles'), 500),
            exempt_channels=sanitize_str(ar.get('exempt_channels'), 500),
            alert_channel_id=sanitize_str(ar.get('alert_channel_id'), 50),
        )
        db.session.add(rule)

    db.session.commit()

    print(f'[Observer API] Guild scan stored: {guild.name} — {guild.staff_count} staff, {guild.member_count} members, {len(data.get("channels", []))} channels, {len(data.get("automod_rules", []))} automod rules')
    return jsonify({'message': 'Guild scan stored', 'guild': guild.name, 'staff': guild.staff_count}), 201


@observer_bp.route('/observer/guilds', methods=['GET'])
@require_api_key
def list_guilds():
    """Lists all scanned guilds with summary stats."""
    guilds = GuildInfo.query.order_by(GuildInfo.name).all()
    return jsonify([{
        'guild_id': g.guild_id,
        'name': g.name,
        'prefixes': json.loads(g.prefix) if g.prefix else ['!ss '],
        'owner_name': g.owner_name,
        'member_count': g.member_count,
        'online_count': g.online_count,
        'staff_count': g.staff_count,
        'bot_count': g.bot_count,
        'role_count': g.role_count,
        'scanned_at': g.scanned_at.isoformat() if g.scanned_at else None,
        'store_content': g.store_content,
    } for g in guilds])


@observer_bp.route('/observer/guilds/<guild_id>/members', methods=['GET'])
@require_api_key
def list_guild_members(guild_id):
    """Lists members of a guild, split by staff/non-staff."""
    staff_only = request.args.get('staff', '').lower() == 'true'
    bots = request.args.get('bots', '').lower() == 'true'

    query = GuildMember.query.filter_by(guild_id=guild_id)
    if staff_only:
        query = query.filter_by(is_staff=True)
    if not bots:
        query = query.filter_by(is_bot=False)

    members = query.order_by(GuildMember.top_role_position.desc(), GuildMember.name).all()
    return jsonify([{
        'member_id': m.member_id,
        'name': m.name,
        'display_name': m.display_name,
        'is_bot': m.is_bot,
        'is_owner': m.is_owner,
        'is_staff': m.is_staff,
        'is_manually_set': m.is_manually_set,
        'role_ids': m.role_ids.split(',') if m.role_ids else [],
        'top_role_position': m.top_role_position,
        'total_messages': m.total_messages,
        'is_online': m.is_online,
        'status': m.status,
        'activity_name': m.activity_name,
        'activity_type': m.activity_type,
        'last_seen_online': m.last_seen_online.isoformat() if m.last_seen_online else None,
        'last_message_at': m.last_message_at.isoformat() if m.last_message_at else None,
    } for m in members])


@observer_bp.route('/observer/guilds/<guild_id>/roles', methods=['GET'])
@require_api_key
def list_guild_roles(guild_id):
    """Lists roles of a guild."""
    mods_only = request.args.get('mods', '').lower() == 'true'

    query = GuildRole.query.filter_by(guild_id=guild_id)
    if mods_only:
        query = query.filter_by(is_mod=True)

    roles = query.order_by(GuildRole.position.desc()).all()
    return jsonify([{
        'role_id': r.role_id,
        'name': r.name,
        'position': r.position,
        'color': r.color,
        'is_admin': r.is_admin,
        'can_ban': r.can_ban,
        'can_kick': r.can_kick,
        'can_manage_messages': r.can_manage_messages,
        'can_manage_guild': r.can_manage_guild,
        'can_manage_roles': r.can_manage_roles,
        'is_mod': r.is_mod,
        'member_count': r.member_count,
    } for r in roles])


@observer_bp.route('/observer/automod-rules', methods=['GET'])
@require_api_key
def list_all_automod_rules():
    """Lists all AutoMod rules across all guilds (for bot startup)."""
    rules = AutoModRule.query.filter(AutoModRule.alert_channel_id != None).order_by(AutoModRule.guild_id, AutoModRule.name).all()
    return jsonify([{
        'guild_id': r.guild_id,
        'rule_id': r.rule_id,
        'name': r.name,
        'alert_channel_id': r.alert_channel_id,
        'trigger_type': r.trigger_type,
        'action_type': r.action_type,
    } for r in rules])


@observer_bp.route('/observer/guilds/<guild_id>/automod', methods=['GET'])
@require_api_key
def list_guild_automod(guild_id):
    """Lists AutoMod rules for a guild."""
    rules = AutoModRule.query.filter_by(guild_id=guild_id).order_by(AutoModRule.created_at.desc()).all()
    return jsonify([{
        'rule_id': r.rule_id,
        'name': r.name,
        'creator_name': r.creator_name,
        'trigger_type': r.trigger_type,
        'trigger_text': r.trigger_text,
        'action_type': r.action_type,
        'alert_channel_id': r.alert_channel_id,
        'enabled': r.enabled,
        'exempt_roles': r.exempt_roles.split(',') if r.exempt_roles else [],
        'exempt_channels': r.exempt_channels.split(',') if r.exempt_channels else [],
    } for r in rules])


@observer_bp.route('/observer/guilds/<guild_id>/trust', methods=['GET', 'POST'])
@require_api_key
def guild_trust(guild_id):
    """Get or toggle content storage trust for a guild."""
    guild = GuildInfo.query.filter_by(guild_id=guild_id).first_or_404()

    if request.method == 'POST':
        data = request.json or {}
        enabled = data.get('store_content', not guild.store_content)
        guild.store_content = bool(enabled)
        db.session.commit()
        status = 'enabled' if guild.store_content else 'disabled'
        print(f'[Observer API] Content storage {status} for {guild.name}')
        return jsonify({'guild_id': guild_id, 'store_content': guild.store_content})

    return jsonify({'guild_id': guild_id, 'store_content': guild.store_content, 'name': guild.name})


@observer_bp.route('/observer/guilds/<guild_id>/prefix', methods=['GET', 'PATCH'])
@require_api_key
def guild_prefix(guild_id):
    """Get or set prefixes for a guild. Stores as JSON array."""
    guild = GuildInfo.query.filter_by(guild_id=guild_id).first_or_404()

    if request.method == 'PATCH':
        data = request.json
        if not data:
            return jsonify({'error': 'No JSON body'}), 400
        new_prefixes = data.get('prefixes', ['!ss '])
        for p in new_prefixes:
            if len(p) > 10:
                return jsonify({'error': f'Prefix "{p}" too long (max 10 chars)'}), 400
        guild.prefix = json.dumps(new_prefixes)
        db.session.commit()
        print(f'[Observer API] Prefixes set for {guild.name}: {new_prefixes}')
        return jsonify({'guild_id': guild_id, 'prefixes': new_prefixes})

    prefixes = json.loads(guild.prefix) if guild.prefix else ['!ss ']
    return jsonify({'guild_id': guild_id, 'prefixes': prefixes})


@observer_bp.route('/observer/presence', methods=['POST'])
@require_api_key
def receive_presence():
    """
    Receives real-time presence updates from the bot.
    Upserts GuildMember rows with current online status, activity, and last_seen_online.
    This is the mechanism that builds and maintains the persistent member registry.
    """
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400

    updates = data.get('updates', [data])
    now = datetime.utcnow()
    updated = 0
    created = 0

    for p in updates:
        guild_id = sanitize_str(p.get('guild_id'), 50)
        member_id = sanitize_str(p.get('member_id'), 50)
        if not guild_id or not member_id:
            continue

        member = GuildMember.query.filter_by(guild_id=guild_id, member_id=member_id).first()
        if member:
            member.is_online = p.get('is_online', False)
            member.status = p.get('status', 'offline')
            member.last_seen_online = now if p.get('is_online', False) else member.last_seen_online
            member.activity_name = p.get('activity_name')
            member.activity_type = p.get('activity_type')
            if p.get('name'):
                member.name = sanitize_str(p['name'], 200)
            if p.get('display_name'):
                member.display_name = sanitize_str(p['display_name'], 200)
            if p.get('joined_at'):
                try:
                    member.joined_at = datetime.fromisoformat(p['joined_at'])
                except (ValueError, TypeError):
                    pass
            updated += 1
        else:
            member = GuildMember(
                guild_id=guild_id,
                member_id=member_id,
                name=sanitize_str(p.get('name', 'Unknown'), 200),
                display_name=sanitize_str(p.get('display_name'), 200),
                is_bot=p.get('is_bot', False),
                is_online=p.get('is_online', False),
                status=p.get('status', 'offline'),
                last_seen_online=now if p.get('is_online', False) else None,
                activity_name=p.get('activity_name'),
                activity_type=p.get('activity_type'),
            )
            created += 1
        db.session.add(member)

    db.session.commit()
    print(f'[Presence API] {updated} updated, {created} created')
    return jsonify({'updated': updated, 'created': created}), 200


@observer_bp.route('/observer/seed-online', methods=['GET'])
@require_api_key
def seed_online():
    """
    Returns online member IDs per guild for seeding the bot's online_members set.
    Only includes members seen online in the last 30 minutes to avoid stale counts.
    Response: { guild_id: [member_id_int, ...], ... }
    """
    cutoff = datetime.utcnow() - timedelta(minutes=30)
    guilds = GuildInfo.query.all()
    result = {}
    for g in guilds:
        members = GuildMember.query.filter(
            GuildMember.guild_id == g.guild_id,
            GuildMember.is_online == True,
            GuildMember.last_seen_online >= cutoff
        ).all()
        if members:
            result[g.guild_id] = [int(m.member_id) for m in members]
    print(f'[SeedOnline] Returned online members for {len(result)} guilds (last 30min)')
    return jsonify(result), 200


@observer_bp.route('/observer/online-count', methods=['POST'])
@require_api_key
def receive_online_count():
    """
    Receives live online member counts per guild from the bot's presence tracking.
    Updates GuildInfo.online_count in DB for dashboard display.
    Payload: { guild_id: count, ... }
    """
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400
    updated = 0
    for guild_id_str, count in data.items():
        if not isinstance(count, int):
            continue
        guild = GuildInfo.query.filter_by(guild_id=sanitize_str(guild_id_str, 50)).first()
        if guild:
            guild.online_count = count
            updated += 1
    db.session.commit()
    print(f'[OnlineCount] Updated {updated} guilds')
    return jsonify({'updated': updated}), 200


# ─────────────────────────────────────────────
# BEHAVIORAL ANOMALY DETECTION
# ─────────────────────────────────────────────

def detect_anomalies_for_user(discord_id, name=None):
    """Run anomaly detection for a single user. Returns list of anomalies found."""
    now = datetime.utcnow()
    anomalies = []

    all_msgs = MessageRecord.query.filter_by(discord_id=discord_id).order_by(MessageRecord.created_at).all()
    if len(all_msgs) < 10:
        return []

    # ── Baseline: messages older than 24h ──
    cutoff = now - timedelta(hours=24)
    baseline_msgs = [m for m in all_msgs if m.created_at < cutoff]
    lengths = [m.message_length for m in baseline_msgs if m.message_length]
    hours = [m.hour_of_day for m in baseline_msgs if m.hour_of_day is not None]

    baseline_avg_len = sum(lengths) / len(lengths) if lengths else 0
    baseline_std_len = math.sqrt(sum((x - baseline_avg_len) ** 2 for x in lengths) / len(lengths)) if len(lengths) > 1 else 0
    active_hours = set(hours)

    # Daily baseline
    if baseline_msgs:
        days_span = max(1, (baseline_msgs[-1].created_at - baseline_msgs[0].created_at).days or 1)
        daily_avg = len(baseline_msgs) / days_span
    else:
        days_span = 1
        daily_avg = 0
    # Approximate daily std from count variance across hours
    hour_counts = defaultdict(int)
    for h in hours:
        hour_counts[h] += 1

    # ── Recent: last 24h ──
    recent = [m for m in all_msgs if m.created_at >= cutoff]
    if not recent:
        return []

    recent_lengths = [m.message_length for m in recent if m.message_length]
    recent_hours = [m.hour_of_day for m in recent if m.hour_of_day is not None]
    recent_count = len(recent)

    anomalies_found = []

    # 1. Odd hours — user posted in hours they never did before
    new_hours = set(recent_hours) - active_hours
    for h in new_hours:
        anomalies_found.append({
            'type': 'odd_hours',
            'severity': 60,
            'details': f'Posted at hour {h}:00, a time they have never posted before'
        })

    # 2. Volume spike/drop — recent 24h count vs daily avg
    if daily_avg > 1 and len(all_msgs) > 20:
        deviation = (recent_count - daily_avg) / max(daily_avg, 0.1)
        if deviation > 2.0:
            anomalies_found.append({
                'type': 'volume_spike',
                'severity': min(90, 50 + deviation * 10),
                'details': f'{recent_count} messages in last 24h vs daily avg of {daily_avg:.1f} ({deviation:.1f}x spike)'
            })
        elif deviation < -0.8:
            anomalies_found.append({
                'type': 'volume_drop',
                'severity': min(80, 40 + abs(deviation) * 15),
                'details': f'Only {recent_count} messages in last 24h vs daily avg of {daily_avg:.1f} ({abs(deviation):.0f}% of normal)'
            })

    # 3. Message length shift
    if baseline_std_len > 5 and len(recent_lengths) >= 5:
        recent_avg_len = sum(recent_lengths) / len(recent_lengths)
        z_score = abs(recent_avg_len - baseline_avg_len) / max(baseline_std_len, 1)
        if z_score > 2.0:
            direction = 'longer' if recent_avg_len > baseline_avg_len else 'shorter'
            anomalies_found.append({
                'type': 'length_shift',
                'severity': min(70, 40 + z_score * 8),
                'details': f'Messages {direction} than usual ({recent_avg_len:.0f} chars vs baseline {baseline_avg_len:.0f}, z={z_score:.1f})'
            })

    return anomalies_found


@observer_bp.route('/observer/anomalies/scan', methods=['POST'])
@require_api_key
def scan_anomalies():
    """Scan all tracked users for behavioral anomalies."""
    now = datetime.utcnow()
    discord_ids = db.session.query(MessageRecord.discord_id, MessageRecord.name).distinct().all()

    total_flagged = 0
    for discord_id, name in discord_ids:
        anomalies = detect_anomalies_for_user(discord_id, name)
        for a in anomalies:
            existing = BehavioralAnomaly.query.filter_by(
                discord_id=discord_id, anomaly_type=a['type'], cleared_at=None
            ).filter(BehavioralAnomaly.detected_at > now - timedelta(hours=12)).first()
            if not existing:
                record = BehavioralAnomaly(
                    discord_id=discord_id,
                    name=name,
                    anomaly_type=a['type'],
                    severity=a['severity'],
                    details=a['details'],
                    source='discord',
                )
                db.session.add(record)
                total_flagged += 1

    db.session.commit()
    return jsonify({'scanned_users': len(discord_ids), 'new_anomalies': total_flagged})


@observer_bp.route('/observer/anomalies', methods=['GET'])
@require_api_key
def list_anomalies():
    """Return active anomalies, optionally filtered by severity."""
    min_sev = request.args.get('min_severity', 0, type=float)
    anomalies = BehavioralAnomaly.query.filter_by(cleared_at=None).filter(BehavioralAnomaly.severity >= min_sev).order_by(BehavioralAnomaly.severity.desc()).limit(50).all()
    return jsonify([{
        'id': a.id,
        'discord_id': a.discord_id,
        'name': a.name,
        'type': a.anomaly_type,
        'severity': a.severity,
        'details': a.details,
        'detected_at': a.detected_at.isoformat(),
    } for a in anomalies])


@observer_bp.route('/observer/anomalies/<discord_id>', methods=['GET'])
@require_api_key
def user_anomalies(discord_id):
    """Return active anomalies for a specific user."""
    anomalies = BehavioralAnomaly.query.filter_by(discord_id=discord_id, cleared_at=None).order_by(BehavioralAnomaly.severity.desc()).all()
    return jsonify([{
        'id': a.id,
        'type': a.anomaly_type,
        'severity': a.severity,
        'details': a.details,
        'detected_at': a.detected_at.isoformat(),
    } for a in anomalies])


# ─────────────────────────────────────────────
# MESSAGE RETENTION CLEANUP
# ─────────────────────────────────────────────

@observer_bp.route('/observer/cleanup', methods=['POST'])
@require_api_key
def cleanup_old_messages():
    """Delete messages and mentions older than the retention period."""
    data = request.json or {}
    retention_days = int(data.get('retention_days', 90))
    retention_days = max(7, min(365, retention_days))
    cutoff = datetime.utcnow() - timedelta(days=retention_days)

    deleted_msgs = MessageRecord.query.filter(MessageRecord.created_at < cutoff).delete()
    deleted_mentions = MentionRecord.query.filter(MentionRecord.created_at < cutoff).delete()
    db.session.commit()

    return jsonify({
        'deleted': deleted_msgs,
        'deleted_mentions': deleted_mentions,
        'retention_days': retention_days,
    })


# ─────────────────────────────────────────────
# PING → JOIN EVENT TRACKING
# ─────────────────────────────────────────────

@observer_bp.route('/observer/ping-join', methods=['POST'])
@require_api_key
def log_ping_join():
    """Records when a moderator's @everyone ping led to new member joins within 20 min."""
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400

    ok, err = validate_payload(data, ['moderator_id', 'moderator_name', 'guild_id'])
    if not ok:
        return jsonify({'error': err}), 400

    event = PingJoinEvent(
        guild_id=sanitize_str(data['guild_id'], 50),
        guild_name=sanitize_str(data.get('guild_name'), 100),
        moderator_id=sanitize_str(data['moderator_id'], 50),
        moderator_name=sanitize_str(data['moderator_name'], 100),
        channel=sanitize_str(data.get('channel'), 100),
        new_members=int(data.get('new_members', 0)),
        joiners=sanitize_str(data.get('joiners'), 500),
    )
    db.session.add(event)
    db.session.commit()

    print(f'[PingJoin API] {event.moderator_name} pinged @everyone → +{event.new_members} joins')
    return jsonify({'message': 'Ping-join event logged', 'id': event.id}), 201


# ─────────────────────────────────────────────
# VOICE ACTIVITY TRACKING
# ─────────────────────────────────────────────

@observer_bp.route('/observer/voice-activity', methods=['POST'])
@require_api_key
def log_voice_activity():
    """Batch-log voice channel sessions for behavioral pattern recognition."""
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400

    sessions = data.get('sessions', [data])
    if not sessions:
        return jsonify({'error': 'No sessions provided'}), 400

    created = 0
    for s in sessions:
        entry = VoiceActivity(
            discord_id=sanitize_str(s.get('discord_id', ''), 50),
            name=sanitize_str(s.get('name'), 100),
            guild_id=sanitize_str(s.get('guild_id', ''), 50),
            guild_name=sanitize_str(s.get('guild_name'), 100),
            channel_name=sanitize_str(s.get('channel_name'), 100),
            duration_seconds=float(s.get('duration_seconds', 0)),
            hour_of_day=int(s['hour_of_day']) if s.get('hour_of_day') is not None else None,
            day_of_week=int(s['day_of_week']) if s.get('day_of_week') is not None else None,
            joined_at=datetime.fromisoformat(s['joined_at']) if s.get('joined_at') else None,
            left_at=datetime.fromisoformat(s['left_at']) if s.get('left_at') else None,
        )
        db.session.add(entry)
        created += 1

    db.session.commit()
    print(f'[Voice API] Logged {created} voice sessions')
    return jsonify({'message': f'{created} voice sessions logged', 'created': created}), 201


# ─────────────────────────────────────────────
# BURNOUT RISK SCANNING
# ─────────────────────────────────────────────

@observer_bp.route('/observer/burnout-scan', methods=['POST'])
@require_api_key
def scan_burnout_risks():
    """Scan all workers for burnout risk indicators (anomalies + voice + reversals)."""
    now = datetime.utcnow()
    workers = Worker.query.filter(Worker.discord_id != None).all()
    thirty_days = timedelta(days=30)
    fourteen_days = timedelta(days=14)
    cutoff_30 = now - thirty_days
    cutoff_14 = now - fourteen_days

    results = []
    for w in workers:
        did = w.discord_id
        # Anomaly frequency (last 30 days)
        anomaly_count = BehavioralAnomaly.query.filter(
            BehavioralAnomaly.discord_id == did,
            BehavioralAnomaly.detected_at >= cutoff_30
        ).count()
        anomaly_freq = min(1.0, anomaly_count / 10)

        # Voice session creep: compare last 14 days avg vs prior 14
        voice_recent = db.session.query(func.avg(VoiceActivity.duration_seconds)).filter(
            VoiceActivity.discord_id == did,
            VoiceActivity.created_at >= cutoff_14
        ).scalar() or 0
        voice_prior = db.session.query(func.avg(VoiceActivity.duration_seconds)).filter(
            VoiceActivity.discord_id == did,
            VoiceActivity.created_at >= cutoff_30,
            VoiceActivity.created_at < cutoff_14
        ).scalar() or 0
        voice_creep = 0
        if voice_prior > 0 and voice_recent > voice_prior:
            voice_creep = min(1.0, (voice_recent - voice_prior) / voice_prior)

        # Reversal rate increase (last 14 days vs prior 14)
        reversals_recent = ScoreLog.query.filter(
            ScoreLog.worker_id == w.id,
            ScoreLog.source == 'discord',
            ScoreLog.change < 0,
            ScoreLog.created_at >= cutoff_14
        ).count()
        reversals_prior = ScoreLog.query.filter(
            ScoreLog.worker_id == w.id,
            ScoreLog.source == 'discord',
            ScoreLog.change < 0,
            ScoreLog.created_at >= cutoff_30,
            ScoreLog.created_at < cutoff_14
        ).count()
        reversal_risk = 0
        if reversals_prior > 0 and reversals_recent > reversals_prior:
            reversal_risk = min(1.0, (reversals_recent - reversals_prior) / reversals_prior)

        # Volume volatility: recent anomaly presence
        has_volume_anomaly = BehavioralAnomaly.query.filter(
            BehavioralAnomaly.discord_id == did,
            BehavioralAnomaly.anomaly_type.in_(['volume_spike', 'volume_drop']),
            BehavioralAnomaly.detected_at >= cutoff_14,
            BehavioralAnomaly.cleared_at == None
        ).count() > 0
        volume_volatility = 0.6 if has_volume_anomaly else 0

        # Composite burnout score (0-100)
        score = (anomaly_freq * 30) + (volume_volatility * 25) + (reversal_risk * 25) + (voice_creep * 20)
        score = round(min(100, score), 1)

        signals = []
        if anomaly_freq > 0.3: signals.append('frequent_anomalies')
        if has_volume_anomaly: signals.append('volume_volatility')
        if reversal_risk > 0.3: signals.append('increasing_reversals')
        if voice_creep > 0.3: signals.append('voice_creep')

        # Upsert
        existing = BurnoutRisk.query.filter_by(worker_id=w.id).first()
        if existing:
            existing.score = score
            existing.anomaly_freq = anomaly_freq
            existing.volume_volatility = volume_volatility
            existing.reversal_rate = reversal_risk
            existing.voice_creep = voice_creep
            existing.signals = ','.join(signals) if signals else None
            existing.detected_at = now
        else:
            br = BurnoutRisk(
                worker_id=w.id, discord_id=did, name=w.name,
                score=score, anomaly_freq=anomaly_freq,
                volume_volatility=volume_volatility,
                reversal_rate=reversal_risk, voice_creep=voice_creep,
                signals=','.join(signals) if signals else None,
            )
            db.session.add(br)

        results.append({'name': w.name, 'score': score, 'signals': signals})

    db.session.commit()
    flagged = [r for r in results if r['score'] >= 25]
    return jsonify({'scanned': len(results), 'flagged': len(flagged), 'results': results})


# ─────────────────────────────────────────────
# ML ENGINE ENDPOINTS
# ─────────────────────────────────────────────

@observer_bp.route('/observer/ml/retrain', methods=['POST'])
@require_api_key
def ml_retrain():
    """Retrain all ML models (consumes any pending retrain request)."""
    data = request.json or {}
    days = int(data.get('days', 30))
    min_msgs = int(data.get('min_msgs', 10))
    results = ml_engine.train_all(days=days, min_msgs=min_msgs)
    # Consume the correction-triggered retrain flag if present
    consume_retrain_request()
    return jsonify(results)


@observer_bp.route('/observer/ml/status', methods=['GET'])
@require_api_key
def ml_status():
    """Get training status of all ML models."""
    status = ml_engine.get_model_status()
    summary_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'ml', 'models', 'training_summary.json')
    last_train = None
    if os.path.exists(summary_path):
        try:
            with open(summary_path) as f:
                last_train = json.load(f).get('trained_at')
        except Exception:
            pass
    return jsonify({'models': status, 'last_training': last_train})


@observer_bp.route('/observer/ml/forecast/<guild_id>', methods=['GET', 'POST'])
@require_api_key
def ml_forecast_guild(guild_id):
    """Get 24h activity forecast for a guild."""
    preds = ml_forecast.predict_next_24h(guild_id)
    if preds is None:
        return jsonify({'error': 'No forecast available for this guild'}), 404
    return jsonify({
        'guild_id': guild_id,
        'forecast': preds.tolist(),
        'hours': list(range(24)),
    })


@observer_bp.route('/observer/ml/resolve', methods=['POST'])
@require_api_key
def ml_resolve_outcomes():
    """Resolve pending predictions — compare forecast vs actual."""
    days_back = request.json.get('days_back', 7) if request.is_json else 7
    from ml.forecast import resolve_outcomes
    resolved = resolve_outcomes(days_back=days_back)
    return jsonify({'resolved': resolved})


@observer_bp.route('/observer/ml/accuracy', methods=['GET'])
@require_api_key
def ml_accuracy():
    """Return accuracy metrics for all ML models over trailing N days."""
    days = request.args.get('days', 7, type=int)
    from ml.engine import get_all_accuracy_metrics
    metrics = get_all_accuracy_metrics(days=days)
    return jsonify(metrics)


@observer_bp.route('/observer/ml/anomalies/scan', methods=['POST'])
@require_api_key
def ml_scan_anomalies():
    """Run ML-based anomaly detection across all users.
    Replaces rule-based detect_anomalies_for_user with Isolation Forest."""
    now = datetime.utcnow()
    anomalies = ml_anomaly.scan_all()
    total_new = 0
    for a in anomalies:
        existing = BehavioralAnomaly.query.filter_by(
            discord_id=a['discord_id'], anomaly_type='ml_anomaly', cleared_at=None
        ).filter(BehavioralAnomaly.detected_at > now - timedelta(hours=12)).first()
        if not existing:
            record = BehavioralAnomaly(
                discord_id=a['discord_id'],
                anomaly_type='ml_anomaly',
                severity=a['severity'],
                details=f'ML isolation forest anomaly (score: {a["anomaly_score"]})',
                source='discord',
            )
            db.session.add(record)
            total_new += 1
    db.session.commit()
    return jsonify({'scanned': len(anomalies), 'new_anomalies': total_new})


@observer_bp.route('/observer/ml/burnout-scan', methods=['POST'])
@require_api_key
def ml_scan_burnout():
    """Run ML-based burnout risk detection across all workers.
    Replaces heuristic scoring with Isolation Forest on staff feature vectors."""
    now = datetime.utcnow()
    flagged = ml_burnout.scan_all()
    from database import Worker, BurnoutRisk

    updated = 0
    for fb in flagged:
        existing = BurnoutRisk.query.filter_by(worker_id=fb['worker_id']).first()
        signals_str = ','.join(fb['signals']) if fb['signals'] else None
        if existing:
            existing.score = fb['burnout_score']
            existing.anomaly_freq = 0  # Not used in ML mode
            existing.volume_volatility = 0
            existing.reversal_rate = 0
            existing.voice_creep = 0
            existing.signals = signals_str
            existing.detected_at = now
        else:
            w = db.session.get(Worker, fb['worker_id'])
            br = BurnoutRisk(
                worker_id=fb['worker_id'], discord_id=fb['discord_id'], name=fb['name'],
                score=fb['burnout_score'], signals=signals_str,
            )
            db.session.add(br)
        updated += 1
    db.session.commit()
    return jsonify({'scanned': len(flagged), 'updated': updated, 'flagged': flagged})


@observer_bp.route('/observer/ml/anomalies/feedback', methods=['POST'])
@require_api_key
def ml_anomaly_feedback():
    """Submit admin feedback on an anomaly prediction (confirm or dismiss)."""
    data = request.json or {}
    ok, err = validate_payload(data, ['anomaly_id', 'feedback'])
    if not ok:
        return jsonify({'error': err}), 400
    feedback = data['feedback']
    if feedback not in ('confirmed', 'dismissed'):
        return jsonify({'error': 'feedback must be "confirmed" or "dismissed"'}), 400
    anomaly = db.session.get(BehavioralAnomaly, int(data['anomaly_id']))
    if not anomaly:
        return jsonify({'error': 'Anomaly not found'}), 404
    anomaly.feedback = feedback
    anomaly.feedback_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'status': 'ok', 'feedback': feedback, 'anomaly_id': anomaly.id})


@observer_bp.route('/observer/ml/anomalies/precision-recall', methods=['GET'])
@require_api_key
def ml_anomaly_precision_recall():
    """Get precision/recall metrics for ML anomaly detection."""
    days = request.args.get('days', 30, type=int)
    return jsonify(ml_anomaly.get_precision_recall(days=days))


@observer_bp.route('/observer/ml/burnout/feedback', methods=['POST'])
@require_api_key
def ml_burnout_feedback():
    """Submit admin feedback on a burnout risk prediction (confirm or dismiss)."""
    from database import BurnoutRisk
    data = request.json or {}
    ok, err = validate_payload(data, ['risk_id', 'feedback'])
    if not ok:
        return jsonify({'error': err}), 400
    feedback = data['feedback']
    if feedback not in ('confirmed', 'dismissed'):
        return jsonify({'error': 'feedback must be "confirmed" or "dismissed"'}), 400
    risk = db.session.get(BurnoutRisk, int(data['risk_id']))
    if not risk:
        return jsonify({'error': 'Burnout risk record not found'}), 404
    risk.feedback = feedback
    risk.feedback_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'status': 'ok', 'feedback': feedback, 'risk_id': risk.id})


@observer_bp.route('/observer/ml/burnout/precision-recall', methods=['GET'])
@require_api_key
def ml_burnout_precision_recall():
    """Get precision metrics for burnout risk predictions."""
    days = request.args.get('days', 30, type=int)
    return jsonify(ml_burnout.get_precision_recall(days=days))


@observer_bp.route('/observer/ml/request-retrain', methods=['POST'])
@require_api_key
def ml_request_retrain():
    """Signal that a correction-feedback retrain is needed."""
    _set_retrain_flag()
    return jsonify({'status': 'retrain_requested', 'at': datetime.utcnow().isoformat()})


@observer_bp.route('/observer/ml/pending-retrain', methods=['GET'])
@require_api_key
def ml_pending_retrain():
    """Check if a correction-triggered retrain is pending."""
    if os.path.exists(_RETRAIN_FLAG_FILE):
        try:
            with open(_RETRAIN_FLAG_FILE) as f:
                ts = f.read().strip()
            return jsonify({'pending': True, 'requested_at': ts})
        except Exception:
            pass
    return jsonify({'pending': False})


@observer_bp.route('/observer/ml/federated/train', methods=['POST'])
@require_api_key
def ml_federated_train():
    """Run one round of FedAvg over guild-partitioned message data."""
    data = request.json or {}
    days = int(data.get('days', 30))
    result = ml_federated.train_federated(days=days)
    return jsonify(result)


@observer_bp.route('/observer/ml/federated', methods=['GET'])
@require_api_key
def ml_federated_status():
    """Get federated learning training status and history."""
    return jsonify(ml_federated.get_status())


@observer_bp.route('/observer/role-change', methods=['POST'])
@require_api_key
def log_role_change():
    """Log a staff role change (promotion/demotion/retirement) and award points."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'No JSON payload'}), 400

    ok, err = validate_payload(data, ['guild_id', 'member_id', 'member_name', 'change_type', 'role_id', 'role_name', 'change_category'])
    if not ok:
        return jsonify({'error': err}), 400

    record = RoleChangeLog(
        guild_id=sanitize_str(data['guild_id'], 50),
        member_id=sanitize_str(data['member_id'], 50),
        member_name=sanitize_str(data['member_name'], 100),
        change_type=sanitize_str(data['change_type'], 20),
        role_id=sanitize_str(data['role_id'], 50),
        role_name=sanitize_str(data['role_name'], 100),
        change_category=sanitize_str(data['change_category'], 30),
        was_staff_before=bool(data.get('was_staff_before', False)),
        is_staff_now=bool(data.get('is_staff_now', False)),
        modifier_id=sanitize_str(data.get('modifier_id'), 50),
        modifier_name=sanitize_str(data.get('modifier_name'), 100),
    )
    db.session.add(record)

    # Award points to the modifier (staff who changed the role)
    modifier_id = data.get('modifier_id')
    category = data.get('change_category')
    points_awarded = 0
    reason = None

    if modifier_id:
        modifier = Worker.query.filter_by(discord_id=str(modifier_id)).first()
        if modifier:
            if category == 'promotion':
                points_awarded = 5
                reason = f'Promoted {data["member_name"]} to staff ({data["role_name"]})'
            elif category == 'demotion':
                points_awarded = 3
                reason = f'Demoted {data["member_name"]} from staff ({data["role_name"]})'
            elif category == 'retirement':
                points_awarded = 2
                reason = f'Retired {data["member_name"]} from staff ({data["role_name"]})'
            elif category == 'other':
                points_awarded = 1
                reason = f'Role change for {data["member_name"]}: {data["role_name"]}'

            if points_awarded and reason:
                log = ScoreLog(
                    worker_id=modifier.id,
                    change=points_awarded,
                    reason=reason,
                    source='discord',
                    guild_id=sanitize_str(data['guild_id'], 50),
                )
                db.session.add(log)

    db.session.commit()
    return jsonify({'status': 'logged', 'id': record.id, 'points_awarded': points_awarded})


@observer_bp.route('/observer/join-leave', methods=['POST'])
@require_api_key
def log_join_leave():
    """Log member join and leave events for pattern recognition and ML growth prediction."""
    data = request.json or {}
    events = data.get('events', [data])
    
    for event in events:
        try:
            event_type = event.get('event_type')
            if event_type not in ['join', 'leave']:
                continue
                
            record = MemberJoinLeave(
                guild_id=sanitize_str(event.get('guild_id'), 50),
                member_id=sanitize_str(event.get('member_id'), 50),
                member_name=sanitize_str(event.get('member_name'), 100),
                is_bot=bool(event.get('is_bot', False)),
                event_type=event_type,
                leave_reason=sanitize_str(event.get('leave_reason'), 50),
                hour_of_day=event.get('hour_of_day'),
                day_of_week=event.get('day_of_week'),
            )
            db.session.add(record)
        except Exception as e:
            log(f'Error storing join-leave event: {e}')
            continue
    
    db.session.commit()
    print(f'[Observer API] Stored {len(events)} join/leave events')
    return jsonify({'message': f'Stored {len(events)} join/leave events'}), 201


@observer_bp.route('/observer/join-leave', methods=['GET'])
@require_api_key
def get_join_leave():
    """Get paginated join/leave events with filters."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    guild_id = request.args.get('guild_id')
    event_type = request.args.get('event_type')
    member_id = request.args.get('member_id')
    
    query = MemberJoinLeave.query.order_by(MemberJoinLeave.created_at.desc())
    
    if guild_id:
        query = query.filter(MemberJoinLeave.guild_id == guild_id)
    if event_type:
        query = query.filter(MemberJoinLeave.event_type == event_type)
    if member_id:
        query = query.filter(MemberJoinLeave.member_id == member_id)
    
    paginated = paginate_query(query, page, per_page)
    
    return jsonify({
        'page': paginated['page'],
        'per_page': paginated['per_page'],
        'total': paginated['total'],
        'total_pages': paginated['total_pages'],
        'has_next': paginated['has_next'],
        'has_prev': paginated['has_prev'],
        'items': [{
            'id': e.id,
            'guild_id': e.guild_id,
            'member_id': e.member_id,
            'member_name': e.member_name,
            'is_bot': e.is_bot,
            'event_type': e.event_type,
            'leave_reason': e.leave_reason,
            'hour_of_day': e.hour_of_day,
            'day_of_week': e.day_of_week,
            'created_at': e.created_at.isoformat() if e.created_at else None
        } for e in paginated['items']]
    })


@observer_bp.route('/observer/join-leave/stats', methods=['GET'])
@require_api_key
def get_join_leave_stats():
    """Get join/leave statistics for guild growth analysis."""
    guild_id = request.args.get('guild_id')
    days = int(request.args.get('days', 7))
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    base_query = MemberJoinLeave.query.filter(MemberJoinLeave.created_at >= cutoff)
    if guild_id:
        base_query = base_query.filter(MemberJoinLeave.guild_id == guild_id)
    
    total_joins = base_query.filter(MemberJoinLeave.event_type == 'join').count()
    total_leaves = base_query.filter(MemberJoinLeave.event_type == 'leave').count()
    
    # Hourly breakdown
    hourly_joins = db.session.query(
        MemberJoinLeave.hour_of_day, func.count(MemberJoinLeave.id)
    ).filter(
        MemberJoinLeave.created_at >= cutoff,
        MemberJoinLeave.event_type == 'join'
    )
    if guild_id:
        hourly_joins = hourly_joins.filter(MemberJoinLeave.guild_id == guild_id)
    hourly_joins = hourly_joins.group_by(MemberJoinLeave.hour_of_day).all()
    
    hourly_leaves = db.session.query(
        MemberJoinLeave.hour_of_day, func.count(MemberJoinLeave.id)
    ).filter(
        MemberJoinLeave.created_at >= cutoff,
        MemberJoinLeave.event_type == 'leave'
    )
    if guild_id:
        hourly_leaves = hourly_leaves.filter(MemberJoinLeave.guild_id == guild_id)
    hourly_leaves = hourly_leaves.group_by(MemberJoinLeave.hour_of_day).all()
    
    # Leave reasons
    leave_reasons = db.session.query(
        MemberJoinLeave.leave_reason, func.count(MemberJoinLeave.id)
    ).filter(
        MemberJoinLeave.created_at >= cutoff,
        MemberJoinLeave.event_type == 'leave',
        MemberJoinLeave.leave_reason != None
    )
    if guild_id:
        leave_reasons = leave_reasons.filter(MemberJoinLeave.guild_id == guild_id)
    leave_reasons = leave_reasons.group_by(MemberJoinLeave.leave_reason).all()
    
    return jsonify({
        'guild_id': guild_id,
        'days': days,
        'total_joins': total_joins,
        'total_leaves': total_leaves,
        'net_growth': total_joins - total_leaves,
        'hourly_joins': {str(h): c for h, c in hourly_joins},
        'hourly_leaves': {str(h): c for h, c in hourly_leaves},
        'leave_reasons': {r: c for r, c in leave_reasons},
    })

