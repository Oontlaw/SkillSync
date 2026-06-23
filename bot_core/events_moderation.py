import asyncio
import discord
from datetime import datetime, timedelta, timezone
from bot_core.config import BAN_WATCH_HOURS
from bot_core.state import (
    pending_bans, pending_timeouts, last_staff_activity, track_offline,
)
from bot_core.privacy import is_mod_bot
from bot_core.api_client import api_post
from bot_core.logging import log


async def handle_member_ban(guild, user):
    """Detect bans via audit log, attribute to staff, store in pending_bans."""
    try:
        track_offline(str(guild.id), user.id)
        log(f'on_member_ban FIRED: user={user.name} (id={user.id}) in guild={guild.name}')
        await asyncio.sleep(1)

        banner_id = None
        banner_name = 'Unknown'
        reason = 'No reason given'

        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
                if entry.target and entry.target.id == user.id:
                    if entry.user:
                        banner_id = entry.user.id
                        banner_name = entry.user.name
                    reason = entry.reason or 'No reason given'
                    break

            if banner_id is None:
                async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.automod_quarantine_user):
                    if entry.target and entry.target.id == user.id:
                        entry_user_name = entry.user.name if entry.user else 'Unknown'
                        banner_name = f'AutoMod ({entry_user_name})'
                        reason = f'AutoMod quarantine: {entry.reason or "No reason given"}'
                        await api_post('/observer/flag', {
                            'discord_id': str(entry.user.id) if entry.user else None,
                            'staff_name': entry_user_name,
                            'action_type': 'ban_issued',
                            'target': user.name,
                            'guild': guild.name,
                            'guild_id': str(guild.id),
                            'flagged': True,
                            'flag_reason': f'AutoMod rule triggered by {entry_user_name} -- review needed',
                            'timestamp': datetime.now(timezone.utc).isoformat()
                        })
                        log(f'AutoMod QUARANTINE detected: {user.name} triggered rule by {entry_user_name}')
                        return
        except Exception as e:
            print(f'[Observer] Could not read audit log for ban: {e}')
            log(f'AUDIT LOG ERROR: {e}')

        if banner_id is None:
            log(f'BANNER ID IS None - cannot identify banner (need View Audit Log permission)')
            print(f'[Observer] Ban detected but could not identify banner (missing audit log permission?)')
            await api_post('/observer/action', {
                'discord_id': None,
                'staff_name': 'Unknown',
                'action_type': 'ban_issued',
                'target': user.name,
                'target_id': str(user.id),
                'guild': guild.name,
                'guild_id': str(guild.id),
                'reason': reason,
                'flagged': False,
                'flag_reason': 'Could not identify moderator - bot lacks View Audit Log permission',
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
            return

        if is_mod_bot(guild.get_member(banner_id)):
            log(f'Banner is mod bot ({banner_name}), checking staff proximity for guild={guild.id}')
            recent = last_staff_activity.get(guild.id)
            if recent and (datetime.now(timezone.utc) - recent['timestamp']).total_seconds() < 60:
                banner_id = recent['id']
                banner_name = recent['name']
                log(f'Proximity match! Attributed to: {banner_name} (active {(datetime.now(timezone.utc) - recent["timestamp"]).total_seconds():.0f}s ago)')
                print(f'[Observer] Ban by mod bot -- attributed to staff: {banner_name}')
            else:
                log(f'No recent staff activity for guild={guild.id} -- skipping ban')
                print(f'[Observer] Ban by mod bot {banner_name} -- skipping (no staff nearby)')
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

        await api_post('/observer/action', {
            'discord_id': str(banner_id),
            'staff_name': banner_name,
            'action_type': 'ban_issued',
            'target': user.name,
            'target_id': str(user.id),
            'guild': guild.name,
            'guild_id': str(guild.id),
            'reason': reason,
            'flagged': False,
            'flag_reason': None,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        print(f'[Observer] Error in on_member_ban: {e}')


async def handle_member_unban(guild, user):
    """Detect ban reversal and flag if hasty."""
    try:
        key = (guild.id, user.id)
        ban_data = pending_bans.pop(key, None)

        unbanner_name = 'Unknown'
        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.unban):
                if entry.target and entry.target.id == user.id:
                    unbanner_name = entry.user.name if entry.user else 'Unknown'
                    break
        except Exception as e:
            log(f'UNBAN AUDIT LOG ERROR in {guild.name}: {e}')
            print(f'[Observer] Could not read audit log for unban in {guild.name}: {e}')

        if ban_data:
            elapsed = datetime.now(timezone.utc) - ban_data['timestamp']
            hours_elapsed = elapsed.total_seconds() / 3600
            is_hasty = hours_elapsed <= BAN_WATCH_HOURS
            print(f'[Observer] Unban: {user.name} -- {hours_elapsed:.1f}h later -- flagged: {is_hasty}')
            await api_post('/observer/flag', {
                'discord_id': ban_data['banner_id'],
                'staff_name': ban_data['banner_name'],
                'action_type': 'ban_reversed',
                'target': user.name,
                'guild': ban_data['guild_name'],
                'guild_id': str(ban_data['guild_id']),
                'original_reason': ban_data['reason'],
                'reversed_by': unbanner_name,
                'hours_until_reversal': round(hours_elapsed, 2),
                'flagged': is_hasty,
                'flag_reason': 'Ban reversed within 48 hours -- possible wrongful ban' if is_hasty else None,
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
        else:
            print(f'[Observer] Unban: {user.name} -- no original ban data found in pending_bans')
    except Exception as e:
        print(f'[Observer] Error in on_member_unban: {e}')


def _is_mod_role(role):
    """Check if a Discord role grants moderation powers."""
    return any([
        role.permissions.administrator,
        role.permissions.ban_members,
        role.permissions.kick_members,
        role.permissions.manage_guild,
        role.permissions.manage_roles,
    ])


async def _handle_role_change(before, after, guild):
    """Detect staff role changes (promotion/demotion/retirement/reactivation)."""
    if set(before.roles) == set(after.roles):
        return

    added = [r for r in after.roles if r not in before.roles]
    removed = [r for r in before.roles if r not in after.roles]

    added_mod = [r for r in added if _is_mod_role(r)]
    removed_mod = [r for r in removed if _is_mod_role(r)]

    if not added_mod and not removed_mod:
        return

    was_staff = any(_is_mod_role(r) for r in before.roles) or before.guild_permissions.administrator
    is_staff = any(_is_mod_role(r) for r in after.roles) or after.guild_permissions.administrator
    has_retired_role = any('retire' in r.name.lower() or 'emeritus' in r.name.lower() or 'inactive' in r.name.lower() for r in added + after.roles)

    # Determine category
    if not was_staff and is_staff:
        category = 'promotion'
    elif was_staff and not is_staff:
        if has_retired_role:
            category = 'retirement'
        else:
            category = 'demotion'
    else:
        category = 'other'

    # Find who made the change via audit log
    modifier_id = None
    modifier_name = None
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_role_update):
            if entry.target and entry.target.id == after.id and entry.user:
                modifier_id = entry.user.id
                modifier_name = entry.user.name
                break
    except Exception as e:
        log(f'ROLE CHANGE AUDIT LOG ERROR in {guild.name}: {e}')

    # Log each changed mod role
    for role in added_mod:
        log(f'ROLE CHANGE: {after.name} GAINED mod role {role.name} ({category}) in {guild.name}')
        await api_post('/observer/role-change', {
            'guild_id': str(guild.id),
            'member_id': str(after.id),
            'member_name': after.name,
            'change_type': 'added',
            'role_id': str(role.id),
            'role_name': role.name,
            'change_category': category,
            'was_staff_before': was_staff,
            'is_staff_now': is_staff,
            'modifier_id': str(modifier_id) if modifier_id else None,
            'modifier_name': modifier_name,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })

    for role in removed_mod:
        log(f'ROLE CHANGE: {after.name} LOST mod role {role.name} ({category}) in {guild.name}')
        await api_post('/observer/role-change', {
            'guild_id': str(guild.id),
            'member_id': str(after.id),
            'member_name': after.name,
            'change_type': 'removed',
            'role_id': str(role.id),
            'role_name': role.name,
            'change_category': category,
            'was_staff_before': was_staff,
            'is_staff_now': is_staff,
            'modifier_id': str(modifier_id) if modifier_id else None,
            'modifier_name': modifier_name,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })


async def handle_member_update(before, after):
    """Detect timeout add/early removal and staff role changes."""
    try:
        guild = after.guild
        if not guild:
            return

        await _handle_role_change(before, after, guild)

        if before.timed_out_until is None and after.timed_out_until is not None:
            await asyncio.sleep(1)
            mod_id = None
            mod_name = 'Unknown'
            reason = 'No reason given'

            try:
                async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
                    if entry.target and entry.target.id == after.id:
                        mod_id = entry.user.id if entry.user else None
                        mod_name = entry.user.name if entry.user else 'Unknown'
                        reason = entry.reason or 'No reason given'
                        break

                if mod_id is None:
                    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.automod_timeout_member):
                        if entry.target and entry.target.id == after.id:
                            entry_user_name = entry.user.name if entry.user else 'Unknown'
                            mod_name = f'AutoMod ({entry_user_name})'
                            reason = f'AutoMod timeout: {entry.reason or "No reason given"}'
                            mod_id = entry.user.id if entry.user else None
                            log(f'AutoMod TIMEOUT detected: {after.name} triggered rule by {entry_user_name}')
                            break
            except Exception as e:
                log(f'TIMEOUT AUDIT LOG ERROR in {guild.name}: {e}')
                print(f'[Observer] Could not read audit log for timeout in {guild.name}: {e}')

            if mod_id is None:
                print(f'[Observer] Timeout detected but could not identify moderator')
                return

            if is_mod_bot(guild.get_member(mod_id)):
                log(f'Timeout by mod bot ({mod_name}), checking staff proximity for guild={guild.id}')
                recent = last_staff_activity.get(guild.id)
                if recent and (datetime.now(timezone.utc) - recent['timestamp']).total_seconds() < 60:
                    mod_id = recent['id']
                    mod_name = recent['name']
                    log(f'Proximity match for timeout! Attributed to: {mod_name}')
                    print(f'[Observer] Timeout by mod bot -- attributed to staff: {mod_name}')
                else:
                    log(f'No recent staff activity for guild={guild.id} -- skipping timeout')
                    print(f'[Observer] Timeout by mod bot {mod_name} -- skipping (no staff nearby)')
                    return

            key = (guild.id, after.id)
            pending_timeouts[key] = {
                'mod_id': str(mod_id),
                'mod_name': mod_name,
                'until': after.timed_out_until,
                'timestamp': datetime.now(timezone.utc)
            }

            duration_minutes = int((after.timed_out_until - datetime.now(timezone.utc)).total_seconds() / 60) if after.timed_out_until else None
            print(f'[Observer] Timeout: {after.name} by {mod_name} ({duration_minutes} min)')
            await api_post('/observer/action', {
                'discord_id': str(mod_id),
                'staff_name': mod_name,
                'action_type': 'timeout_issued',
                'target': after.name,
                'target_id': str(after.id),
                'guild': guild.name,
                'guild_id': str(guild.id),
                'duration_minutes': duration_minutes,
                'reason': reason,
                'flagged': False,
                'flag_reason': None,
                'timestamp': datetime.now(timezone.utc).isoformat()
            })

        elif before.timed_out_until is not None and after.timed_out_until is None:
            key = (guild.id, after.id)
            timeout_data = pending_timeouts.pop(key, None)

            if timeout_data:
                now = datetime.now(timezone.utc)
                removed_early = now < timeout_data['until'] - timedelta(minutes=5)
                await api_post('/observer/flag', {
                    'discord_id': timeout_data['mod_id'],
                    'staff_name': timeout_data['mod_name'],
                    'action_type': 'timeout_reversed',
                    'target': after.name,
                    'guild': guild.name,
                    'guild_id': str(guild.id),
                    'flagged': removed_early,
                    'flag_reason': 'Timeout lifted before natural expiry -- possible overreach' if removed_early else None,
                    'timestamp': now.isoformat()
                })
    except Exception as e:
        print(f'[Observer] Error in on_member_update: {e}')


async def handle_member_remove(member):
    """Detect kicks via audit log."""
    try:
        if member.guild:
            track_offline(str(member.guild.id), member.id)
        await asyncio.sleep(1)
        guild = member.guild

        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                if entry.target and entry.target.id == member.id:
                    kicker_id = entry.user.id if entry.user else None
                    kicker_name = entry.user.name if entry.user else 'Unknown'

                    if is_mod_bot(entry.user):
                        log(f'Kick by mod bot ({kicker_name}), checking staff proximity for guild={guild.id}')
                        recent = last_staff_activity.get(guild.id)
                        if recent and (datetime.now(timezone.utc) - recent['timestamp']).total_seconds() < 60:
                            kicker_id = recent['id']
                            kicker_name = recent['name']
                            log(f'Proximity match for kick! Attributed to: {kicker_name}')
                            print(f'[Observer] Kick by mod bot -- attributed to staff: {kicker_name}')
                        else:
                            log(f'No recent staff activity for guild={guild.id} -- skipping kick')
                            print(f'[Observer] Kick by mod bot {entry.user.name} -- skipping (no staff nearby)')
                            return
                    else:
                        print(f'[Observer] Kick: {member.name} by {kicker_name}')

                    await api_post('/observer/action', {
                        'discord_id': str(kicker_id),
                        'staff_name': kicker_name,
                        'action_type': 'kick_issued',
                        'target': member.name,
                        'target_id': str(member.id),
                        'guild': guild.name,
                        'guild_id': str(guild.id),
                        'reason': entry.reason or 'No reason given',
                        'flagged': False,
                        'flag_reason': None,
                        'timestamp': datetime.now(timezone.utc).isoformat()
                    })
                    return
        except Exception as e:
            log(f'KICK AUDIT LOG ERROR in {guild.name}: {e}')
            print(f'[Observer] Could not read audit log for kick in {guild.name}: {e}')
    except Exception as e:
        print(f'[Observer] Error in on_member_remove: {e}')
