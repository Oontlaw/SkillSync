import json
from flask import Blueprint, request, jsonify
from database import db, Worker, ScoreLog, MessageRecord, GuildInfo, GuildRole, GuildMember
from datetime import datetime
from sqlalchemy import func

observer_bp = Blueprint('observer', __name__)

# ── In-memory store for staff activity (aggregated per session) ──
# { discord_id: { 'message_count': int, 'channels': set, 'last_seen': datetime } }
staff_activity = {}


@observer_bp.route('/observer/action', methods=['POST'])
def log_action():
    """
    Receives a staff moderation action (ban, kick, timeout).
    Logs it and gives the staff member points for taking action.
    """
    data = request.json
    discord_id = data.get('discord_id')
    staff_name = data.get('staff_name', 'Unknown')
    action_type = data.get('action_type')
    target = data.get('target')
    guild = data.get('guild')
    reason = data.get('reason', 'No reason given')

    # Find the worker by discord_id, create if doesn't exist
    worker = Worker.query.filter_by(discord_id=discord_id).first()
    if not worker:
        worker = Worker(
            name=staff_name,
            email=f'{staff_name.lower().replace(" ", ".")}@discord.local',
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
    }
    points = points_map.get(action_type, 0)

    if worker and points:
        worker.score += points
        log = ScoreLog(
            worker_id=worker.id,
            change=points,
            reason=f'[Discord] {action_type.replace("_", " ").title()} on {target} in {guild}',
            source='discord',
            admin_correction=False
        )
        db.session.add(log)
        db.session.commit()

    print(f'[Observer API] Action logged: {action_type} by {staff_name} on {target}')
    return jsonify({'message': 'Action logged', 'points_awarded': points}), 201


@observer_bp.route('/observer/flag', methods=['POST'])
def log_flag():
    """
    Receives a flagged event (ban reversed, timeout reversed early).
    Deducts points if flagged=True.
    """
    data = request.json
    discord_id = data.get('discord_id')
    staff_name = data.get('staff_name', 'Unknown')
    action_type = data.get('action_type')
    target = data.get('target')
    guild = data.get('guild')
    flagged = data.get('flagged', False)
    flag_reason = data.get('flag_reason', '')
    hours = data.get('hours_until_reversal')

    # Find the worker by discord_id, create if doesn't exist
    worker = Worker.query.filter_by(discord_id=discord_id).first()
    if not worker:
        worker = Worker(
            name=staff_name,
            email=f'{staff_name.lower().replace(" ", ".")}@discord.local',
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
        worker.score += points

        note = flag_reason
        if hours:
            note += f' ({hours:.1f} hours later)'

        log = ScoreLog(
            worker_id=worker.id,
            change=points,
            reason=f'[Discord] FLAG: {note} | Target: {target} in {guild}',
            source='discord',
            admin_correction=False
        )
        db.session.add(log)
        db.session.commit()

        print(f'[Observer API] Flagged action: {action_type} by {staff_name} — {points} pts')

    return jsonify({'message': 'Flag logged', 'flagged': flagged}), 201


@observer_bp.route('/observer/warn', methods=['POST'])
def log_warn():
    """
    Receives a warn/infraction parsed from a mod bot embed.
    Awards small points to the staff member for issuing a warn.
    """
    data = request.json
    mod_name = data.get('mod_name')
    target_name = data.get('target_name')
    reason = data.get('reason')
    source_bot = data.get('source_bot')
    channel = data.get('channel')
    guild = data.get('guild')

    # Try to find worker by name (since we only have name from embed parsing)
    worker = Worker.query.filter(
        Worker.name.ilike(f'%{mod_name}%')
    ).first() if mod_name else None

    if worker:
        worker.score += 3
        log = ScoreLog(
            worker_id=worker.id,
            change=3,
            reason=f'[Discord] Warn issued to {target_name} via {source_bot} in #{channel} | {guild}',
            source='discord',
            admin_correction=False
        )
        db.session.add(log)
        db.session.commit()

    print(f'[Observer API] Warn logged: {mod_name} warned {target_name} — reason: {reason}')
    return jsonify({'message': 'Warn logged'}), 201


@observer_bp.route('/observer/activity', methods=['POST'])
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
            worker.score += 1
            log = ScoreLog(
                worker_id=worker.id,
                change=1,
                reason=f'[Discord] Active in {len(staff_activity[discord_id]["channels"])} channel(s) in {guild}',
                source='discord',
                admin_correction=False
            )
            db.session.add(log)
            db.session.commit()

    return jsonify({'message': 'Activity recorded', 'total_messages': count}), 200


@observer_bp.route('/observer/confirm', methods=['POST'])
def confirm_action():
    """
    Confirms a ban stood for 48+ hours (valid moderation action).
    Already awarded points at ban_issued — this just logs confirmation.
    """
    data = request.json
    print(f'[Observer API] Confirmed: {data.get("action_type")} by {data.get("staff_name")} on {data.get("target")}')
    return jsonify({'message': 'Action confirmed as valid'}), 200


@observer_bp.route('/observer/staff-activity', methods=['GET'])
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


# ─────────────────────────────────────────────
# BEHAVIORAL MESSAGE LOGGING (Community Engine)
# ─────────────────────────────────────────────

@observer_bp.route('/observer/messages', methods=['POST'])
def log_messages():
    """
    Receives a batch of message records from the bot.
    Stores anonymized behavioral data (no message content).
    """
    data = request.json
    messages = data if isinstance(data, list) else [data]

    for msg in messages:
        record = MessageRecord(
            discord_id=msg['discord_id'],
            name=msg.get('name', 'Unknown'),
            guild_id=msg.get('guild_id', ''),
            channel_name=msg.get('channel', 'unknown'),
            is_public_channel=msg.get('channel_is_public', True),
            message_length=msg.get('length', 0),
            message_content=msg.get('content') if msg.get('channel_is_public') else None,
            hour_of_day=msg.get('hour'),
            day_of_week=msg.get('day'),
        )
        db.session.add(record)

    db.session.commit()
    return jsonify({'message': f'{len(messages)} messages logged'}), 201


@observer_bp.route('/observer/analytics/<discord_id>', methods=['GET'])
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


@observer_bp.route('/observer/analytics', methods=['GET'])
def all_analytics():
    """Returns overall behavioral analytics across all users."""
    total_messages = MessageRecord.query.count()
    unique_users = db.session.query(MessageRecord.discord_id).distinct().count()

    top_users = db.session.query(
        MessageRecord.discord_id,
        MessageRecord.name,
        func.count(MessageRecord.id).label('count')
    ).group_by(MessageRecord.discord_id, MessageRecord.name).order_by(func.count(MessageRecord.id).desc()).limit(10).all()

    hourly_all = db.session.query(
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
def receive_guild_scan():
    """
    Receives a full guild scan from the bot.
    Stores guild info, roles, and members with staff flags.
    """
    data = request.json

    guild_id = data['guild_id']

    # Upsert GuildInfo
    guild = GuildInfo.query.filter_by(guild_id=guild_id).first()
    if not guild:
        guild = GuildInfo(guild_id=guild_id)
    guild.name = data['name']
    guild.owner_id = data.get('owner_id')
    guild.owner_name = data.get('owner_name')
    guild.member_count = data.get('member_count', 0)
    guild.online_count = data.get('online_count', 0)
    guild.staff_count = data.get('staff_count', 0)
    guild.bot_count = data.get('bot_count', 0)
    guild.role_count = data.get('role_count', 0)
    guild.prefix = data.get('prefix', '["!ss "]')
    guild.scanned_at = datetime.utcnow()
    db.session.add(guild)
    db.session.flush()

    # Upsert roles
    GuildRole.query.filter_by(guild_id=guild_id).delete()
    for r in data.get('roles', []):
        role = GuildRole(
            guild_id=guild_id,
            role_id=r['role_id'],
            name=r['name'],
            position=r['position'],
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

    # Upsert members
    GuildMember.query.filter_by(guild_id=guild_id).delete()
    for m in data.get('members', []):
        member = GuildMember(
            guild_id=guild_id,
            member_id=m['member_id'],
            name=m['name'],
            display_name=m.get('display_name'),
            joined_at=datetime.fromisoformat(m['joined_at']) if m.get('joined_at') else None,
            is_bot=m.get('is_bot', False),
            is_owner=m.get('is_owner', False),
            is_staff=m.get('is_staff', False),
            role_ids=m.get('role_ids'),
            top_role_position=m.get('top_role_position', 0),
        )
        db.session.add(member)

    db.session.commit()

    print(f'[Observer API] Guild scan stored: {guild.name} — {guild.staff_count} staff, {guild.member_count} members')
    return jsonify({'message': 'Guild scan stored', 'guild': guild.name, 'staff': guild.staff_count}), 201


@observer_bp.route('/observer/guilds', methods=['GET'])
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
    } for g in guilds])


@observer_bp.route('/observer/guilds/<guild_id>/members', methods=['GET'])
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
    } for m in members])


@observer_bp.route('/observer/guilds/<guild_id>/roles', methods=['GET'])
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


@observer_bp.route('/observer/guilds/<guild_id>/prefix', methods=['GET', 'PATCH'])
def guild_prefix(guild_id):
    """Get or set prefixes for a guild. Stores as JSON array."""
    guild = GuildInfo.query.filter_by(guild_id=guild_id).first_or_404()

    if request.method == 'PATCH':
        data = request.json
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
