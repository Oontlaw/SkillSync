import asyncio
import requests
from discord.ext import tasks
from datetime import datetime, timezone, timedelta
from bot_core.config import (
    BAN_WATCH_HOURS, MESSAGE_RETENTION_DAYS, PING_WATCH_MINUTES,
    HEARTBEAT_INTERVAL_MINUTES, SKILLSYNC_API, API_KEY, HEARTBEAT_GUILD_ID,
)
from bot_core import state as bot_state
from bot_core.state import (  # re-exported for other modules
    flush_message_buffer, flush_presence_buffer,
    flush_voice_buffer, flush_mention_buffer,
    flush_join_buffer, flush_member_presence_buffer,
)
from bot_core.api_client import api_post
from bot_core.logging import log


_bot = None
_last_heartbeat = -9999999999  # fire immediately on first check


def set_bot(bot):
    global _bot
    _bot = bot


@tasks.loop(seconds=30)
async def flush_all_buffers():
    """Flush all buffered data every 30 seconds."""
    await flush_message_buffer()
    await flush_presence_buffer()
    await flush_member_presence_buffer()
    await flush_mention_buffer()
    await flush_voice_buffer()
    await flush_join_buffer()
    await _maybe_heartbeat()


async def _maybe_heartbeat():
    global _last_heartbeat
    if not bot_state.heartbeat_channel_id or not HEARTBEAT_GUILD_ID or not _bot:
        return
    now = datetime.now(timezone.utc)
    elapsed = now.timestamp() - _last_heartbeat
    if elapsed < HEARTBEAT_INTERVAL_MINUTES * 60:
        return
    _last_heartbeat = now.timestamp()
    try:
        channel = _bot.get_channel(bot_state.heartbeat_channel_id)
        if not channel:
            bot_state.set_heartbeat_channel(None)
            return
        uptime = now - bot_state.bot_start_time if bot_state.bot_start_time else timedelta()
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes = remainder // 60
        msg_count = "?"
        member_count = "?"
        try:
            resp = await asyncio.to_thread(requests.get, f'{SKILLSYNC_API}/observer/staff-activity',
                                           headers={'Authorization': f'Bearer {API_KEY}'}, timeout=5)
            if resp.ok:
                data = resp.json()
                msg_count = str(data.get('total_messages', '?'))
                member_count = str(data.get('total_members', '?'))
        except Exception:
            pass
        names = ', '.join(g.name for g in _bot.guilds)
        await channel.send(
            f'🟢 **Bot Alive** | Uptime: `{hours}h {minutes}m` | '
            f'Servers: `{len(_bot.guilds)}` | '
            f'Messages: `{msg_count}` | '
            f'Members: `{member_count}` | '
            f'``{names}``'
        )
        log(f'Heartbeat posted to #{channel.name}')
    except Exception as e:
        log(f'Heartbeat error: {e}')


@tasks.loop(hours=1)
async def check_reversed_actions():
    """
    Every hour: confirm bans that have stood 48+ hours,
    scan anomalies, and trigger weekly ML retrain.
    """
    now = datetime.now(timezone.utc)
    to_confirm = [
        key for key, data in bot_state.pending_bans.items()
        if (now - data['timestamp']).total_seconds() / 3600 > BAN_WATCH_HOURS
    ]

    for key in to_confirm:
        data = bot_state.pending_bans.pop(key)
        user_id_str = str(key[1]) if isinstance(key, tuple) and len(key) > 1 else ''
        guild_id_str = str(key[0]) if isinstance(key, tuple) and len(key) > 0 else str(data.get('guild_id', ''))
        print(f'[Observer] Ban confirmed valid: {data["user_name"]} by {data["banner_name"]}')
        await api_post('/observer/confirm', {
            'discord_id': data['banner_id'],
            'staff_name': data['banner_name'],
            'action_type': 'ban_confirmed',
            'target': data['user_name'],
            'target_id': user_id_str,
            'guild': data['guild_name'],
            'guild_id': guild_id_str,
            'note': 'Ban stood for 48+ hours — confirmed as valid moderation action',
            'timestamp': now.isoformat()
        })

    # Scan anomalies and burnout risks
    print(f'[Observer] Scanning behavioral anomalies...')
    await api_post('/observer/anomalies/scan', {'trigger': 'hourly'})
    print(f'[Observer] Scanning burnout risks...')
    await api_post('/observer/burnout-scan', {'trigger': 'hourly'})
    print(f'[Observer] ML anomaly scan...')
    await api_post('/observer/ml/anomalies/scan', {'trigger': 'hourly'})
    print(f'[Observer] ML burnout scan...')
    await api_post('/observer/ml/burnout-scan', {'trigger': 'hourly'})

    # Correction-triggered retrain check
    try:
        resp = await asyncio.to_thread(requests.get,
            f'{SKILLSYNC_API}/observer/ml/pending-retrain',
            headers={'Authorization': f'Bearer {API_KEY}'}, timeout=5)
        if resp.ok and resp.json().get('pending'):
            print(f'[Observer] Correction-triggered retrain pending...')
            await api_post('/observer/ml/retrain', {'trigger': 'correction_feedback'})
    except Exception as e:
        print(f'[Observer] Retrain check error: {e}')

    # Weekly ML model retrain (168 hours = 7 days)
    val = bot_state.inc_ml_retrain_counter()
    if val >= 168:
        bot_state.set_ml_retrain_counter(0)
        print(f'[Observer] Weekly ML retrain triggered...')
        await api_post('/observer/ml/retrain', {'trigger': 'weekly'})


@tasks.loop(hours=6)
async def message_cleanup_loop():
    """Delete messages older than MESSAGE_RETENTION_DAYS via API."""
    try:
        resp = await api_post('/observer/cleanup', {'retention_days': MESSAGE_RETENTION_DAYS})
        if resp and resp.get('deleted'):
            print(f'[Cleanup] Deleted {resp["deleted"]} old messages, {resp.get("deleted_mentions", 0)} old mentions')
    except Exception as e:
        print(f'[Cleanup] Error: {e}')


@tasks.loop(minutes=5)
async def check_ping_joins():
    """Every 5 min, expire @everyone pings after 20 min window."""
    now = datetime.now(timezone.utc)
    expired = [
        gid for gid, data in bot_state.active_pings.items()
        if (now - data['timestamp']).total_seconds() / 60 > PING_WATCH_MINUTES
    ]
    for gid in expired:
        data = bot_state.active_pings.pop(gid)
        if data['join_count'] > 0:
            print(f'[PingWatch] {data["mod_name"]} pinged @everyone, {data["join_count"]} joined within {PING_WATCH_MINUTES}min')
            await api_post('/observer/ping-join', {
                'moderator_id': data['mod_id'],
                'moderator_name': data['mod_name'],
                'guild_id': str(gid),
                'guild_name': data['guild_name'],
                'channel': data['channel'],
                'new_members': data['join_count'],
                'joiners': ','.join(data['joiners'][:50]),
                'timestamp': data['timestamp'].isoformat(),
            })
