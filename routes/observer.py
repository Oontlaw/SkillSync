from flask import Blueprint, request, jsonify
from database import db, Worker, ScoreLog
from datetime import datetime

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
