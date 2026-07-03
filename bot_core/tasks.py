import asyncio
from datetime import datetime, timedelta, timezone

import requests
from discord.ext import tasks

from bot_core import state as bot_state
from bot_core.api_client import api_post
from bot_core.config import (
    API_KEY,
    BAN_WATCH_HOURS,
    HEARTBEAT_GUILD_ID,
    HEARTBEAT_INTERVAL_MINUTES,
    MESSAGE_RETENTION_DAYS,
    PING_WATCH_MINUTES,
    SKILLSYNC_API,
)
from bot_core.logging import log
from bot_core.scanner import scan_guild
from bot_core.state import (  # re-exported for other modules
    flush_join_buffer,
    flush_join_leave_buffer,
    flush_member_presence_buffer,
    flush_mention_buffer,
    flush_message_buffer,
    flush_online_count,
    flush_presence_buffer,
    flush_voice_buffer,
    inc_forecast_counter,
    reset_forecast_counter,
)
from database import Task, Worker, db
from scoring import award_points
from work_engine.connector_jira import is_configured, map_issue_to_task, poll_issues

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
    await flush_join_leave_buffer()
    await flush_online_count()
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
        uptime = (
            now - bot_state.bot_start_time if bot_state.bot_start_time else timedelta()
        )
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes = remainder // 60
        msg_count = "?"
        member_count = "?"
        try:
            resp = await asyncio.to_thread(
                requests.get,
                f"{SKILLSYNC_API}/observer/staff-activity",
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=5,
            )
            if resp.ok:
                data = resp.json()
                msg_count = str(data.get("total_messages", "?"))
                member_count = str(data.get("total_members", "?"))
        except Exception:
            pass
        names = ", ".join(g.name for g in _bot.guilds)
        await channel.send(
            f"🟢 **Bot Alive** | Uptime: `{hours}h {minutes}m` | "
            f"Servers: `{len(_bot.guilds)}` | "
            f"Messages: `{msg_count}` | "
            f"Members: `{member_count}` | "
            f"``{names}``"
        )
        log(f"Heartbeat posted to #{channel.name}")
    except Exception as e:
        log(f"Heartbeat error: {e}")


@tasks.loop(hours=1)
async def check_reversed_actions():
    """
    Every hour: confirm bans that have stood 48+ hours,
    scan anomalies, and trigger weekly ML retrain.
    """
    now = datetime.now(timezone.utc)
    to_confirm = [
        key
        for key, data in bot_state.pending_bans.items()
        if (now - data["timestamp"]).total_seconds() / 3600 > BAN_WATCH_HOURS
    ]

    for key in to_confirm:
        data = bot_state.pending_bans.get(key)
        if not data:
            continue
        user_id_str = str(key[1]) if isinstance(key, tuple) and len(key) > 1 else ""
        guild_id_str = (
            str(key[0])
            if isinstance(key, tuple) and len(key) > 0
            else str(data.get("guild_id", ""))
        )
        print(
            f"[Observer] Ban confirmed valid: {data['user_name']} by {data['banner_name']}"
        )
        try:
            result = await api_post(
                "/observer/confirm",
                {
                    "discord_id": data["banner_id"],
                    "staff_name": data["banner_name"],
                    "action_type": "ban_confirmed",
                    "target": data["user_name"],
                    "target_id": user_id_str,
                    "guild": data["guild_name"],
                    "guild_id": guild_id_str,
                    "note": "Ban stood for 48+ hours — confirmed as valid moderation action",
                    "timestamp": now.isoformat(),
                },
            )
            if result and not result.get("error"):
                bot_state.pending_bans.pop(key, None)
        except Exception as e:
            print(f"[Observer] Ban confirm API error for {data['user_name']}: {e}")

    # Scan anomalies and burnout risks — per-guild for ML, global for rule-based
    print(f"[Observer] Scanning behavioral anomalies...")
    await api_post("/observer/anomalies/scan", {"trigger": "hourly"})
    print(f"[Observer] Scanning burnout risks...")
    await api_post("/observer/burnout-scan", {"trigger": "hourly"})
    print(f"[Observer] ML anomaly scan (per-guild)...")
    try:
        resp = await asyncio.to_thread(
            requests.get,
            f"{SKILLSYNC_API}/observer/guilds",
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=5,
        )
        if resp.ok:
            data = resp.json()
            guilds = data if isinstance(data, list) else data.get("value", [])
            for g in guilds:
                gid = g["guild_id"]
                try:
                    await api_post("/observer/ml/anomalies/scan", {"guild_id": gid})
                except Exception as e:
                    print(f"[Observer] Anomaly scan error for guild {gid}: {e}")
        else:
            await api_post("/observer/ml/anomalies/scan", {"trigger": "hourly"})
    except Exception as e:
        print(f"[Observer] Failed to fetch guild list for per-guild scan: {e}")
        await api_post("/observer/ml/anomalies/scan", {"trigger": "hourly"})
    print(f"[Observer] ML burnout scan...")
    await api_post("/observer/ml/burnout-scan", {"trigger": "hourly"})

    # ML forecast: run prediction every hour (direct hourly scheduling)
    print(f"[Observer] Running ML forecast predictions...")
    try:
        resp = await asyncio.to_thread(
            requests.get,
            f"{SKILLSYNC_API}/observer/guilds",
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=5,
        )
        if resp.ok:
            data = resp.json()
            guilds = data if isinstance(data, list) else data.get("value", [])
            for g in guilds:
                gid = g["guild_id"] if isinstance(g, dict) else g
                try:
                    await asyncio.to_thread(
                        requests.get,
                        f"{SKILLSYNC_API}/observer/ml/forecast/{gid}",
                        headers={"Authorization": f"Bearer {API_KEY}"},
                        timeout=10,
                    )
                except Exception:
                    pass
    except Exception as e:
        print(f"[Observer] Forecast prediction error: {e}")

    # ML forecast: resolve pending outcomes every heartbeat (cheap query)
    print(f"[Observer] Resolving forecast outcomes...")
    await api_post("/observer/ml/resolve", {"days_back": 7})

    # Correction-triggered retrain check
    try:
        resp = await asyncio.to_thread(
            requests.get,
            f"{SKILLSYNC_API}/observer/ml/pending-retrain",
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=5,
        )
        if resp.ok and resp.json().get("pending"):
            print(f"[Observer] Correction-triggered retrain pending...")
            await api_post("/observer/ml/retrain", {"trigger": "correction_feedback"})
    except Exception as e:
        print(f"[Observer] Retrain check error: {e}")

    # Auto-retrain when anomaly precision drops below threshold
    try:
        resp = await asyncio.to_thread(
            requests.get,
            f"{SKILLSYNC_API}/observer/ml/anomalies/precision-recall",
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=5,
        )
        if resp.ok:
            data = resp.json()
            if (
                data.get("total_with_feedback", 0) >= 3
                and data.get("precision") is not None
                and data["precision"] < 0.5
            ):
                print(
                    f"[Observer] Anomaly precision {data['precision_pct']}% below 50%, triggering retrain..."
                )
                await api_post("/observer/ml/retrain", {"trigger": "low_precision"})
    except Exception as e:
        print(f"[Observer] Precision check error: {e}")

    # Weekly ML model retrain (168 hours = 7 days)
    val = bot_state.inc_ml_retrain_counter()
    if val >= 168:
        bot_state.set_ml_retrain_counter(0)
        print(f"[Observer] Weekly ML retrain triggered...")
        await api_post("/observer/ml/retrain", {"trigger": "weekly"})


@tasks.loop(hours=6)
async def message_cleanup_loop():
    """Delete messages older than MESSAGE_RETENTION_DAYS via API."""
    try:
        resp = await api_post(
            "/observer/cleanup", {"retention_days": MESSAGE_RETENTION_DAYS}
        )
        if resp and resp.get("deleted"):
            print(
                f"[Cleanup] Deleted {resp['deleted']} old messages, {resp.get('deleted_mentions', 0)} old mentions"
            )
    except Exception as e:
        print(f"[Cleanup] Error: {e}")


@tasks.loop(minutes=5)
async def check_ping_joins():
    """Every 5 min, expire @everyone pings after 20 min window."""
    now = datetime.now(timezone.utc)
    expired = [
        gid
        for gid, data in bot_state.active_pings.items()
        if (now - data["timestamp"]).total_seconds() / 60 > PING_WATCH_MINUTES
    ]
    for gid in expired:
        data = bot_state.active_pings.pop(gid)
        if data["join_count"] > 0:
            print(
                f"[PingWatch] {data['mod_name']} pinged @everyone, {data['join_count']} joined within {PING_WATCH_MINUTES}min"
            )
            await api_post(
                "/observer/ping-join",
                {
                    "moderator_id": data["mod_id"],
                    "moderator_name": data["mod_name"],
                    "guild_id": str(gid),
                    "guild_name": data["guild_name"],
                    "channel": data["channel"],
                    "new_members": data["join_count"],
                    "joiners": ",".join(data["joiners"][:50]),
                    "timestamp": data["timestamp"].isoformat(),
                },
            )


@tasks.loop(hours=1)
async def jira_poll_loop():
    """Poll Jira for updated issues and sync to internal tasks."""
    if not is_configured():
        return
    print(f"[WorkEngine] Polling Jira...")
    issues = poll_issues(days_back=7)
    if not issues:
        return
    synced = 0
    for issue in issues:
        assignee_email = issue.get("assignee_email", "")
        assignee_account = issue.get("assignee_account_id", "")
        if not assignee_email and not assignee_account:
            continue

        # Try to resolve through WorkerIdentity.jira_account_id -> WorkerIdentity.worker_id -> Worker
        worker = None
        if assignee_account:
            identity = WorkerIdentity.query.filter_by(
                jira_account_id=assignee_account
            ).first()
            if identity and identity.worker_id:
                worker = Worker.query.get(identity.worker_id)

        # If not found, try email fallback through WorkerIdentity.email -> Worker.email
        if not worker and assignee_email:
            identity = WorkerIdentity.query.filter_by(email=assignee_email).first()
            if identity and identity.worker_id:
                worker = Worker.query.get(identity.worker_id)

        # If still not found, try Worker.email directly (legacy fallback)
        if not worker and assignee_email:
            worker = Worker.query.filter_by(email=assignee_email).first()

        # If still not found, try Worker.discord_id (legacy fallback)
        if not worker and assignee_account:
            worker = Worker.query.filter_by(discord_id=assignee_account).first()

        if not worker:
            print(
                f"[WorkEngine] Could not resolve Jira assignee: email={assignee_email}, account={assignee_account}"
            )
            continue

        task_data = map_issue_to_task(issue, worker.id)
        existing = Task.query.filter_by(external_id=issue["key"], source="jira").first()
        if existing:
            old_status = existing.status
            existing.title = task_data["title"]
            existing.description = task_data.get("description", "")
            existing.priority = task_data.get("priority", "medium")
            existing.status = task_data.get("status", "pending")
            if task_data.get("due_at"):
                existing.due_at = (
                    datetime.fromisoformat(task_data["due_at"])
                    if isinstance(task_data["due_at"], str)
                    else task_data["due_at"]
                )
            if old_status != existing.status and existing.status in (
                "completed",
                "missed",
            ):
                if existing.status == "completed":
                    due = existing.due_at
                    now = datetime.utcnow()
                    key = (
                        "task_completed_on_time"
                        if not due or now <= due
                        else "task_completed_late"
                    )
                else:
                    key = "task_missed"
                note = f"Task {key.replace('task_', '').replace('_', ' ')}: {existing.title}"
                result = award_points(worker.id, key, source="jira", note=note)
                existing.points_awarded = result.get("change", 0)
        else:
            task = Task(
                worker_id=worker.id,
                title=task_data["title"],
                description=task_data.get("description", ""),
                status=task_data.get("status", "pending"),
                source="jira",
                external_id=issue["key"],
                external_url=task_data.get("external_url", ""),
                priority=task_data.get("priority", "medium"),
            )
            if task_data.get("due_at"):
                task.due_at = (
                    datetime.fromisoformat(task_data["due_at"])
                    if isinstance(task_data["due_at"], str)
                    else task_data["due_at"]
                )
            db.session.add(task)
        synced += 1
    db.session.commit()
    print(f"[WorkEngine] Synced {synced} issues from Jira")


@tasks.loop(hours=1)
async def jira_per_org_poll_loop():
    """Poll Jira for every org that has credentials configured.
    Auto-creates/updates tasks and awards points per org credentials.
    Runs every hour."""
    from app import app

    with app.app_context():
        from database import Organisation
        from work_engine.connector_jira import poll_and_sync_for_org

        orgs = Organisation.query.filter(
            Organisation.jira_url.isnot(None),
            Organisation.jira_email.isnot(None),
            Organisation.jira_api_token.isnot(None),
            Organisation.jira_project.isnot(None),
            Organisation.is_active.is_(True),
        ).all()

        if not orgs:
            return

        print(f"[WorkEngine] Per-org Jira poll: {len(orgs)} org(s) configured")
        for org in orgs:
            try:
                result = poll_and_sync_for_org(org)
                if result.get("synced", 0) > 0:
                    print(
                        f"[WorkEngine] Org {org.slug}: synced {result['synced']} tasks"
                    )
                if result.get("errors", 0) > 0:
                    print(f"[WorkEngine] Org {org.slug}: {result['errors']} errors")
            except Exception as e:
                print(f"[WorkEngine] Org {org.slug}: poll error: {e}")


@tasks.loop(hours=6)
async def rescan_guilds_loop():
    """Re-scan all guilds every 6 hours to refresh online counts, members, and staff lists."""
    if not _bot:
        return
    print(
        f"[Rescan] Starting periodic guild re-scan for {len(_bot.guilds)} guild(s)..."
    )
    for guild in _bot.guilds:
        try:
            await scan_guild(guild)
            print(f"[Rescan] Re-scanned {guild.name} ({guild.id})")
        except Exception as e:
            print(f"[Rescan] Error scanning {guild.name}: {e}")
    print(f"[Rescan] Periodic guild re-scan complete")


@tasks.loop(hours=6)
async def check_overdue_tasks():
    """Check for tasks past their due date and notify Slack.
    Only notifies on exact day boundaries (1, 3, 7 days overdue) to avoid spam."""
    from app import app

    with app.app_context():
        try:
            from services.slack import notify_task_overdue

            now = datetime.utcnow()
            overdue = Task.query.filter(
                Task.status == "pending",
                Task.due_at != None,
                Task.due_at < now,
            ).all()

            for task in overdue:
                days_overdue = (now - task.due_at).days
                if days_overdue not in (1, 3, 7):
                    continue
                worker = Worker.query.get(task.worker_id)
                if not worker:
                    continue
                notify_task_overdue(
                    worker_name=worker.name,
                    task_title=task.title,
                    days_overdue=days_overdue,
                    worker_id=task.worker_id,
                )
        except Exception as e:
            print(f"[check_overdue_tasks] error: {e}")


@tasks.loop(hours=168)
async def weekly_health_digest():
    """Send weekly team health summary to Slack for all orgs.
    Runs every 168 hours (1 week)."""
    from app import app

    with app.app_context():
        try:
            from database import (
                BehavioralAnomaly,
                BurnoutRisk,
                Organisation,
                ScoreLog,
                WorkerIdentity,
            )
            from database import Task as TaskModel
            from database import Worker as WorkerModel
            from services.slack import notify_team_health_summary

            cutoff_30 = datetime.utcnow() - timedelta(days=30)
            cutoff_7 = datetime.utcnow() - timedelta(days=7)

            orgs = Organisation.query.filter_by(is_active=True).all()
            for org in orgs:
                identities = WorkerIdentity.query.filter_by(
                    org_id=org.id, is_active=True
                ).all()
                green = yellow = red = 0

                for ident in identities:
                    if not ident.worker_id:
                        continue
                    logs = ScoreLog.query.filter(
                        ScoreLog.worker_id == ident.worker_id,
                        ScoreLog.created_at >= cutoff_30,
                    ).all()
                    score_30d = sum(s.change for s in logs)
                    score_7d = sum(s.change for s in logs if s.created_at >= cutoff_7)
                    missed = TaskModel.query.filter_by(
                        worker_id=ident.worker_id, status="missed"
                    ).count()
                    burnout_score = 0
                    if ident.consent_community_prior and ident.discord_id:
                        br = (
                            BurnoutRisk.query.filter_by(discord_id=ident.discord_id)
                            .order_by(BurnoutRisk.detected_at.desc())
                            .first()
                        )
                        burnout_score = br.score if br else 0

                    if missed > 2 or score_30d < -20 or burnout_score > 60:
                        red += 1
                    elif missed > 0 or score_30d < 0 or burnout_score > 30:
                        yellow += 1
                    else:
                        green += 1

                notify_team_health_summary(
                    org_name=org.name,
                    green=green,
                    yellow=yellow,
                    red=red,
                )
        except Exception as e:
            print(f"[weekly_health_digest] error: {e}")
