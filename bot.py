import discord
from discord.ext import commands, tasks
import requests
import os
import asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
SKILLSYNC_API = os.getenv('SKILLSYNC_API', 'http://localhost:5000/api')

# How long to watch a ban before deciding it's "confirmed" (in hours)
BAN_WATCH_HOURS = 48

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True
intents.guilds = True

bot = commands.Bot(command_prefix='!ss ', intents=intents)

# --- In-memory watch lists ---
# Tracks recent bans: { (guild_id, user_id): { 'banner_id': ..., 'timestamp': ... } }
pending_bans = {}

# Tracks recent timeouts: { (guild_id, user_id): { 'mod_id': ..., 'until': ..., 'timestamp': ... } }
pending_timeouts = {}

# Known moderation bot names to read logs from
MOD_BOT_NAMES = ['mee6', 'dyno', 'carl-bot', 'wick', 'arcane', 'combot', 'gaius']


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def api_post(endpoint, payload):
    """Send data to SkillSync backend silently."""
    try:
        requests.post(f'{SKILLSYNC_API}{endpoint}', json=payload, timeout=5)
    except Exception as e:
        print(f'[SkillSync Observer] API error on {endpoint}: {e}')

def is_mod_bot(member):
    """Check if a member is a known moderation bot."""
    if not member.bot:
        return False
    return any(name in member.name.lower() for name in MOD_BOT_NAMES)

def extract_warn_from_embed(embed):
    """
    Parse warn embeds from common mod bots (MEE6, Dyno, Carl-bot).
    Returns parsed data dict or None if not a warn embed.
    """
    if not embed.description and not embed.fields:
        return None

    text = (embed.description or '') + ' '.join(f.value for f in embed.fields)
    text_lower = text.lower()

    warn_keywords = ['warned', 'warning', 'infraction', 'strike', 'muted', 'kicked']
    if not any(kw in text_lower for kw in warn_keywords):
        return None

    mod_name = None
    target_name = None
    reason = None

    for field in embed.fields:
        name_lower = field.name.lower()
        if 'moderator' in name_lower or 'staff' in name_lower or 'by' in name_lower:
            mod_name = field.value.strip()
        if 'user' in name_lower or 'member' in name_lower or 'target' in name_lower:
            target_name = field.value.strip()
        if 'reason' in name_lower:
            reason = field.value.strip()

    if embed.footer and embed.footer.text:
        footer = embed.footer.text
        if 'moderator' in footer.lower() or 'by' in footer.lower():
            mod_name = footer

    return {
        'mod_name': mod_name,
        'target_name': target_name,
        'reason': reason or 'No reason provided',
        'raw': text[:300]
    }


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f'👁️  SkillSync Observer is online as {bot.user}')
    print(f'   Watching {len(bot.guilds)} server(s)')
    check_reversed_actions.start()


# ─────────────────────────────────────────────
# BAN DETECTION
# ─────────────────────────────────────────────

@bot.event
async def on_member_ban(guild, user):
    """
    Fires when any member is banned.
    Reads audit log to identify which staff member issued it.
    Stores in pending_bans to watch for reversal.
    """
    await asyncio.sleep(1)

    banner_id = None
    banner_name = 'Unknown'
    reason = 'No reason given'

    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
            if entry.target.id == user.id:
                banner_id = entry.user.id
                banner_name = entry.user.name
                reason = entry.reason or 'No reason given'
                break
    except Exception as e:
        print(f'[Observer] Could not read audit log for ban: {e}')

    print(f'[Observer] Ban: {user.name} by {banner_name} in {guild.name}')

    key = (guild.id, user.id)
    pending_bans[key] = {
        'banner_id': str(banner_id),
        'banner_name': banner_name,
        'user_name': user.name,
        'guild_id': guild.id,
        'guild_name': guild.name,
        'reason': reason,
        'timestamp': datetime.now(timezone.utc)
    }

    api_post('/observer/action', {
        'discord_id': str(banner_id),
        'staff_name': banner_name,
        'action_type': 'ban_issued',
        'target': user.name,
        'guild': guild.name,
        'reason': reason,
        'flagged': False,
        'flag_reason': None,
        'timestamp': datetime.now(timezone.utc).isoformat()
    })


@bot.event
async def on_member_unban(guild, user):
    """
    Fires when a ban is lifted.
    If the ban was recent (under BAN_WATCH_HOURS), flag it as hasty/wrongful.
    """
    key = (guild.id, user.id)
    ban_data = pending_bans.pop(key, None)

    unbanner_name = 'Unknown'
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.unban):
            if entry.target.id == user.id:
                unbanner_name = entry.user.name
                break
    except:
        pass

    if ban_data:
        elapsed = datetime.now(timezone.utc) - ban_data['timestamp']
        hours_elapsed = elapsed.total_seconds() / 3600
        is_hasty = hours_elapsed <= BAN_WATCH_HOURS

        print(f'[Observer] Unban: {user.name} — {hours_elapsed:.1f}h later — flagged: {is_hasty}')

        api_post('/observer/flag', {
            'discord_id': ban_data['banner_id'],
            'staff_name': ban_data['banner_name'],
            'action_type': 'ban_reversed',
            'target': user.name,
            'guild': ban_data['guild_name'],
            'original_reason': ban_data['reason'],
            'reversed_by': unbanner_name,
            'hours_until_reversal': round(hours_elapsed, 2),
            'flagged': is_hasty,
            'flag_reason': 'Ban reversed within 48 hours — possible wrongful ban' if is_hasty else None,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })


# ─────────────────────────────────────────────
# TIMEOUT / MUTE DETECTION
# ─────────────────────────────────────────────

@bot.event
async def on_member_update(before, after):
    """
    Detects Discord timeouts being added or removed early.
    Early removal = possible overreach by original mod.
    """
    guild = after.guild

    # Timeout added
    if before.timed_out_until is None and after.timed_out_until is not None:
        await asyncio.sleep(1)
        mod_id = None
        mod_name = 'Unknown'
        reason = 'No reason given'

        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
                if entry.target.id == after.id:
                    mod_id = entry.user.id
                    mod_name = entry.user.name
                    reason = entry.reason or 'No reason given'
                    break
        except:
            pass

        key = (guild.id, after.id)
        pending_timeouts[key] = {
            'mod_id': str(mod_id),
            'mod_name': mod_name,
            'until': after.timed_out_until,
            'timestamp': datetime.now(timezone.utc)
        }

        print(f'[Observer] Timeout: {after.name} by {mod_name}')

        api_post('/observer/action', {
            'discord_id': str(mod_id),
            'staff_name': mod_name,
            'action_type': 'timeout_issued',
            'target': after.name,
            'guild': guild.name,
            'reason': reason,
            'flagged': False,
            'flag_reason': None,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })

    # Timeout removed early
    elif before.timed_out_until is not None and after.timed_out_until is None:
        key = (guild.id, after.id)
        timeout_data = pending_timeouts.pop(key, None)

        if timeout_data:
            now = datetime.now(timezone.utc)
            removed_early = now < timeout_data['until'] - timedelta(minutes=5)

            api_post('/observer/flag', {
                'discord_id': timeout_data['mod_id'],
                'staff_name': timeout_data['mod_name'],
                'action_type': 'timeout_reversed',
                'target': after.name,
                'guild': guild.name,
                'flagged': removed_early,
                'flag_reason': 'Timeout lifted before natural expiry — possible overreach' if removed_early else None,
                'timestamp': now.isoformat()
            })


# ─────────────────────────────────────────────
# KICK DETECTION
# ─────────────────────────────────────────────

@bot.event
async def on_member_remove(member):
    """
    Fires when a member leaves or is kicked.
    We check audit logs to confirm it was a kick (not a voluntary leave).
    """
    await asyncio.sleep(1)
    guild = member.guild

    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
            if entry.target.id == member.id:
                print(f'[Observer] Kick: {member.name} by {entry.user.name}')
                api_post('/observer/action', {
                    'discord_id': str(entry.user.id),
                    'staff_name': entry.user.name,
                    'action_type': 'kick_issued',
                    'target': member.name,
                    'guild': guild.name,
                    'reason': entry.reason or 'No reason given',
                    'flagged': False,
                    'flag_reason': None,
                    'timestamp': datetime.now(timezone.utc).isoformat()
                })
                return
    except:
        pass
    # If no audit log match — member left voluntarily, do nothing


# ─────────────────────────────────────────────
# MOD BOT LOG READER — Warns & Infractions
# ─────────────────────────────────────────────

@bot.event
async def on_message(message):
    """
    Two jobs:
    1. Read embeds from known mod bots to detect warns/infractions
    2. Passively track when staff members are active and in which channels
    """
    if not message.guild:
        return

    # Job 1: Parse mod bot embeds
    if is_mod_bot(message.author) and message.embeds:
        for embed in message.embeds:
            warn_data = extract_warn_from_embed(embed)
            if warn_data:
                print(f'[Observer] Warn via {message.author.name}: {warn_data["target_name"]} — {warn_data["reason"]}')
                api_post('/observer/warn', {
                    'source_bot': message.author.name,
                    'mod_name': warn_data['mod_name'],
                    'target_name': warn_data['target_name'],
                    'reason': warn_data['reason'],
                    'channel': message.channel.name,
                    'guild': message.guild.name,
                    'raw_embed': warn_data['raw'],
                    'timestamp': datetime.now(timezone.utc).isoformat()
                })

    # Job 2: Track staff activity (non-bot members with mod permissions)
    if not message.author.bot:
        member = message.guild.get_member(message.author.id)
        if member:
            is_staff = any(
                r.permissions.ban_members or
                r.permissions.kick_members or
                r.permissions.manage_messages
                for r in member.roles
            )
            if is_staff:
                api_post('/observer/activity', {
                    'discord_id': str(member.id),
                    'staff_name': member.name,
                    'channel': message.channel.name,
                    'guild': message.guild.name,
                    'message_length': len(message.content),
                    'timestamp': datetime.now(timezone.utc).isoformat()
                })

    await bot.process_commands(message)


# ─────────────────────────────────────────────
# BACKGROUND TASK — Confirm Stale Bans
# ─────────────────────────────────────────────

@tasks.loop(hours=1)
async def check_reversed_actions():
    """
    Every hour: any ban that has sat in pending_bans for 48+ hours
    without reversal is confirmed as a valid moderation action.
    """
    now = datetime.now(timezone.utc)
    to_confirm = [
        key for key, data in pending_bans.items()
        if (now - data['timestamp']).total_seconds() / 3600 > BAN_WATCH_HOURS
    ]

    for key in to_confirm:
        data = pending_bans.pop(key)
        print(f'[Observer] Ban confirmed valid: {data["user_name"]} by {data["banner_name"]}')
        api_post('/observer/confirm', {
            'discord_id': data['banner_id'],
            'staff_name': data['banner_name'],
            'action_type': 'ban_confirmed',
            'target': data['user_name'],
            'guild': data['guild_name'],
            'note': 'Ban stood for 48+ hours — confirmed as valid moderation action',
            'timestamp': now.isoformat()
        })


if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
