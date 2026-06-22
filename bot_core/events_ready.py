import asyncio
import requests
import discord
from datetime import datetime, timezone
from bot_core.config import SKILLSYNC_API, API_KEY, MESSAGE_RETENTION_DAYS, PING_WATCH_MINUTES
from bot_core.state import (
    prefix_cache, content_trust, pending_bans, pending_timeouts,
    set_bot_start_time,
)
from bot_core.scanner import scan_guild, build_automod_alert_channels
from bot_core.heartbeat import setup_heartbeat, heartbeat_status
from bot_core.tasks import flush_all_buffers, check_reversed_actions, message_cleanup_loop, check_ping_joins
from bot_core.logging import log


async def handle_ready(bot):
    log(f'Bot online as {bot.user}')
    log(f'Watching {len(bot.guilds)} server(s)')
    for guild in bot.guilds:
        log(f'  - Guild: {guild.name} (ID: {guild.id})')
        me = guild.me
        if me:
            perms = dict(me.guild_permissions)
            log(f'    Perms: ban_members={perms["ban_members"]}, kick_members={perms["kick_members"]}, view_audit_log={perms["view_audit_log"]}, manage_messages={perms["manage_messages"]}')
            if not perms.get('view_audit_log'):
                log(f'    \u26a0\ufe0f  Bot LACKS view_audit_log permission in {guild.name}! Ban/kick/timeout detection will NOT work.')
                print(f'[SkillSync] WARNING: Bot lacks view_audit_log permission in {guild.name}')
    print(f'[SkillSync] Bot online as {bot.user}')
    print(f'[SkillSync] Watching {len(bot.guilds)} server(s)')
    print(f'[SkillSync] Message retention: {MESSAGE_RETENTION_DAYS} days')
    print(f'[SkillSync] Ping watch window: {PING_WATCH_MINUTES} min')

    # Start background tasks
    check_reversed_actions.start()
    flush_all_buffers.start()
    message_cleanup_loop.start()
    check_ping_joins.start()

    # Fetch prefixes + content trust from API
    try:
        resp = await asyncio.to_thread(requests.get, f'{SKILLSYNC_API}/observer/guilds',
                                       headers={'Authorization': f'Bearer {API_KEY}'}, timeout=5)
        if resp.ok:
            for g in resp.json():
                prefix_cache[g['guild_id']] = g.get('prefixes', ['!ss '])
                content_trust[g['guild_id']] = g.get('store_content', False)
    except Exception as e:
        print(f'[SkillSync] Could not fetch prefixes: {e}')

    # Set bot presence with prefix info
    if bot.guilds:
        first_prefixes = prefix_cache.get(str(bot.guilds[0].id), ['!ss '])
        try:
            await bot.change_presence(activity=discord.Game(name=f'{first_prefixes[0].strip()} | Watches over you in your sleep'))
        except Exception as e:
            print(f'[SkillSync] Could not set presence: {e}')

    # Scan all existing guilds on startup
    for guild in bot.guilds:
        await scan_guild(guild)

    # Load AutoMod alert channels after scan
    build_automod_alert_channels()

    # Reload pending state from API for restart resilience
    try:
        resp = await asyncio.to_thread(requests.get, f'{SKILLSYNC_API}/observer/pending-state',
                                       headers={'Authorization': f'Bearer {API_KEY}'}, timeout=5)
        if resp.ok:
            state_data = resp.json()

            def ensure_utc(dt):
                if dt is None:
                    return datetime.now(timezone.utc)
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)

            for b in state_data.get('pending_bans', []):
                key = (int(b['guild_id']), int(b['user_id'])) if b['guild_id'] and b['user_id'] else None
                if key:
                    pending_bans[key] = {
                        'banner_id': b['banner_id'],
                        'banner_name': b['banner_name'],
                        'user_name': b['user_name'],
                        'guild_id': int(b['guild_id']),
                        'guild_name': '',
                        'reason': b.get('reason'),
                        'timestamp': ensure_utc(datetime.fromisoformat(b['timestamp'])) if b.get('timestamp') else datetime.now(timezone.utc),
                    }

            for t in state_data.get('pending_timeouts', []):
                key = (int(t['guild_id']), int(t['user_id'])) if t['guild_id'] and t['user_id'] else None
                if key:
                    pending_timeouts[key] = {
                        'mod_id': t['mod_id'],
                        'mod_name': t['mod_name'],
                        'until': ensure_utc(datetime.fromisoformat(t['until'])) if t.get('until') else None,
                        'timestamp': ensure_utc(datetime.fromisoformat(t['timestamp'])) if t.get('timestamp') else datetime.now(timezone.utc),
                    }

            if state_data.get('pending_bans') or state_data.get('pending_timeouts'):
                log(f'Restored {len(state_data["pending_bans"])} pending bans, {len(state_data["pending_timeouts"])} pending timeouts from DB')
                print(f'[Restore] Loaded {len(state_data["pending_bans"])} bans, {len(state_data["pending_timeouts"])} timeouts')
    except Exception as e:
        print(f'[Restore] Could not fetch pending state: {e}')

    # Setup heartbeat
    set_bot_start_time(discord.utils.utcnow())
    await setup_heartbeat(bot)
    heartbeat_status.start(bot)


async def handle_guild_join(bot, guild):
    """Fires when the bot joins a new server. Full scan immediately."""
    log(f'JOINED new guild: {guild.name} (ID: {guild.id})')
    print(f'[SkillSync] Joined new guild: {guild.name}')
    prefix_cache[str(guild.id)] = ['!ss ']
    await scan_guild(guild)
