import asyncio
import os
from datetime import datetime, timezone

import discord
import requests

from bot_core.config import (
    API_KEY,
    MESSAGE_RETENTION_DAYS,
    PING_WATCH_MINUTES,
    SKILLSYNC_API,
)
from bot_core.heartbeat import setup_heartbeat
from bot_core.logging import log
from bot_core.scanner import build_automod_alert_channels, scan_guild
from bot_core.state import (
    content_trust,
    pending_bans,
    pending_timeouts,
    prefix_cache,
    set_bot_start_time,
)
from bot_core.tasks import (
    check_overdue_tasks,
    check_ping_joins,
    check_reversed_actions,
    flush_all_buffers,
    forecast_logging_loop,
    jira_per_org_poll_loop,
    jira_poll_loop,
    message_cleanup_loop,
    rescan_guilds_loop,
    set_bot,
    weekly_health_digest,
)


async def handle_ready(bot):
    log(f"Bot online as {bot.user}")
    log(f"Watching {len(bot.guilds)} server(s)")
    for guild in bot.guilds:
        log(f"  - Guild: {guild.name} (ID: {guild.id})")
        me = guild.me
        if me:
            perms = dict(me.guild_permissions)
            log(
                f"    Perms: ban_members={perms['ban_members']}, kick_members={perms['kick_members']}, view_audit_log={perms['view_audit_log']}, manage_messages={perms['manage_messages']}"
            )
            if not perms.get("view_audit_log"):
                log(
                    f"    \u26a0\ufe0f  Bot LACKS view_audit_log permission in {guild.name}! Ban/kick/timeout detection will NOT work."
                )
                print(
                    f"[SkillSync] WARNING: Bot lacks view_audit_log permission in {guild.name}"
                )
    print(f"[SkillSync] Bot online as {bot.user}")
    print(f"[SkillSync] Watching {len(bot.guilds)} server(s)")
    print(f"[SkillSync] Message retention: {MESSAGE_RETENTION_DAYS} days")
    print(f"[SkillSync] Ping watch window: {PING_WATCH_MINUTES} min")

    # Start background tasks
    if not check_reversed_actions.is_running():
        check_reversed_actions.start()
    if not flush_all_buffers.is_running():
        flush_all_buffers.start()
    if not message_cleanup_loop.is_running():
        message_cleanup_loop.start()
    if not check_ping_joins.is_running():
        check_ping_joins.start()
    if not jira_poll_loop.is_running():
        jira_poll_loop.start()
    if not jira_per_org_poll_loop.is_running():
        jira_per_org_poll_loop.start()
    if not rescan_guilds_loop.is_running():
        rescan_guilds_loop.start()
    if not forecast_logging_loop.is_running():
        forecast_logging_loop.start()
    if not check_overdue_tasks.is_running():
        check_overdue_tasks.start()
    if not weekly_health_digest.is_running():
        weekly_health_digest.start()

    # Fetch prefixes + content trust from API
    try:
        resp = await asyncio.to_thread(
            requests.get,
            f"{SKILLSYNC_API}/observer/guilds",
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=5,
        )
        if resp.ok:
            for g in resp.json():
                prefix_cache[g["guild_id"]] = g.get("prefixes", ["!ss "])
                content_trust[g["guild_id"]] = g.get("store_content", False)
    except Exception as e:
        print(f"[SkillSync] Could not fetch prefixes: {e}")

    # Setup heartbeat early so loops can use it
    set_bot_start_time(discord.utils.utcnow())
    set_bot(bot)
    await setup_heartbeat(bot)

    # Set bot presence with prefix info
    if bot.guilds:
        first_prefixes = prefix_cache.get(str(bot.guilds[0].id), ["!ss "])
        try:
            await bot.change_presence(
                activity=discord.Game(
                    name=f"{first_prefixes[0].strip()} | Watches over you in your sleep"
                )
            )
        except Exception as e:
            print(f"[SkillSync] Could not set presence: {e}")

    # Scan all existing guilds on startup
    for guild in bot.guilds:
        await scan_guild(guild)

    # Load AutoMod alert channels after scan
    build_automod_alert_channels()

    # Reload pending state from API for restart resilience
    try:
        resp = await asyncio.to_thread(
            requests.get,
            f"{SKILLSYNC_API}/observer/pending-state",
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=5,
        )
        if resp.ok:
            state_data = resp.json()

            def ensure_utc(dt):
                if dt is None:
                    return datetime.now(timezone.utc)
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)

            for b in state_data.get("pending_bans", []):
                key = (
                    (int(b["guild_id"]), int(b["user_id"]))
                    if b["guild_id"] and b["user_id"]
                    else None
                )
                if key:
                    pending_bans[key] = {
                        "banner_id": b["banner_id"],
                        "banner_name": b["banner_name"],
                        "user_name": b["user_name"],
                        "guild_id": int(b["guild_id"]),
                        "guild_name": "",
                        "reason": b.get("reason"),
                        "timestamp": ensure_utc(datetime.fromisoformat(b["timestamp"]))
                        if b.get("timestamp")
                        else datetime.now(timezone.utc),
                    }

            for t in state_data.get("pending_timeouts", []):
                key = (
                    (int(t["guild_id"]), int(t["user_id"]))
                    if t["guild_id"] and t["user_id"]
                    else None
                )
                if key:
                    pending_timeouts[key] = {
                        "mod_id": t["mod_id"],
                        "mod_name": t["mod_name"],
                        "until": ensure_utc(datetime.fromisoformat(t["until"]))
                        if t.get("until")
                        else None,
                        "timestamp": ensure_utc(datetime.fromisoformat(t["timestamp"]))
                        if t.get("timestamp")
                        else datetime.now(timezone.utc),
                    }

            if state_data.get("pending_bans") or state_data.get("pending_timeouts"):
                log(
                    f"Restored {len(state_data['pending_bans'])} pending bans, {len(state_data['pending_timeouts'])} pending timeouts from DB"
                )
                print(
                    f"[Restore] Loaded {len(state_data['pending_bans'])} bans, {len(state_data['pending_timeouts'])} timeouts"
                )
    except Exception as e:
        print(f"[Restore] Could not fetch pending state: {e}")


async def handle_guild_join(bot, guild):
    """Fires when the bot joins a new server. Full scan immediately.
    Posts a welcome embed to the system channel or first available text channel.
    """
    log(f"JOINED new guild: {guild.name} (ID: {guild.id})")
    print(f"[SkillSync] Joined new guild: {guild.name}")
    prefix_cache[str(guild.id)] = ["!ss "]
    await scan_guild(guild)

    # Post welcome embed
    client_id = os.getenv("DISCORD_CLIENT_ID", "1513743115364597790")
    embed = discord.Embed(
        title="SkillSync is here!",
        description=(
            "Thank you for adding SkillSync! I'm a workforce intelligence bot that helps "
            "you understand your community through activity tracking, moderation analytics, "
            "and ML-powered anomaly detection.\n\n"
            "**What I track:**\n"
            "\u2022 Moderation actions (bans, kicks, timeouts, warns)\n"
            "\u2022 Staff activity and scoring\n"
            "\u2022 Message volume and community engagement\n"
            "\u2022 Voice activity sessions\n"
            "\u2022 ML anomaly and burnout detection\n\n"
            "No setup required \u2014 I am already collecting data."
        ),
        color=discord.Color.from_str("#155e75"),
    )
    permissions = 1099780156550
    invite_link = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&permissions={permissions}&scope=bot%20applications.commands"
    embed.add_field(
        name="Add to another server",
        value=f"[Click here]({invite_link})",
        inline=False,
    )
    embed.add_field(
        name="View your dashboard",
        value=(
            "To view your server's dashboard, visit the SkillSync web app and log in "
            "with Discord. You need **Manage Server** or **Administrator** permission "
            "to access the dashboard for this server."
        ),
        inline=False,
    )
    embed.set_footer(text="SkillSync — Watches over you in your sleep")

    # Try system channel first, then first text channel with send permissions
    target_channel = guild.system_channel
    if target_channel is None:
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if perms.send_messages and perms.read_messages and perms.embed_links:
                target_channel = channel
                break

    if target_channel:
        try:
            await target_channel.send(embed=embed)
            log(f"Welcome message posted in #{target_channel.name} in {guild.name}")
        except Exception as e:
            log(f"Could not post welcome embed in {guild.name}: {e}")
    else:
        log(f"No suitable channel found for welcome embed in {guild.name}")
