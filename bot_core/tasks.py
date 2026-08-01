import asyncio
import os
import threading
import time as _time
from datetime import datetime, timedelta, timezone

from discord.ext import tasks

from bot_core import state as bot_state
from bot_core.api_client import api_get, api_post
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
)
from database import Task, Worker, WorkerIdentity, db
from scoring import award_points
from work_engine.connector_jira import is_configured, map_issue_to_task, poll_issues

_bot = None
_last_heartbeat = -9999999999  # fire immediately on first check
_reversed_actions_first_run = True

# ponytail: _http_hit counter tracks how many api_post/api_get calls are in-flight.
# Helps diagnose thread pool saturation in FLUSH SLOW output.
_http_hit = 0
_http_hit_lock = threading.Lock()


def _inc_http():
    global _http_hit
    with _http_hit_lock:
        _http_hit += 1


def _dec_http():
    global _http_hit
    with _http_hit_lock:
        _http_hit -= 1


def set_bot(bot):
    global _bot
    _bot = bot


# ── Heartbeat daemon thread ──
# ponytail: writes .bot_heartbeat every 10s from a thread that's
# completely independent of the asyncio event loop.  If the event loop
# freezes, the heartbeat file stops updating and the watchdog can still
# detect it.  Also monitors event-loop health: if heartbeat wasn't
# refreshed for 120s it logs a warning; at 180s it prints a traceback.
_heartbeat_thread = None
_loop_healthy_ts = _time.monotonic()
# ponytail: grace period — don't warn about frozen loop until 120s after
# the thread starts, because flush_all_buffers (which calls
# _mark_loop_healthy) hasn't run yet at import time.
_heartbeat_start_ts = _time.monotonic()


def _mark_loop_healthy():
    global _loop_healthy_ts
    _loop_healthy_ts = _time.monotonic()


def _heartbeat_daemon(heartbeat_path, interval=10):
    """Runs in a daemon thread, writes heartbeat file on a fixed interval.

    ponytail: only writes .bot_heartbeat when the event loop is healthy.
    When frozen, the file goes stale → watchdog detects and kills the process.
    Without this, the daemon thread keeps both log mtime and heartbeat file
    fresh, and the watchdog's min(log_age, hb_age) never triggers.
    """
    _logged_thread_dump = False
    while True:
        # Check event loop health (skip during grace period)
        elapsed_since_start = _time.monotonic() - _heartbeat_start_ts
        if elapsed_since_start > 120:
            age = _time.monotonic() - _loop_healthy_ts
            if age > 180:
                if not _logged_thread_dump:
                    _logged_thread_dump = True
                    _dump_thread_state()
                log(f"HEARTBEAT THREAD: event loop frozen for {int(age)}s — watchdog will kill")
                # ponytail: don't write heartbeat file — let watchdog see it stale
                _time.sleep(interval)
                continue
            elif age > 120:
                log(f"HEARTBEAT THREAD: event loop unresponsive for {int(age)}s")
            else:
                _logged_thread_dump = False

        try:
            with open(heartbeat_path, 'w') as f:
                f.write(str(_time.time()))
        except Exception:
            pass

        _time.sleep(interval)


def _dump_thread_state():
    """Dump thread and executor state when event loop appears frozen."""
    import threading
    import sys
    try:
        import traceback
        threads = threading.enumerate()
        log(f"THREAD DUMP: {len(threads)} threads alive:")
        for t in threads:
            if t is threading.main_thread() or t.name.startswith("heartbeat"):
                continue
            # ponytail: capture ALL non-main threads — ThreadPoolExecutor
            # threads are non-daemon, so we can't filter on t.daemon
            log(f"  Thread: {t.name} (daemon={t.daemon}, alive={t.is_alive()})")
            if t.is_alive():
                for frame_id, frame in sys._current_frames().items():
                    if frame_id == t.ident:
                        lines = ''.join(traceback.format_stack(frame))
                        # Log last 10 lines of the stack to avoid flooding
                        stack_lines = lines.strip().split('\n')
                        log(f"    Stack ({len(stack_lines)} frames):")
                        for line in stack_lines[-10:]:
                            log(f"      {line}")
                        break
        # Check default executor state
        try:
            loop = asyncio.get_event_loop()
            executor = loop._default_executor
            if executor:
                log(f"  Default executor: {executor._max_workers} max workers, "
                    f"tasks queued: {executor._work_queue.qsize() if hasattr(executor, '_work_queue') else '?'}")
        except Exception:
            pass
    except Exception as e:
        log(f"  Thread dump failed: {e}")


def _ensure_heartbeat_thread():
    global _heartbeat_thread
    if _heartbeat_thread is not None:
        return
    hb_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.bot_heartbeat')
    _heartbeat_thread = threading.Thread(target=_heartbeat_daemon, args=(hb_path,), daemon=True, name="heartbeat")
    _heartbeat_thread.start()
    log("Heartbeat daemon thread started (10s interval)")


# Start heartbeat thread on import
_ensure_heartbeat_thread()


@tasks.loop(seconds=30)
async def flush_all_buffers():
    """Flush all buffered data every 30 seconds."""
    try:
        _mark_loop_healthy()
        _t0 = _time.monotonic()

        # ponytail: heartbeat file is now written by daemon thread — skip here
        # to avoid blocking the event loop on file I/O.

        _hb = _time.monotonic()
        await flush_message_buffer()
        _m1 = _time.monotonic()
        await flush_presence_buffer()
        _m2 = _time.monotonic()
        await flush_member_presence_buffer()
        _m3 = _time.monotonic()
        await flush_mention_buffer()
        _m4 = _time.monotonic()
        await flush_voice_buffer()
        _m5 = _time.monotonic()
        await flush_join_buffer()
        _m6 = _time.monotonic()
        await flush_join_leave_buffer()
        _m7 = _time.monotonic()
        await flush_online_count()
        _m8 = _time.monotonic()
        await _maybe_heartbeat()
        _end = _time.monotonic()

        total = _end - _t0
        hits = _http_hit
        if total > 5:
            log(f"FLUSH SLOW {total:.1f}s http_in_flight={hits} | msg={_m1-_hb:.2f} pres={_m2-_m1:.2f} member={_m3-_m2:.2f} mention={_m4-_m3:.2f} voice={_m5-_m4:.2f} join={_m6-_m5:.2f} joinleave={_m7-_m6:.2f} online={_m8-_m7:.2f} heartbeat={_end-_m8:.2f}")
        else:
            log(f"FLUSHED online counts http_in_flight={hits}")
    except Exception as e:
        log(f"FLUSH EXCEPTION (task survived): {type(e).__name__}: {e}")


@flush_all_buffers.before_loop
async def _flush_before_loop():
    await asyncio.sleep(1)


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
            data = await asyncio.wait_for(
                api_get(
                    f"{SKILLSYNC_API}/observer/staff-activity",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    timeout=5,
                ),
                timeout=10,
            )
            if data:
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

    ponytail: every api_post is wrapped in wait_for(15s) so a slow Flask
    endpoint can't block this task indefinitely and starve the event loop.
    """
    try:
        await _check_reversed_actions_body()
    except Exception as e:
        log(f"check_reversed_actions EXCEPTION (task survived): {type(e).__name__}: {e}")


async def _check_reversed_actions_body():
    global _reversed_actions_first_run
    if _reversed_actions_first_run:
        _reversed_actions_first_run = False
        print("[Observer] Skipping first run — Flask still busy with startup scans")
        return
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
            result = await asyncio.wait_for(
                api_post(
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
                ),
                timeout=15,
            )
            if result and not result.get("error"):
                bot_state.pending_bans.pop(key, None)
        except asyncio.TimeoutError:
            print(f"[Observer] Ban confirm timed out for {data['user_name']}")
        except Exception as e:
            print(f"[Observer] Ban confirm API error for {data['user_name']}: {e}")

    # ponytail: each heavy endpoint gets a 15s timeout so one slow Flask
    # response can't cascade into event-loop starvation.
    print(f"[Observer] Scanning behavioral anomalies...")
    try:
        await asyncio.wait_for(api_post("/observer/anomalies/scan", {"trigger": "hourly"}), timeout=15)
    except asyncio.TimeoutError:
        print("[Observer] Anomaly scan timed out")
    print(f"[Observer] Scanning burnout risks...")
    try:
        await asyncio.wait_for(api_post("/observer/burnout-scan", {"trigger": "hourly"}), timeout=15)
    except asyncio.TimeoutError:
        print("[Observer] Burnout scan timed out")
    print(f"[Observer] ML anomaly scan (per-guild)...")
    try:
        resp = await asyncio.wait_for(
            api_get(
                f"{SKILLSYNC_API}/observer/guilds",
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=5,
            ),
            timeout=10,
        )
        if resp:
            guilds = resp if isinstance(resp, list) else resp.get("value", [])
            for g in guilds:
                gid = g["guild_id"]
                try:
                    await asyncio.wait_for(api_post("/observer/ml/anomalies/scan", {"guild_id": gid}), timeout=15)
                except asyncio.TimeoutError:
                    print(f"[Observer] Anomaly scan timed out for guild {gid}")
                except Exception as e:
                    print(f"[Observer] Anomaly scan error for guild {gid}: {e}")
        else:
            try:
                await asyncio.wait_for(api_post("/observer/ml/anomalies/scan", {"trigger": "hourly"}), timeout=15)
            except asyncio.TimeoutError:
                pass
    except asyncio.TimeoutError:
        print("[Observer] Guild list fetch timed out")
    except Exception as e:
        print(f"[Observer] Failed to fetch guild list for per-guild scan: {e}")
        try:
            await asyncio.wait_for(api_post("/observer/ml/anomalies/scan", {"trigger": "hourly"}), timeout=15)
        except asyncio.TimeoutError:
            pass
    print(f"[Observer] ML burnout scan...")
    try:
        await asyncio.wait_for(api_post("/observer/ml/burnout-scan", {"trigger": "hourly"}), timeout=15)
    except asyncio.TimeoutError:
        print("[Observer] ML burnout scan timed out")

    # ML forecast: resolve pending outcomes every heartbeat (cheap query)
    print(f"[Observer] Resolving forecast outcomes...")
    try:
        await asyncio.wait_for(api_post("/observer/ml/resolve", {"days_back": 7}), timeout=15)
    except asyncio.TimeoutError:
        print("[Observer] Forecast resolve timed out")

    # Correction-triggered retrain check
    try:
        resp = await asyncio.wait_for(
            api_get(
                f"{SKILLSYNC_API}/observer/ml/pending-retrain",
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=5,
            ),
            timeout=10,
        )
        if resp and resp.get("pending"):
            print(f"[Observer] Correction-triggered retrain pending...")
            try:
                await asyncio.wait_for(api_post("/observer/ml/retrain", {"trigger": "correction_feedback"}), timeout=30)
            except asyncio.TimeoutError:
                print("[Observer] Correction retrain timed out")
    except asyncio.TimeoutError:
        print("[Observer] Retrain check timed out")
    except Exception as e:
        print(f"[Observer] Retrain check error: {e}")

    # Auto-retrain when anomaly precision drops below threshold
    try:
        resp = await asyncio.wait_for(
            api_get(
                f"{SKILLSYNC_API}/observer/ml/anomalies/precision-recall",
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=5,
            ),
            timeout=10,
        )
        if resp:
            if (
                resp.get("total_with_feedback", 0) >= 3
                and resp.get("precision") is not None
                and resp["precision"] < 0.5
            ):
                print(
                    f"[Observer] Anomaly precision {resp.get('precision_pct')}% below 50%, triggering retrain..."
                )
                try:
                    await asyncio.wait_for(api_post("/observer/ml/retrain", {"trigger": "low_precision"}), timeout=30)
                except asyncio.TimeoutError:
                    print("[Observer] Low-precision retrain timed out")
    except asyncio.TimeoutError:
        print("[Observer] Precision check timed out")
    except Exception as e:
        print(f"[Observer] Precision check error: {e}")

    # Weekly ML model retrain (168 hours = 7 days)
    val = bot_state.inc_ml_retrain_counter()
    if val >= 168:
        bot_state.set_ml_retrain_counter(0)
        print(f"[Observer] Weekly ML retrain triggered...")
        try:
            await asyncio.wait_for(api_post("/observer/ml/retrain", {"trigger": "weekly"}), timeout=30)
        except asyncio.TimeoutError:
            print("[Observer] Weekly retrain timed out")


@tasks.loop(hours=6)
async def message_cleanup_loop():
    """Retrain ML on all data, then delete old messages."""
    try:
        await api_post("/observer/ml/retrain", {"trigger": "pre_cleanup"})
    except Exception as e:
        print(f"[Cleanup] Retrain error (non-fatal): {e}")
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
    """Poll Jira for updated issues and sync to internal tasks.

    Entire body runs in asyncio.to_thread to avoid blocking the event loop
    with synchronous DB queries and HTTP requests.
    """
    def _do_jira_poll():
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

            worker = None
            if assignee_account:
                identity = WorkerIdentity.query.filter_by(
                    jira_account_id=assignee_account
                ).first()
                if identity and identity.worker_id:
                    worker = Worker.query.get(identity.worker_id)

            if not worker and assignee_email:
                identity = WorkerIdentity.query.filter_by(email=assignee_email).first()
                if identity and identity.worker_id:
                    worker = Worker.query.get(identity.worker_id)

            if not worker and assignee_email:
                worker = Worker.query.filter_by(email=assignee_email).first()

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

    # ponytail: do NOT wrap in asyncio.wait_for — it cancels the future but
    # the thread keeps running, permanently consuming a pool slot.  The Jira
    # connector has its own HTTP timeout (15s) so the thread will finish.
    try:
        await asyncio.to_thread(_do_jira_poll)
    except Exception as e:
        print(f"[WorkEngine] Jira poll error: {e}")


@tasks.loop(hours=1)
async def jira_per_org_poll_loop():
    """Poll Jira for every org that has credentials configured.
    Auto-creates/updates tasks and awards points per org credentials.
    Runs every hour.
    """
    def _poll_all_orgs():
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

    # ponytail: do NOT wrap in asyncio.wait_for — same thread-leak fix as above
    try:
        await asyncio.to_thread(_poll_all_orgs)
    except Exception as e:
        print(f"[WorkEngine] Per-org Jira poll error: {e}")


def _compute_lead_bucket(prediction_time):
    """Compute the lead bucket hours for the next target day.

    Target day is the next calendar day (midnight to midnight).
    Lead bucket = hours from prediction_time to target_end (next midnight).
    Rounds to one of: 24, 18, 12, 6.
    """
    midnight = prediction_time.replace(hour=0, minute=0, second=0, microsecond=0)
    target_start = midnight + timedelta(days=1)
    target_end = target_start + timedelta(hours=24)
    hours_until_end = (target_end - prediction_time).total_seconds() / 3600.0
    # Round to nearest standard bucket
    buckets = [24, 18, 12, 6]
    closest = min(buckets, key=lambda b: abs(hours_until_end - b))
    return closest


@tasks.loop(hours=6)
async def forecast_logging_loop():
    """Every 6 hours: log forecast predictions with appropriate lead buckets.

    Lead buckets: 24h, 18h, 12h, 6h before target day end.
    The dedup check in forecast.py ensures each (guild, target_day, lead_bucket)
    combination is only logged once.
    """
    print("[Forecast] Running scheduled forecast predictions (6h cycle)...")
    try:
        resp = await asyncio.wait_for(
            api_get(
                f"{SKILLSYNC_API}/observer/guilds",
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=5,
            ),
            timeout=10,
        )
        if resp:
            guilds = resp if isinstance(resp, list) else resp.get("value", [])
            now = datetime.utcnow()
            lead_bucket = _compute_lead_bucket(now)
            print(f"[Forecast] Lead bucket: {lead_bucket}h for {len(guilds)} guild(s)")
            for g in guilds:
                gid = g["guild_id"] if isinstance(g, dict) else g
                try:
                    await asyncio.wait_for(
                        api_get(
                            f"{SKILLSYNC_API}/observer/ml/forecast/{gid}?log=true&lead_bucket={lead_bucket}",
                            headers={"Authorization": f"Bearer {API_KEY}"},
                            timeout=10,
                        ),
                        timeout=15,
                    )
                except (asyncio.TimeoutError, Exception):
                    pass
        print("[Forecast] Scheduled forecast logging complete.")
    except asyncio.TimeoutError:
        print("[Forecast] Guild list fetch timed out")
    except Exception as e:
        print(f"[Forecast] Forecast prediction error: {e}")


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
    Only notifies on exact day boundaries (1, 3, 7 days overdue) to avoid spam.
    
    Entire body runs in asyncio.to_thread to avoid blocking the event loop
    with synchronous DB queries and Slack HTTP calls.
    """
    def _check_overdue():
        from app import app
        with app.app_context():
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

    await asyncio.to_thread(_check_overdue)


@tasks.loop(hours=168)
async def weekly_health_digest():
    """Send weekly team health summary to Slack for all orgs.
    Runs every 168 hours (1 week).
    
    Entire body runs in asyncio.to_thread to avoid blocking the event loop
    with hundreds of synchronous DB queries.
    """
    def _weekly_digest():
        from app import app
        with app.app_context():
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

    await asyncio.to_thread(_weekly_digest)
