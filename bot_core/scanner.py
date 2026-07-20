import json
import discord
import requests
from bot_core.config import SKILLSYNC_API, API_KEY
from bot_core.api_client import api_post
from bot_core.state import prefix_cache, set_automod_alert_channels, track_online, track_offline, online_members
from bot_core.logging import log


async def build_automod_alert_channels():
    """Fetch AutoMod rules from API and populate automod_alert_channels.
    
    Async — uses asyncio.to_thread to avoid blocking the event loop.
    Sync HTTP calls on the event loop thread can hang on Windows DNS
    resolution (getaddrinfo), causing zombie states.
    """
    import asyncio
    try:
        resp = await asyncio.to_thread(
            requests.get,
            f'{SKILLSYNC_API}/observer/automod-rules',
            headers={'Authorization': f'Bearer {API_KEY}'},
            timeout=5,
        )
        if resp.ok:
            rules = resp.json()
            channels = {}
            for rule in rules:
                if rule.get('alert_channel_id'):
                    gid = rule['guild_id']
                    ch = rule['alert_channel_id']
                    channels.setdefault(gid, {}).setdefault(ch, []).append(rule['name'])
            set_automod_alert_channels(channels)
            print(f'[AutoMod] Loaded {sum(len(v) for v in channels.values())} alert channels from {len(channels)} guilds')
        else:
            set_automod_alert_channels({})
    except Exception as e:
        print(f'[AutoMod] Failed to load alert channels: {e}')
        set_automod_alert_channels({})


async def scan_guild(guild):
    """
    Full scan of a guild: roles, members, permissions, hierarchy.
    Posts results to SkillSync API.
    """
    log(f'SCANNING guild: {guild.name} (ID: {guild.id})')
    print(f'[SkillSync] Scanning guild: {guild.name} ({guild.id})')

    try:
        owner_id = str(guild.owner_id) if guild.owner_id else None
        owner_name = guild.owner.name if guild.owner and guild.owner.name else 'Unknown'

        mod_role_ids = set()
        roles_data = []
        for role in sorted(guild.roles, key=lambda r: r.position or 0, reverse=True):
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
            rd['is_mod'] = any([
                rd['is_admin'], rd['can_ban'], rd['can_kick'],
                rd['can_manage_guild'], rd['can_manage_roles'],
            ])
            if rd['is_mod']:
                mod_role_ids.add(str(role.id))
            roles_data.append(rd)

        members_data = []
        staff_member_ids = []
        bot_count = 0
        online_count = 0
        is_large_guild = guild.member_count > 1000

        # Reset online set for this guild so skipped members don't leak as stale online
        online_members[str(guild.id)] = set()

        for member in guild.members:
            if is_large_guild and not member.bot and member.status == discord.Status.offline:
                is_staff = any(str(r.id) in mod_role_ids for r in member.roles)
                if not is_staff:
                    continue
            activities = [a for a in member.activities if not isinstance(a, discord.CustomActivity)]
            primary_activity = activities[0] if activities else None
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
                'is_online': member.status != discord.Status.offline,
                'status': str(member.status),
                'activity_name': primary_activity.name if primary_activity else None,
                'activity_type': str(primary_activity.type).split('.')[-1] if primary_activity else None,
            }
            is_staff = md['is_owner'] or any(str(r.id) in mod_role_ids for r in member.roles)
            md['is_staff'] = is_staff
            members_data.append(md)
            if is_staff and not member.bot:
                staff_member_ids.append(str(member.id))
            is_online = member.status != discord.Status.offline
            if is_online:
                online_count += 1
                track_online(str(guild.id), member.id)
            else:
                track_offline(str(guild.id), member.id)

        channels_data = []
        for channel in guild.channels:
            if isinstance(channel, discord.CategoryChannel):
                continue
            is_public = True
            default_overwrites = channel.overwrites_for(guild.default_role) if hasattr(channel, 'overwrites_for') else None
            if default_overwrites and default_overwrites.read_messages is False:
                is_public = False
            ch_type = str(channel.type) if hasattr(channel, 'type') else 'text'
            channels_data.append({
                'channel_id': str(channel.id),
                'name': channel.name,
                'topic': channel.topic if hasattr(channel, 'topic') else None,
                'channel_type': ch_type,
                'category': channel.category.name if channel.category else None,
                'position': channel.position,
                'is_public': is_public,
            })

        automod_data = []
        try:
            rules = await guild.fetch_automod_rules()
            for rule in rules:
                trigger_type = str(rule.trigger.type) if hasattr(rule.trigger, 'type') else str(rule.trigger)
                if hasattr(rule.trigger, 'presets') and rule.trigger.presets:
                    trigger_text = ','.join(str(p) for p in rule.trigger.presets)
                elif hasattr(rule.trigger, 'keyword_filter') and rule.trigger.keyword_filter:
                    trigger_text = ','.join(rule.trigger.keyword_filter[:5])
                else:
                    trigger_text = str(rule.trigger)[:200]
                action = rule.actions[0] if rule.actions else None
                action_type = str(action.type) if action else 'unknown'
                alert_channel_id = str(action.channel_id) if action and hasattr(action, 'channel_id') and action.channel_id else None
                automod_data.append({
                    'rule_id': str(rule.id),
                    'name': rule.name,
                    'creator_id': str(rule.creator.id) if rule.creator else None,
                    'creator_name': rule.creator.name if rule.creator else None,
                    'trigger_type': trigger_type,
                    'trigger_text': trigger_text[:500],
                    'action_type': action_type,
                    'alert_channel_id': alert_channel_id,
                    'enabled': rule.enabled,
                    'exempt_roles': ','.join(str(r.id) for r in rule.exempt_roles) if rule.exempt_roles else None,
                    'exempt_channels': ','.join(str(c.id) for c in rule.exempt_channels) if rule.exempt_channels else None,
                })
        except Exception as e:
            print(f'[SkillSync] Could not fetch AutoMod rules for {guild.name}: {e}')

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
            'channels': channels_data,
            'automod_rules': automod_data,
        }

        await api_post('/observer/guild-scan', payload)
        log(f'SCAN COMPLETE: {guild.name} — {len(roles_data)} roles, {guild.member_count} members, {len(staff_member_ids)} staff, {len(automod_data)} automod rules')
        print(f'[SkillSync] Scan complete: {guild.name} — {len(staff_member_ids)} staff out of {guild.member_count} members')
        await build_automod_alert_channels()

    except Exception as e:
        log(f'SCAN FAILED for {guild.name} ({guild.id}): {e}')
        print(f'[SkillSync] ERROR scanning {guild.name}: {e}')
