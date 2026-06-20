# SkillSync — Workforce Intelligence Platform

SkillSync is a dual-engine workforce intelligence platform that tracks workers, tasks, behavioral patterns, and moderation activity across Discord communities and enterprise task systems. It scores individuals in real time based on contributions, reliability, and problem-solving ability, with a feedback loop where admin corrections improve the scoring model over time.

**Two Engines:**
1. **Community Engine** — Discord bot that passively observes behavioral patterns, staff-member interactions, and moderation actions
2. **Work Engine** — (future) Integrates with Jira/Trello/internal task systems to assign, track, and score tasks

---

## Architecture

### File Structure

| File | Purpose |
|------|---------|
| `app.py` | Flask entry point, blueprint registration, DB init |
| `bot.py` | Discord observer bot (main logic + prefix cache) |
| `bot_commands.py` | Moderation commands (ban, kick, timeout, warn, prefix, scan, purge) |
| `database.py` | SQLAlchemy models (all tables) |
| `scoring.py` | Points engine (award_points, admin_correction, leaderboard) |
| `config.py` | Flask config (reads .env) |
| `routes/dashboard.py` | Web UI routes (/, /worker/<id>, /guild/<guild_id>) |
| `routes/auth.py` | Discord OAuth2 login/callback/logout |
| `routes/api.py` | Worker CRUD API, task management, admin corrections |
| `routes/observer.py` | Bot → server communication API (14 endpoints) |
| `templates/dashboard.html` | Main dashboard |
| `templates/worker.html` | Worker detail page |
| `templates/guild.html` | Guild detail page |

### Database Models

| Table | Purpose | Key Fields |
|-------|---------|------------|
| `workers` | Staff members tracked by the system | name, email, discord_id, role, score |
| `tasks` | Tasks assigned to workers | worker_id, title, status, points_awarded |
| `score_logs` | Points change history | worker_id, change, reason, source, admin_correction |
| `message_records` | Behavioral message metadata | discord_id, guild_id, channel_name, message_length, hour_of_day, day_of_week |
| `guild_info` | Scanned Discord server metadata | guild_id, name, owner, prefix, member/staff/bot counts |
| `guild_roles` | Role permissions per guild | guild_id, role_id, permissions flags, is_mod |
| `guild_members` | Member info per guild | member_id, name, is_staff, is_owner, is_bot, role_ids |
| `behavioral_anomalies` | Detected anomalies per user | discord_id, anomaly_type, severity, details |
| `admin_corrections` | Manual score corrections | worker_id, original/corrected change, corrected_by |
| `community_events` | General community events | discord_id, event_type, score_impact |
| `voice_activity` | Voice channel session logs | guild_id, user_id, channel_name, duration_seconds, hour_of_day |
| `ping_join_events` | @everyone ping → new member correlation | guild_id, moderator_id, new_members, channel |
| `burnout_risks` | Staff burnout risk scores | worker_id, score, anomaly_freq, volume_volatility, reversal_rate |
| `guild_channels` | Channel metadata per guild | guild_id, channel_id, name, type, is_public |

---

## Bot Behavior

### Core Philosophy
- **Passive observer first** — the bot watches, reads audit logs, and infers who did what via staff activity proximity
- **Not primarily a moderation bot** — it doesn't ban/kick/timeout anyone itself (though it has these commands)
- **Attribution via proximity** — when a mod bot (Carl-bot, MEE6, Dyno, etc.) acts, the bot attributes it to the most recently active staff member in that guild (within 60s)
- **Staff auto-detection** — on joining a guild, scans roles/permissions to auto-identify staff (anyone with ADMINISTRATOR, BAN_MEMBERS, KICK_MEMBERS, MANAGE_GUILD, MANAGE_ROLES, or is the server owner)

### Key Events

| Event | Purpose |
|-------|---------|
| `on_message` | 3 jobs: (1) parse mod bot embeds for warns, (2) track staff activity proximity, (3) buffer ALL human messages for behavioral analytics |
| `on_member_ban` | Detects bans via audit log, attributes to staff via proximity if mod bot executed it |
| `on_member_unban` | Detects ban reversal, flags as hasty if within 48h |
| `on_member_update` | Detects timeout add/early removal |
| `on_member_remove` | Detects kicks via audit log |
| `on_guild_join` | Triggers full guild scan (roles, members, permissions) |
| `on_ready` | Logs guild info, starts background tasks, scans all existing guilds |
| `on_presence_update` | Buffers online/offline changes for large guilds |
| `on_member_join` | Tracks new members joining |

### Staff Activity Proximity
- `last_staff_activity[guild_id]` — tracks `{id, name, timestamp}` of the most recent potential moderator
- Updated when: (1) someone sends a mod-command-like message (`!ban`, `,kick`, etc.), (2) someone with ban/kick/manage_messages perms sends any message
- Stale entries cleaned after 60s

### Background Tasks
- `flush_message_loop` (every 30s) — flushes buffered messages, presence updates, voice sessions, and member joins to API
- `check_reversed_actions` (every 1h) — confirms bans that stood 48h+ as valid, also triggers anomaly and burnout scans

### Message Storage Policy
- **Public channels** (@everyone can read) → full message content stored
- **Private/restricted channels** (mod-only, staff-only) → only metadata stored (length, time, channel), content set to NULL
- All analytics (behavioral patterns, anomaly detection, activity scoring) work on metadata alone

### Prefix System
- Default prefix `!ss` always active, not removable
- Multi-prefix per guild stored as JSON array in DB
- Commands: `prefix add ,` adds `,`; `prefix remove ,` removes; `prefix reset` restores default
- Bot also responds to @mention as prefix
- Prefixes cached in-memory in `prefix_cache` dict, fetched from API on startup

---

## Points System

| Action | Points |
|--------|--------|
| Ban issued | +8 |
| Kick issued | +5 |
| Timeout issued | +4 |
| Warn issued | +3 |
| Staff activity (20 msgs) | +1 |
| Ban reversed within 48h | -15 |
| Timeout removed early | -8 |
| Admin correction | variable |

### Task Points

| Action | Points |
|--------|--------|
| Task completed on time | +10 |
| Task completed late | +5 |
| Task missed | -15 |
| Extra contribution (beyond task) | +20 |
| Anomaly detected | -10 |
| Community helpful | +5 |
| Community rule break | -8 |
| Community moderation action | +3 |

---

## API Endpoints

### Observer API (`/api/observer/*`)
All require `Authorization: Bearer <API_KEY>` header.

| Method | Route | Purpose |
|--------|-------|---------|
| POST | `/observer/action` | Log a moderation action (ban/kick/timeout/warn) + award points |
| POST | `/observer/flag` | Log a flagged event (ban reversal, early timeout removal) + deduct points |
| POST | `/observer/warn` | Log a warn parsed from mod bot embed |
| POST | `/observer/activity` | Log passive staff message activity (every 20msgs = 1pt) |
| POST | `/observer/confirm` | Confirm a ban stood 48h+ as valid |
| GET | `/observer/staff-activity` | In-memory staff activity summary |
| POST | `/observer/messages` | Batch behavioral message logging (metadata only) |
| GET | `/observer/analytics/<discord_id>` | Per-user behavioral analytics |
| GET | `/observer/analytics` | Overall behavioral analytics |
| POST | `/observer/guild-scan` | Receive and store full guild scan |
| GET | `/observer/guilds` | List all scanned guilds |
| GET | `/observer/guilds/<id>/members` | List guild members |
| GET | `/observer/guilds/<id>/roles` | List guild roles |
| GET/PATCH | `/observer/guilds/<id>/prefix` | Get or set guild prefixes |
| POST | `/observer/messages` | Batch behavioral message logging |
| POST | `/observer/mentions` | Batch mention tracking |
| POST | `/observer/voice-activity` | Batch voice session logging |
| POST | `/observer/ping-join` | Log @everyone ping → new member events |
| POST | `/observer/burnout-scan` | Compute burnout risk scores for all staff |
| POST | `/observer/cleanup` | Purge message records older than TTL |
| POST | `/observer/anomalies/scan` | Trigger anomaly detection |

### Worker/Task API (`/api/*`)

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/api/workers` | List workers |
| POST | `/api/workers` | Add worker |
| POST | `/api/tasks` | Assign task |
| POST | `/api/tasks/<id>/complete` | Mark task complete |
| POST | `/api/tasks/<id>/miss` | Mark task missed |
| POST | `/api/tasks/<id>/anomaly` | Flag task anomaly |
| POST | `/api/admin/correct` | Admin score correction |
| GET | `/api/leaderboard` | Worker leaderboard |
| GET | `/api/workers/<id>/history` | Worker score history |

### Auth Routes

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/auth/login` | Discord OAuth2 redirect |
| GET | `/auth/callback` | OAuth2 callback |
| POST | `/auth/logout` | Clear session |

---

## Dashboard Routes

| Route | Purpose |
|-------|---------|
| `/` | Landing page (unauthenticated) or dashboard with stats, guild overview, leaderboard, recent activity, anomalies |
| `/worker/<id>` | Worker detail: profile, behavioral analytics, score history, tasks, admin correction |
| `/guild/<guild_id>` | Guild detail: header stats, roles/permissions table, staff list, community list |

---

## UI Design
- Dark theme: `#0b0d11` background, `#111318` cards
- Inter font, gradient accent text (blue → purple)
- Card hover effects (border glow + lift)
- Chart.js visualizations: hourly activity, guild comparison, message distribution, voice activity, staffing overlay
- Consistent nav + footer across all pages
- All timestamps displayed in the viewer's local timezone (client-side conversion)

---

## Login / Authentication
- Discord OAuth2 for dashboard access
- Only users with ADMINISTRATOR or MANAGE_GUILD permission on servers where the bot is also present can log in
- Session-based auth with Flask sessions
- Observer API uses separate Bearer token auth

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Staff proximity instead of command parsing | Works regardless of bot prefix, slash commands, or Discord member caching |
| Track mod-command-like messages from ANY human | `guild.get_member()` may return None, but `message.author` is always available |
| File logging to `%TEMP%` | Console output lost with detached Start-Process on Windows |
| Public channels store content, private channels store metadata only | Balances analytics needs with privacy |
| Buffer + batch POST to API | Avoids HTTP overhead per message; flushes every 30msgs or 30s |
| Full guild scan on join + on startup | Ensures staff list is always up-to-date without manual setup |
| Wipe + replace roles/members on each scan | Simplifies sync; scans are relatively infrequent |
| Client-side timezone conversion | Works immediately on page load without cookies or page refresh |

---

## Anomaly Detection (3 types)
- **odd_hours** — posting at hours never seen before
- **volume_spike** / **volume_drop** — 24h count vs daily average deviation > 2x
- **length_shift** — message length z-score > 2.0 compared to baseline
- Baseline requires 10+ messages per user

## Scoring Algorithm
- Rule-based currently (no ML yet)
- All moderation actions awarded via `/observer/action` endpoint
- Activity points every 20 messages via `/observer/activity`
- Flagged reversals (ban within 48h, early timeout) deduct points via `/observer/flag`
- Admin corrections stored as `AdminCorrection` records for future ML training

---

## Large Guild Optimizations
- For guilds with 1000+ members, `scan_guild()` only processes online members + bots + staff
- Presence updates buffered (flush every 50 events or 30s)
- All in-memory buffers capped at 10k entries to prevent OOM
- `on_member_join` events batched and flushed every 30 joins or 30s

---

## Environment Variables (.env)

```
DISCORD_TOKEN=your_token
SKILLSYNC_API=http://localhost:5000/api
SECRET_KEY=your_secret_key
API_KEY=your_api_key
DISCORD_CLIENT_ID=your_client_id
DISCORD_CLIENT_SECRET=your_client_secret
DISCORD_REDIRECT_URI=http://localhost:5000/auth/callback
```

---

## Running Locally (Windows)

```powershell
# Start Flask with debug logging
$env:FLASK_ENV="development"
Start-Process -WindowStyle Hidden -FilePath "cmd.exe" -ArgumentList "/c set FLASK_ENV=development && .\.venv\Scripts\python.exe app.py > flask_debug.log 2>&1" -WorkingDirectory "C:\Users\ACER\Downloads\SkillSync"

# Start Bot
Start-Process -WindowStyle Hidden -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "bot.py" -WorkingDirectory "C:\Users\ACER\Downloads\SkillSync"

# Kill all
Get-Process python* | Stop-Process -Force
```

---

## Technical Details

- **Bot Name**: KillSync#5939
- **Intents**: message_content, members, moderation, guilds, presences
- **Permissions**: ban_members, kick_members, view_audit_log, manage_messages, moderate_members

### Current Connected Servers
- SunRayBee (13k+ members)
- Rope Agency (x2)
- See i Dih

---

## Git Repository
- **Remote**: https://github.com/Oontlaw/SkillSync.git
- **Branch**: main
- **Not tracked** (gitignored): .env, AGENTS.md, *.db, instance/, __pycache__/, *.log
