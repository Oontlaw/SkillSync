from datetime import datetime, timezone
from bot_core.config import MAX_BUFFER_SIZE, MESSAGE_BUFFER_LIMIT, MENTION_BUFFER_LIMIT
from bot_core.state import (
    last_staff_activity, content_trust, message_buffer, mention_buffer,
    pending_mentions, automod_alert_channels, active_pings,
)
from bot_core.privacy import is_channel_public, is_mod_bot
from bot_core.parsers import extract_warn_from_embed, extract_automod_alert
from bot_core.api_client import api_post
from bot_core.state import flush_message_buffer, flush_mention_buffer
from bot_core.logging import log


async def handle_message(bot, message):
    """
    Jobs:
    1. Parse mod bot embed responses (warns/infractions)
    2. Track staff activity
    3. Forward commands
    4. Track mentions and reply times
    5. Detect @everyone / @here pings by staff
    6. Parse AutoMod alert embeds from alert channels
    """
    from bot_core.state import message_buffer
    log(f'DEBUG: handle_message called for {message.author.name}. Buffer: {len(message_buffer)}')
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
                await api_post('/observer/warn', {
                    'source_bot': message.author.name,
                    'mod_name': warn_data['mod_name'],
                    'target_name': warn_data['target_name'],
                    'reason': warn_data['reason'],
                    'channel': message.channel.name,
                    'guild': message.guild.name,
                    'guild_id': str(message.guild.id),
                    'raw_embed': warn_data['raw'],
                    'timestamp': datetime.now(timezone.utc).isoformat()
                })
        return

    # ── Job 6: Parse AutoMod alert embeds ──
    guild_id_str = str(guild.id)
    channel_id_str = str(message.channel.id)
    if guild_id_str in automod_alert_channels and channel_id_str in automod_alert_channels[guild_id_str]:
        if message.embeds:
            for embed in message.embeds:
                am_data = extract_automod_alert(embed)
                if am_data:
                    am_data['guild_id'] = guild_id_str
                    print(f'[AutoMod] Trigger: {am_data["rule_name"]} -> {am_data["user_name"]} in #{am_data["channel_name"]}')
                    await api_post('/observer/automod-trigger', am_data)

    if message.author.bot:
        return

    now = datetime.now(timezone.utc)

    # Clean stale proximity entries (>60s)
    stale = [gid for gid, data in last_staff_activity.items() if (now - data['timestamp']).total_seconds() > 60]
    for gid in stale:
        del last_staff_activity[gid]

    # ── Job 2: Track potential moderator activity ──
    content_lower = message.content.lower().lstrip()
    is_mod_command = False
    for prefix in ['!', '.', '/', '-', ',', '?']:
        for action in ['ban', 'kick', 'timeout', 'mute', 'warn']:
            if content_lower.startswith(f'{prefix}{action} '):
                is_mod_command = True
                break
        if is_mod_command:
            break

    author = message.author
    is_member = hasattr(author, 'roles')

    if is_mod_command:
        last_staff_activity[guild.id] = {
            'id': author.id,
            'name': author.name,
            'timestamp': now
        }
        log(f'MOD CMD: {author.name} issued {content_lower[:40]}')
        print(f'[Observer] Mod command by {author.name}: {content_lower[:40]}')
    elif is_member:
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
            await api_post('/observer/activity', {
                'discord_id': str(author.id),
                'staff_name': author.name,
                'channel': message.channel.name,
                'guild': message.guild.name,
                'guild_id': str(guild.id),
                'message_length': len(message.content),
                'timestamp': now.isoformat()
            })

    # ── Job 3: Buffer human messages for behavioral analysis ──
    is_public = is_channel_public(message.channel, guild)
    entry = {
        'discord_id': str(author.id),
        'name': author.name,
        'guild_id': str(guild.id),
        'channel': message.channel.name,
        'length': len(message.content),
        'is_public': is_public,
        'hour': now.hour,
        'day': now.weekday(),
    }
    if is_public:
        entry['content'] = message.content
    message_buffer.append(entry)
    if len(message_buffer) >= MESSAGE_BUFFER_LIMIT:
        await flush_message_buffer()
    elif len(message_buffer) > MAX_BUFFER_SIZE:
        message_buffer[:] = message_buffer[-MAX_BUFFER_SIZE:]

    # ── Job 4: Track mentions and reply times ──
    for mentioned in message.mentions:
        if mentioned.bot:
            continue
        mention_entry = {
            'mentioner_id': str(author.id),
            'mentioner_name': author.name,
            'mentioned_id': str(mentioned.id),
            'mentioned_name': mentioned.name,
            'guild_id': str(guild.id),
            'channel_name': message.channel.name,
            'timestamp': now.isoformat(),
            'reply_time_seconds': None,
        }
        mention_buffer.append(mention_entry)
        if len(mention_buffer) > MAX_BUFFER_SIZE:
            mention_buffer[:] = mention_buffer[-MAX_BUFFER_SIZE:]
        pending_mentions[(message.channel.id, mentioned.id)] = {
            'ts': now,
            'name': author.name,
            'author_id': str(author.id),
        }

    if not message.author.bot:
        # DEBUG: Log buffer size
        from bot_core.state import message_buffer
        log(f'BUFFER SIZE: {len(message_buffer)}')
        pending_key = (message.channel.id, author.id)
        if pending_key in pending_mentions:
            data = pending_mentions.pop(pending_key)
            delta = (datetime.now(timezone.utc) - data['ts']).total_seconds()
            for entry_rev in reversed(mention_buffer):
                if entry_rev['mentioned_id'] == str(author.id) and entry_rev['channel_name'] == message.channel.name and entry_rev['reply_time_seconds'] is None:
                    entry_rev['reply_time_seconds'] = round(delta, 1)
                    break
            log(f'REPLY: {author.name} replied to {data["name"]} in {round(delta, 1)}s')

    if len(mention_buffer) >= MENTION_BUFFER_LIMIT:
        await flush_mention_buffer()

    # ── Job 5: Detect @everyone / @here pings by staff ──
    is_member = hasattr(author, 'roles')
    if ('@everyone' in message.content or '@here' in message.content) and is_member:
        is_staff_ping = any(
            r.permissions.administrator or r.permissions.mention_everyone
            for r in author.roles
        )
        if is_staff_ping:
            now_ts = datetime.now(timezone.utc)
            active_pings[guild.id] = {
                'mod_id': str(author.id),
                'mod_name': author.name,
                'channel': message.channel.name,
                'guild_name': guild.name,
                'timestamp': now_ts,
                'join_count': 0,
                'joiners': [],
            }
            log(f'PING DETECTED: {author.name} pinged @everyone in #{message.channel.name}')

    await bot.process_commands(message)
