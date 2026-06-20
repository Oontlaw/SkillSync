from functools import wraps
from flask import Blueprint, request, jsonify, session
from database import db, CommunityEvent, Worker
from scoring import award_points

community_bp = Blueprint('community', __name__)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return jsonify({'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated


@community_bp.route('/community/event', methods=['POST'])
@login_required
def community_event():
    data = request.json
    if not data:
        return jsonify({'error': 'No JSON body'}), 400
    discord_id = data.get('discord_id')
    event_type = data.get('event_type')
    detail = data.get('detail', '')
    score_impact = data.get('score_impact', 0)

    # Log the community event
    event = CommunityEvent(
        discord_id=discord_id,
        event_type=event_type,
        detail=detail,
        score_impact=score_impact
    )
    db.session.add(event)

    # If worker is registered, update their score
    worker = Worker.query.filter_by(discord_id=discord_id).first()
    if worker and score_impact != 0:
        award_points(worker.id, event_type, source='discord', custom_points=score_impact, note=detail)

    db.session.commit()
    return jsonify({'message': 'Community event recorded'}), 201
