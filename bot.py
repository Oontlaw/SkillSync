import discord
from discord.ext import commands, tasks
import requests
import os
import asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import bot_commands

# File-based logging (persists across process deaths)
LOG_FILE = os.path.join(os.environ.get('TEMP', 'C:\\Temp'), 'skillsync_bot.log')
def log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}\n')
    except:
        pass

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
if not DISCORD_TOKEN:
    log('CRITICAL: DISCORD_TOKEN not set')
    raise RuntimeError('DISCORD_TOKEN environment variable is required')

SKILLSYNC_API = os.getenv('SKILLSYNC_API', 'http://localhost:5000/api')
API_KEY = os.getenv('API_KEY')
if not API_KEY:
    log('CRITICAL: API_KEY not set')
    raise RuntimeError('API_KEY environment variable is required')

# How long to watch a ban before deciding it's "confirmed" (in hours)
BAN_WATCH_HOURS = 48

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True
intents.guilds = True
intents.presences = True

# ── Prefix cache: { guild_id: [prefixes] } ──
# Fetched from API on startup. Each guild can have multiple prefixes.
# '!ss ' is always included as the default.
prefix_cache = {}

def get_prefix(bot_, message):
    """Dynamic prefix per guild + bot mention. Falls back to ['!ss ']."""
    prefixes = ['!ss ']
    if message.guild:
        prefixes = prefix_cache.get(str(message.guild.id), ['!ss '])
    # Ensure !ss is always available
    if '!ss ' not in prefixes:
        prefixes = ['!ss '] + prefixes
    return commands.when_mentioned_or(*prefixes)(bot_, message)

bot = commands.Bot(command_prefix=get_prefix, intents=intents)

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
        requests.post(f'{SKILLSYNC_API}{endpoint}', json=payload,
                     headers={'Authorization': f'Bearer {API_KEY}'}, timeout=5)
    except Exception as e:
        print(f'[SkillSync Observer] API error on {endpoint}: {e}')

def is_mod_bot(member):
    """Check if a member is a known moderation bot."""
    if not member or not member.bot:
        return False
    return any(name in member.name.lower() for name in MOD_BOT_NAMES)

def is_channel_public(channel, guild):
    """Check if a channel is accessible to @everyone (public).
    Private channels (mod-only, staff-only) don't get content stored."""
    try:
        overwrites = channel.overwrites_for(guild.default_role)
        return overwrites.read_messages is not False
    except:
        return True

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
# STAFF ACTIVITY PROXIMITY & EMBED PARSING
# ─────────────────────────────────────────────

# Tracks the most recently active staff member per guild.
# When a mod bot action fires, we attribute it to whoever was active last.
# { guild_id: { 'id': int, 'name': str, 'timestamp': datetime } }
last_staff_activity = {}

# Behavioral message buffer — collects all human messages for analysis
# Flushed to API every 30 messages or 30 seconds
message_buffer = []
MESSAGE_BUFFER_LIMIT = 30

# Presence change buffer — batch-sent to API to avoid spam
presence_buffer = []
PRESENCE_BUFFER_LIMIT = 50

def flush_message_buffer():
    """Send buffered messages to the API for behavioral analysis."""
    global message_buffer
    if not message_buffer:
        return
    batch = message_buffer[:]
    message_buffer = []
    api_post('/observer/messages', batch)
    log(f'FLUSHED {len(batch)} messages to behavioral log')

def flush_presence_buffer():
    """Send buffered presence updates to the API."""
    global presence_buffer
    if not presence_buffer:
        return
    batch = presence_buffer[:]
    presence_buffer = []
    api_post('/observer/activity', {'batch': True, 'updates': batch})
    log(f'FLUSHED {len(batch)} presence updates')

@bot.event
async def on_message(message):
    """
    Three jobs:
    1. Parse mod bot embed responses (warns/infractions)
    2. Track staff activity — who was the last active mod per guild
    3. Forward commands
    """
    guild = message.guild
    if not guild:
        return

    log(f'MSG: {message.author.name} in #{message.channel.name} (len={len(message.content)})')

    # ── Job 1: Read mod bot embeds (warns, infractions) ──
    if is_mod_bot(message.author) and message.embeds:
        for embed in message.embeds:
            warn_data = extract_warn_from_embed(embed)
            if warn_data:
                print(f'[Observer] Warn via {message.author.name}: {warn_data["target_name"]} - {warn_data["reason"]}')
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
        return

    if message.author.bot:
        return

    now = datetime.now(timezone.utc)

    # Clean stale proximity entries (>60s)
    stale = [gid for gid, data in last_staff_activity.items() if (now - data['timestamp']).total_seconds() > 60]
    for gid in stale:
        del last_staff_activity[gid]

    # ── Job 2: Track potential moderator activity ──
    # Track ANY human who sends a message that looks like a mod bot command
    # (e.g. ,ban @user, !kick @user, .warn @user)
    content_lower = message.content.lower().lstrip()
    is_mod_command = False
    for prefix in ['!', '.', '/', '-', ',', '?']:
        for action in ['ban', 'kick', 'timeout', 'mute', 'warn']:
            if content_lower.startswith(f'{prefix}{action} '):
                is_mod_command = True
                break
        if is_mod_command:
            break

    # Use message.author directly (should be Member in guilds, but fallback to User)
    author = message.author
    is_member = hasattr(author, 'roles')  # True if Member object, False if User

    if is_mod_command:
        # Track EVERY mod command issuer
        last_staff_activity[guild.id] = {
            'id': author.id,
            'name': author.name,
            'timestamp': now
        }
        log(f'MOD CMD: {author.name} issued {content_lower[:40]}')
        print(f'[Observer] Mod command by {author.name}: {content_lower[:40]}')
    elif is_member:
        # Also track staff members with native Discord mod perms
        is_staff = any(
            r.permissions.ban_members or
            r.permissions.kick_members or
            r.permissions.manage_messages
            for r in author.roles
        )
        if is_staff:
            last_staff_activity[guild.id] = {
                'id': author.id,
                'name': author.name,
                'timestamp': now
            }
            log(f'STAFF ACTIVE: {author.name} in {guild.name}')
            api_post('/observer/activity', {
                'discord_id': str(author.id),
                'staff_name': author.name,
                'channel': message.channel.name,
                'guild': message.guild.name,
                'message_length': len(message.content),
                'timestamp': now.isoformat()
            })

    # ── Job 3: Buffer EVERY human message for behavioral analysis ──
    message_buffer.append({
        'discord_id': str(author.id),
        'name': author.name,
        'guild_id': str(guild.id),
        'channel': message.channel.name,
        'length': len(message.content),
        'content': message.content,
        'hour': now.hour,
        'day': now.weekday(),
    })
    if len(message_buffer) >= MESSAGE_BUFFER_LIMIT:
        flush_message_buffer()

    await bot.process_commands(message)


# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# GUILD SCANNING — Staff / Community Analysis
# ─────────────────────────────────────────────

def scan_guild(guild):
    """
    Full scan of a guild: roles, members, permissions, hierarchy.
    Determines mod roles and staff members automatically.
    Posts results to SkillSync API.
    """
    log(f'SCANNING guild: {guild.name} (ID: {guild.id})')
    print(f'[SkillSync] Scanning guild: {guild.name} ({guild.id})')

    # ── 1. Owner ──
    owner_id = str(guild.owner_id) if guild.owner_id else None
    owner_name = None
    if guild.owner:
        owner_name = guild.owner.name

    # ── 2. Roles (sorted by position descending for hierarchy) ──
    mod_role_ids = set()
    roles_data = []
    for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if role.is_default():
            continue
        rd = {
            'role_id': str(role.id),
            'name': role.name,
            'position': role.position,
            'color': str(role.color) if role.color else None,
            'is_admin': role.permissions.administrator,
            'can_ban': role.permissions.ban_members,
            'can_kick': role.permissions.kick_members,
            'can_manage_messages': role.permissions.manage_messages,
            'can_manage_guild': role.permissions.manage_guild,
            'can_manage_roles': role.permissions.manage_roles,
            'is_mod': False,
            'member_count': len(role.members) if hasattr(role, 'members') else 0,
        }
        # Auto-determine mod roles
        rd['is_mod'] = any([
            rd['is_admin'],
            rd['can_ban'],
            rd['can_kick'],
            rd['can_manage_guild'],
            rd['can_manage_roles'],
        ])
        if rd['is_mod']:
            mod_role_ids.add(str(role.id))
        roles_data.append(rd)

    # ── 3. Members ──
    members_data = []
    staff_member_ids = []
    bot_count = 0
    online_count = 0
    is_large_guild = guild.member_count > 1000

    for member in guild.members:
        # For large guilds (1000+), only scan online members + bots + staff
        if is_large_guild and not member.bot and member.status == discord.Status.offline:
            # Check if this offline member might be staff (need to check roles)
            is_staff = any(str(r.id) in mod_role_ids for r in member.roles)
            if not is_staff:
                continue

        md = {
            'member_id': str(member.id),
            'name': member.name,
            'display_name': member.display_name,
            'joined_at': member.joined_at.isoformat() if member.joined_at else None,
            'is_bot': member.bot,
            'is_owner': str(member.id) == owner_id,
            'is_staff': False,
            'role_ids': ','.join(str(r.id) for r in member.roles if not r.is_default()),
            'top_role_position': member.top_role.position if member.top_role else 0,
        }
        # Auto-determine staff: owner OR has any mod role
        is_staff = md['is_owner'] or any(str(r.id) in mod_role_ids for r in member.roles)
        md['is_staff'] = is_staff
        members_data.append(md)

        if is_staff and not member.bot:
            staff_member_ids.append(str(member.id))
        if member.bot:
            bot_count += 1
        if member.status != discord.Status.offline:
            online_count += 1

    # ── 4. Post to API ──
    import json
    payload = {
        'guild_id': str(guild.id),
        'name': guild.name,
        'prefix': json.dumps(prefix_cache.get(str(guild.id), ['!ss '])),
        'owner_id': owner_id,
        'owner_name': owner_name,
        'member_count': guild.member_count,
        'online_count': online_count,
        'staff_count': len(staff_member_ids),
        'bot_count': bot_count,
        'role_count': len(roles_data),
        'roles': roles_data,
        'members': members_data,
    }

    api_post('/observer/guild-scan', payload)
    log(f'SCAN COMPLETE: {guild.name} — {len(roles_data)} roles, {guild.member_count} members, {len(staff_member_ids)} staff')
    print(f'[SkillSync] Scan complete: {guild.name} — {len(staff_member_ids)} staff out of {guild.member_count} members')


@bot.event
async def on_ready():
    log(f'Bot online as {bot.user}')
    log(f'Watching {len(bot.guilds)} server(s)')
    for guild in bot.guilds:
        log(f'  - Guild: {guild.name} (ID: {guild.id})')
        me = guild.me
        if me:
            perms = dict(me.guild_permissions)
            log(f'    Perms: ban_members={perms["ban_members"]}, kick_members={perms["kick_members"]}, view_audit_log={perms["view_audit_log"]}, manage_messages={perms["manage_messages"]}')
            if not perms.get('view_audit_log'):
                log(f'    ⚠️  Bot LACKS view_audit_log permission in {guild.name}! Ban/kick/timeout detection will NOT work.')
                print(f'[SkillSync] WARNING: Bot lacks view_audit_log permission in {guild.name}')
    print(f'[SkillSync] Bot online as {bot.user}')
    print(f'[SkillSync] Watching {len(bot.guilds)} server(s)')
    check_reversed_actions.start()
    flush_message_loop.start()
    # Fetch prefixes from API
    try:
        resp = requests.get(f'{SKILLSYNC_API}/observer/guilds', headers={'Authorization': f'Bearer {API_KEY}'}, timeout=5)
        if resp.ok:
            for g in resp.json():
                prefix_cache[g['guild_id']] = g.get('prefixes', ['!ss '])
    except Exception as e:
        print(f'[SkillSync] Could not fetch prefixes: {e}')
    # Set bot presence with prefix info
    first_prefixes = prefix_cache.get(str(bot.guilds[0].id), ['!ss ']) if bot.guilds else ['!ss ']
    await bot.change_presence(activity=discord.Game(name=f'{first_prefixes[0].strip()} | Observe & Moderate'))
    # Scan all existing guilds on startup
    for guild in bot.guilds:
        scan_guild(guild)


@bot.event
async def on_guild_join(guild):
    """Fires when the bot joins a new server. Full scan immediately."""
    log(f'JOINED new guild: {guild.name} (ID: {guild.id})')
    print(f'[SkillSync] Joined new guild: {guild.name}')
    prefix_cache[str(guild.id)] = ['!ss ']
    scan_guild(guild)


# ─────────────────────────────────────────────
# LARGE GUILD TRACKING — Online Members
# ─────────────────────────────────────────────

@bot.event
async def on_presence_update(before, after):
    """Buffer members coming online/offline to avoid API spam."""
    if before.status == after.status or not after.guild:
        return
    presence_buffer.append({
        'discord_id': str(after.id),
        'staff_name': after.name,
        'action_type': 'presence_change',
        'old_status': str(before.status),
        'new_status': str(after.status),
        'guild': after.guild.name,
        'guild_id': str(after.guild.id),
        'timestamp': datetime.now(timezone.utc).isoformat()
    })
    if len(presence_buffer) >= PRESENCE_BUFFER_LIMIT:
        flush_presence_buffer()

@bot.event
async def on_member_join(member):
    """Track new members joining the server."""
    api_post('/observer/activity', {
        'discord_id': str(member.id),
        'staff_name': member.name,
        'action_type': 'member_join',
        'guild': member.guild.name,
        'joined_at': member.joined_at.isoformat() if member.joined_at else None,
        'timestamp': datetime.now(timezone.utc).isoformat()
    })


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
    try:
        log(f'on_member_ban FIRED: user={user.name} (id={user.id}) in guild={guild.name}')
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
            log(f'AUDIT LOG ERROR: {e}')

        if banner_id is None:
            log(f'BANNER ID IS None - cannot identify banner (need View Audit Log permission)')
            print(f'[Observer] Ban detected but could not identify banner (missing audit log permission?)')
            api_post('/observer/action', {
                'discord_id': None,
                'staff_name': 'Unknown',
                'action_type': 'ban_issued',
                'target': user.name,
                'guild': guild.name,
                'reason': reason,
                'flagged': False,
                'flag_reason': 'Could not identify moderator - bot lacks View Audit Log permission',
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
            return

        # If banner is a mod bot, attribute to the most recently active staff member
        if is_mod_bot(guild.get_member(banner_id)):
            log(f'Banner is mod bot ({banner_name}), checking staff proximity for guild={guild.id}')
            recent = last_staff_activity.get(guild.id)
            if recent and (datetime.now(timezone.utc) - recent['timestamp']).total_seconds() < 60:
                banner_id = recent['id']
                banner_name = recent['name']
                log(f'Proximity match! Attributed to: {banner_name} (active {(datetime.now(timezone.utc) - recent["timestamp"]).total_seconds():.0f}s ago)')
                print(f'[Observer] Ban by mod bot — attributed to staff: {banner_name}')
            else:
                log(f'No recent staff activity for guild={guild.id} — skipping ban')
                print(f'[Observer] Ban by mod bot {banner_name} — skipping (no staff nearby)')
                return

        log(f'Ban LOGGED: {user.name} by {banner_name} in {guild.name}')
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
    except Exception as e:
        print(f'[Observer] Error in on_member_ban: {e}')


@bot.event
async def on_member_unban(guild, user):
    """
    Fires when a ban is lifted.
    If the ban was recent (under BAN_WATCH_HOURS), flag it as hasty/wrongful.
    """
    try:
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
    except Exception as e:
        print(f'[Observer] Error in on_member_unban: {e}')


# ─────────────────────────────────────────────
# TIMEOUT / MUTE DETECTION
# ─────────────────────────────────────────────

@bot.event
async def on_member_update(before, after):
    """
    Detects Discord timeouts being added or removed early.
    Early removal = possible overreach by original mod.
    """
    try:
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

            if mod_id is None:
                print(f'[Observer] Timeout detected but could not identify moderator (missing audit log permission?)')
                return

            # If moderator is a mod bot, attribute to the most recently active staff member
            if is_mod_bot(guild.get_member(mod_id)):
                log(f'Timeout by mod bot ({mod_name}), checking staff proximity for guild={guild.id}')
                recent = last_staff_activity.get(guild.id)
                if recent and (datetime.now(timezone.utc) - recent['timestamp']).total_seconds() < 60:
                    mod_id = recent['id']
                    mod_name = recent['name']
                    log(f'Proximity match for timeout! Attributed to: {mod_name}')
                    print(f'[Observer] Timeout by mod bot — attributed to staff: {mod_name}')
                else:
                    log(f'No recent staff activity for guild={guild.id} — skipping timeout')
                    print(f'[Observer] Timeout by mod bot {mod_name} — skipping (no staff nearby)')
                    return

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
    except Exception as e:
        print(f'[Observer] Error in on_member_update: {e}')


# ─────────────────────────────────────────────
# KICK DETECTION
# ─────────────────────────────────────────────

@bot.event
async def on_member_remove(member):
    """
    Fires when a member leaves or is kicked.
    We check audit logs to confirm it was a kick (not a voluntary leave).
    """
    try:
        await asyncio.sleep(1)
        guild = member.guild

        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                if entry.target.id == member.id:
                    kicker_id = entry.user.id
                    kicker_name = entry.user.name

                    # If kicker is a mod bot, attribute to the most recently active staff member
                    if is_mod_bot(entry.user):
                        log(f'Kick by mod bot ({kicker_name}), checking staff proximity for guild={guild.id}')
                        recent = last_staff_activity.get(guild.id)
                        if recent and (datetime.now(timezone.utc) - recent['timestamp']).total_seconds() < 60:
                            kicker_id = recent['id']
                            kicker_name = recent['name']
                            log(f'Proximity match for kick! Attributed to: {kicker_name}')
                            print(f'[Observer] Kick by mod bot — attributed to staff: {kicker_name}')
                        else:
                            log(f'No recent staff activity for guild={guild.id} — skipping kick')
                            print(f'[Observer] Kick by mod bot {entry.user.name} — skipping (no staff nearby)')
                            return
                    else:
                        print(f'[Observer] Kick: {member.name} by {kicker_name}')

                    api_post('/observer/action', {
                        'discord_id': str(kicker_id),
                        'staff_name': kicker_name,
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
    except Exception as e:
        print(f'[Observer] Error in on_member_remove: {e}')
    # If no audit log match — member left voluntarily, do nothing


# ─────────────────────────────────────────────
# BACKGROUND TASK — Confirm Stale Bans
# ─────────────────────────────────────────────

@tasks.loop(seconds=30)
async def flush_message_loop():
    """Flush buffered messages and presence updates every 30 seconds."""
    flush_message_buffer()
    flush_presence_buffer()

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

    # Also scan for behavioral anomalies
    print(f'[Observer] Scanning behavioral anomalies...')
    api_post('/observer/anomalies/scan', {'trigger': 'hourly'})


if __name__ == '__main__':
    # Attach shared state to bot instance for cog access
    bot.prefix_cache = prefix_cache
    bot.scan_guild = scan_guild
    import asyncio
    async def main():
        await bot.add_cog(bot_commands.Moderation(bot))
        await bot.start(DISCORD_TOKEN)
    asyncio.run(main())
