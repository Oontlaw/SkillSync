import os
import secrets
import time
import requests
from flask import Blueprint, redirect, request, session, url_for
from urllib.parse import urlencode
from database import db, GuildInfo

auth_bp = Blueprint('auth', __name__)

CLIENT_ID = os.getenv('DISCORD_CLIENT_ID')
CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET')
DISCORD_API = 'https://discord.com/api/v10'

PERM_ADMINISTRATOR = 1 << 3
PERM_MANAGE_GUILD = 1 << 5

OAUTH_STATE_TTL_SECONDS = 10 * 60
_pending_states = {}


def _remember_state(state):
    now = time.time()
    expired = [
        key
        for key, created_at in _pending_states.items()
        if now - created_at > OAUTH_STATE_TTL_SECONDS
    ]
    for key in expired:
        _pending_states.pop(key, None)
    _pending_states[state] = now


def _consume_state(state):
    if not state:
        return False
    saved_state = session.pop('oauth_state', None)
    if saved_state and secrets.compare_digest(state, saved_state):
        _pending_states.pop(state, None)
        return True

    created_at = _pending_states.pop(state, None)
    if not created_at:
        return False
    return time.time() - created_at <= OAUTH_STATE_TTL_SECONDS


def _redirect_uri():
    uri = os.getenv('DISCORD_REDIRECT_URI')
    if uri:
        return uri
    uri = request.host_url.rstrip('/') + url_for('auth.callback')
    print(f'[Auth] Generated Redirect URI: {uri}')
    return uri


@auth_bp.route('/login')
def login():
    state = secrets.token_hex(16)
    session['oauth_state'] = state
    _remember_state(state)
    uri = _redirect_uri()
    params = {
        'client_id': CLIENT_ID,
        'redirect_uri': uri,
        'response_type': 'code',
        'scope': 'identify guilds',
        'state': state,
    }

    return redirect(f'{DISCORD_API}/oauth2/authorize?{urlencode(params)}')


@auth_bp.route('/callback')
def callback():
    returned_state = request.args.get('state')
    if not _consume_state(returned_state):
        return 'Invalid state parameter. Possible CSRF attack.', 403

    code = request.args.get('code')
    if not code:
        return 'No authorization code received.', 400

    uri = _redirect_uri()
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': uri,
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    try:
        resp = requests.post(f'{DISCORD_API}/oauth2/token', data=data, headers=headers, timeout=10)
        resp.raise_for_status()
        token_data = resp.json()
    except Exception as e:
        print(f'[Auth] Token exchange failed: {e}')
        return 'Failed to exchange authorization code.', 400
    access_token = token_data['access_token']

    try:
        user_resp = requests.get(f'{DISCORD_API}/users/@me', headers={'Authorization': f'Bearer {access_token}'}, timeout=10)
        user_resp.raise_for_status()
        user = user_resp.json()
    except Exception as e:
        print(f'[Auth] User fetch failed: {e}')
        return 'Failed to fetch user info.', 400

    try:
        guilds_resp = requests.get(f'{DISCORD_API}/users/@me/guilds', headers={'Authorization': f'Bearer {access_token}'}, timeout=10)
        guilds_resp.raise_for_status()
        guilds = guilds_resp.json()
    except Exception as e:
        print(f'[Auth] Guilds fetch failed: {e}')
        return 'Failed to fetch guilds.', 400

    # Get guilds where the bot is also present (from scanned GuildInfo table)
    bot_guild_ids = set(g.guild_id for g in GuildInfo.query.with_entities(GuildInfo.guild_id).all())

    accessible = []
    for g in guilds:
        perms = int(g.get('permissions', '0'))
        has_perm = perms & PERM_ADMINISTRATOR or perms & PERM_MANAGE_GUILD
        bot_is_here = g['id'] in bot_guild_ids
        if has_perm and bot_is_here:
            accessible.append({'id': g['id'], 'name': g['name']})

    if not accessible:
        return 'You don\'t have permission to view any servers, or the bot hasn\'t been added to your servers yet.', 403

    session['user'] = {
        'id': user['id'],
        'name': user.get('global_name') or user['username'],
    }
    session['accessible_guilds'] = accessible

    return redirect(url_for('dashboard.index'))


@auth_bp.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('dashboard.index'))
