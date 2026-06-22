import asyncio
import discord
import requests
from discord.ext import tasks
from datetime import datetime, timedelta, timezone
from bot_core.config import HEARTBEAT_GUILD_ID, HEARTBEAT_INTERVAL_MINUTES, SKILLSYNC_API, API_KEY
from bot_core.state import heartbeat_channel_id, bot_start_time, set_heartbeat_channel
from bot_core.logging import log


async def setup_heartbeat(bot):
    """Find or create the heartbeat channel in HEARTBEAT_GUILD_ID."""
    if not HEARTBEAT_GUILD_ID:
        log('Heartbeat disabled: HEARTBEAT_GUILD_ID not set')
        print('[Heartbeat] Disabled — HEARTBEAT_GUILD_ID not set')
        return
    guild = bot.get_guild(int(HEARTBEAT_GUILD_ID))
    if not guild:
        log(f'Heartbeat guild {HEARTBEAT_GUILD_ID} not found')
        print(f'[Heartbeat] Guild {HEARTBEAT_GUILD_ID} not found')
        return
    channel = discord.utils.get(guild.text_channels, name='heartbeat')
    if not channel:
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            channel = await guild.create_text_channel('heartbeat', overwrites=overwrites, topic='Bot status & uptime updates')
            log(f'Created #heartbeat channel in {guild.name}')
            print(f'[Heartbeat] Created #heartbeat channel in {guild.name}')
        except Exception as e:
            log(f'Could not create heartbeat channel: {e}')
            print(f'[Heartbeat] Could not create channel: {e}')
            return
    set_heartbeat_channel(channel.id)
    await channel.send('🟢 **Heartbeat started** — Bot online, monitoring active.')
    log(f'Heartbeat channel set to #{channel.name} in {guild.name}')
    print(f'[Heartbeat] Channel set to #{channel.name} in {guild.name}')


@tasks.loop(minutes=HEARTBEAT_INTERVAL_MINUTES)
async def heartbeat_status(bot):
    """Post bot status to heartbeat channel every 5 minutes."""
    try:
        if not heartbeat_channel_id:
            return
        channel = bot.get_channel(heartbeat_channel_id)
        if not channel:
            log(f'Heartbeat channel {heartbeat_channel_id} not found')
            set_heartbeat_channel(None)
            return
        uptime = datetime.now(timezone.utc) - bot_start_time if bot_start_time else timedelta()
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
        except Exception as e:
            print(f'[Heartbeat] Failed to fetch stats: {e}')
        names = ', '.join(g.name for g in bot.guilds)
        await channel.send(
            f'🟢 **Bot Alive** | Uptime: `{hours}h {minutes}m` | '
            f'Servers: `{len(bot.guilds)}` | '
            f'Messages: `{msg_count}` | '
            f'Members: `{member_count}` | '
            f'``{names}``'
        )
        log(f'Heartbeat posted to #{channel.name}')
    except Exception as e:
        log(f'Heartbeat error: {e}')
        print(f'[Heartbeat] Error: {e}')


@heartbeat_status.before_loop
async def before_heartbeat():
    await asyncio.sleep(60)
