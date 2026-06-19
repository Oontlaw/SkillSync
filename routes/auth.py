import os
import requests
from flask import Blueprint, redirect, request, session, url_for
from database import db, GuildInfo

auth_bp = Blueprint('auth', __name__)

CLIENT_ID = os.getenv('DISCORD_CLIENT_ID')
CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET')

DISCORD_API = 'https://discord.com/api/v10'

PERM_ADMINISTRATOR = 1 << 3
PERM_MANAGE_GUILD = 1 << 5


@auth_bp.route('/login')
def login():
    redirect_uri = url_for('auth.callback', _external=True, _scheme=request.scheme)
    return redirect(
        f'{DISCORD_API}/oauth2/authorize?client_id={CLIENT_ID}'
        f'&redirect_uri={redirect_uri}'
        f'&response_type=code&scope=identify%20guilds'
    )


@auth_bp.route('/callback')
def callback():
    code = request.args.get('code')
    if not code:
        return 'No authorization code received.', 400

    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': url_for('auth.callback', _external=True, _scheme=request.scheme),
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    resp = requests.post(f'{DISCORD_API}/oauth2/token', data=data, headers=headers)
    if not resp.ok:
        return 'Failed to exchange authorization code.', 400
    token_data = resp.json()
    access_token = token_data['access_token']

    headers = {'Authorization': f'Bearer {access_token}'}

    user_resp = requests.get(f'{DISCORD_API}/users/@me', headers=headers)
    if not user_resp.ok:
        return 'Failed to fetch user info.', 400
    user = user_resp.json()

    guilds_resp = requests.get(f'{DISCORD_API}/users/@me/guilds', headers=headers)
    if not guilds_resp.ok:
        return 'Failed to fetch guilds.', 400
    guilds = guilds_resp.json()

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


@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('dashboard.index'))
