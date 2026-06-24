import discord
from datetime import datetime, timezone
from bot_core.config import (
    MAX_BUFFER_SIZE, PRESENCE_BUFFER_LIMIT,
    JOIN_BUFFER_LIMIT, JOIN_LEAVE_BUFFER_LIMIT, MEMBER_PRESENCE_BUFFER_LIMIT,
    VOICE_BUFFER_LIMIT,
)
from bot_core.state import (
    presence_buffer, member_presence_buffer, join_buffer, join_leave_buffer,
    voice_sessions, voice_buffer, active_pings,
    track_online, track_offline,
)
from bot_core.tasks import flush_presence_buffer, flush_member_presence_buffer
from bot_core.tasks import flush_join_buffer, flush_voice_buffer, flush_join_leave_buffer
from bot_core.logging import log


async def handle_presence_update(before, after):
    """Buffer members coming online/offline to avoid API spam.
    Also updates GuildMember presence for the persistent member registry."""
    if before.status == after.status or not after.guild:
        return

    guild_id = str(after.guild.id)
    if after.status != discord.Status.offline and before.status == discord.Status.offline:
        track_online(guild_id, after.id)
    elif after.status == discord.Status.offline and before.status != discord.Status.offline:
        track_offline(guild_id, after.id)
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
    if len(presence_buffer) > MAX_BUFFER_SIZE:
        presence_buffer[:] = presence_buffer[-MAX_BUFFER_SIZE:]
    if len(presence_buffer) >= PRESENCE_BUFFER_LIMIT:
        await flush_presence_buffer()

    if not after.bot:
        activities = [a for a in after.activities if not isinstance(a, discord.CustomActivity)]
        primary = activities[0] if activities else None
        member_presence_buffer.append({
            'guild_id': str(after.guild.id),
            'member_id': str(after.id),
            'name': after.name,
            'display_name': after.display_name,
            'is_online': after.status != discord.Status.offline,
            'status': str(after.status),
            'activity_name': primary.name if primary else None,
            'activity_type': str(primary.type).split('.')[-1] if primary else None,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
        if len(member_presence_buffer) >= MEMBER_PRESENCE_BUFFER_LIMIT:
            await flush_member_presence_buffer()
        elif len(member_presence_buffer) > MAX_BUFFER_SIZE:
            member_presence_buffer[:] = member_presence_buffer[-MAX_BUFFER_SIZE:]


async def handle_member_join(member):
    """Track new members joining — buffered. Also checks @everyone ping window."""
    log(f'DEBUG: on_member_join FIRED: {member.name} (id={member.id}) in guild={member.guild.name if member.guild else "NO_GUILD"}')
    try:
        track_online(str(member.guild.id), member.id)
        join_buffer.append({
            'discord_id': str(member.id),
            'staff_name': member.name,
            'action_type': 'member_join',
            'guild': member.guild.name,
            'joined_at': member.joined_at.isoformat() if member.joined_at else None,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
        if len(join_buffer) >= JOIN_BUFFER_LIMIT:
            await flush_join_buffer()
        elif len(join_buffer) > MAX_BUFFER_SIZE:
            join_buffer[:] = join_buffer[-MAX_BUFFER_SIZE:]

        # Add to join/leave buffer
        now = datetime.now(timezone.utc)
        join_leave_buffer.append({
            'guild_id': str(member.guild.id),
            'member_id': str(member.id),
            'member_name': member.name,
            'is_bot': member.bot,
            'event_type': 'join',
            'leave_reason': None,
            'hour_of_day': now.hour,
            'day_of_week': now.weekday(),
            'timestamp': now.isoformat()
        })
        if len(join_leave_buffer) >= JOIN_LEAVE_BUFFER_LIMIT:
            await flush_join_leave_buffer()
        elif len(join_leave_buffer) > MAX_BUFFER_SIZE:
            join_leave_buffer[:] = join_leave_buffer[-MAX_BUFFER_SIZE:]

        ping = active_pings.get(member.guild.id)
        if ping:
            ping['join_count'] += 1
            ping['joiners'].append(member.name)

        if not member.bot:
            member_presence_buffer.append({
                'guild_id': str(member.guild.id),
                'member_id': str(member.id),
                'name': member.name,
                'display_name': member.display_name,
                'is_online': member.status != discord.Status.offline,
                'status': str(member.status),
                'activity_name': None,
                'activity_type': None,
                'joined_at': member.joined_at.isoformat() if member.joined_at else None,
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
            if len(member_presence_buffer) >= MEMBER_PRESENCE_BUFFER_LIMIT:
                await flush_member_presence_buffer()
            elif len(member_presence_buffer) > MAX_BUFFER_SIZE:
                member_presence_buffer[:] = member_presence_buffer[-MAX_BUFFER_SIZE:]
    except Exception as e:
        log(f'ERROR in on_member_join: {e}')
        print(f'[SkillSync] on_member_join error: {e}')


async def handle_voice_state_update(member, before, after):
    """Track voice channel joins/leaves/moves for behavioral pattern recognition."""
    log(f'VOICE_EVENT: member={member.name} guild={member.guild.name if member.guild else "NO_GUILD"} before={before.channel.name if before.channel else "None"} after={after.channel.name if after.channel else "None"}')
    if member.bot or not member.guild:
        return
    user_id = str(member.id)
    guild_id = str(member.guild.id)
    key = (guild_id, user_id)

    def make_voice_entry(session_data, left_now):
        duration = (left_now - session_data['joined_at']).total_seconds()
        hour = session_data['joined_at'].hour
        day = session_data['joined_at'].weekday()
        return {
            'discord_id': user_id,
            'name': member.name,
            'guild_id': guild_id,
            'guild_name': member.guild.name,
            'channel_name': session_data['channel_name'],
            'duration_seconds': round(duration, 1),
            'hour_of_day': hour,
            'day_of_week': day,
            'joined_at': session_data['joined_at'].isoformat(),
            'left_at': left_now.isoformat(),
        }

    now = datetime.now(timezone.utc)

    if before.channel and not after.channel:
        session = voice_sessions.pop(key, None)
        if session:
            voice_buffer.append(make_voice_entry(session, now))
            if len(voice_buffer) >= VOICE_BUFFER_LIMIT:
                await flush_voice_buffer()
            elif len(voice_buffer) > MAX_BUFFER_SIZE:
                voice_buffer[:] = voice_buffer[-MAX_BUFFER_SIZE:]

    if after.channel:
        if before.channel and before.channel != after.channel:
            session = voice_sessions.pop(key, None)
            if session:
                voice_buffer.append(make_voice_entry(session, now))
                if len(voice_buffer) >= VOICE_BUFFER_LIMIT:
                    await flush_voice_buffer()
                elif len(voice_buffer) > MAX_BUFFER_SIZE:
                    voice_buffer[:] = voice_buffer[-MAX_BUFFER_SIZE:]

        voice_sessions[key] = {
            'channel_name': after.channel.name,
            'guild_id': guild_id,
            'joined_at': now,
        }
