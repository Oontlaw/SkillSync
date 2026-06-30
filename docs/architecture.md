# SkillSync Architecture

## Overview

SkillSync is a Discord-based productivity and community intelligence platform. It
combines a **Discord bot** (observer), a **Flask dashboard** (web UI), **ML modules**
(anomaly/burnout/forecast/federated), a **work engine** (Jira integration), and a
**scoring engine** into a single Python application.

---

## High-Level Structure

```
SkillSync/
│
├── app.py                  Flask application factory + blueprint registration
├── bot.py                  Discord bot instance + event registration
├── bot_commands.py         Discord slash/prefix commands (Moderation cog)
├── config.py               Flask configuration (SECRET_KEY, DB URL, etc.)
├── database.py             SQLAlchemy models + DB helpers
├── scoring.py              Core scoring functions
├── requirements.txt        Python dependencies
│
├── run_dashboard.py        Top-level entry: start the Flask dev server
├── run_bot.py              Top-level entry: start the Discord bot
├── start_services.py       Combined launcher (dashboard + ngrok + bot)
├── scripts/                Helper scripts (migrate, ngrok watchdog)
├── archive/                Legacy launchers (start/stop .bat)
│
├── routes/                 Flask blueprints (API endpoints + page routes)
│   ├── api.py
│   ├── auth.py
│   ├── community.py
│   ├── dashboard.py
│   ├── observer.py
│   ├── security.py
│   ├── work.py
│   └── workspace.py
│
├── bot_core/               Discord bot internals
│   ├── api_client.py       HTTP client to send data to Flask observer endpoints
│   ├── config.py           Bot config (intents, token loader)
│   ├── events_messages.py  on_message handler
│   ├── events_moderation.py  on_member_ban/unban/update/remove
│   ├── events_presence.py  on_presence_update / on_member_join / voice_state
│   ├── events_ready.py     on_ready + on_guild_join
│   ├── heartbeat.py        Periodic health-check loop
│   ├── logging.py          Bot-side logging
│   ├── parsers.py          Discord message parsing utilities
│   ├── privacy.py          Filtering rules for metadata-first tracking
│   ├── scanner.py          Per-guild message scan
│   ├── state.py            In-memory caches (prefix, retrain flags)
│   └── tasks.py            Scheduled background tasks
│
├── ml/                     Machine learning modules
│   ├── anomaly.py          Isolation Forest anomaly detection
│   ├── burnout.py          Burnout risk detection
│   ├── corrector.py        Score corrector (Ridge regression)
│   ├── engine.py           Orchestrator: train_all, get_model_status, etc.
│   ├── features.py         Feature engineering
│   ├── federated.py        Federated learning (simple round-based)
│   ├── forecast.py         Daily-total forecast + hourly distribution
│   ├── growth.py           Guild/user growth model (placeholder)
│   ├── work_anomaly.py     Work-engine anomaly detection
│   └── work_features.py    Work-engine feature extraction
│
├── work_engine/            Jira / external task integration
│   ├── connector_jira.py   Jira API client + SSRF-safe URL validation
│   ├── scoring.py          Task scoring for work engine
│   └── webhook.py          Webhook receiver
│
├── services/               Utility services
│   └── slack.py            Slack webhook notifications
│
├── templates/              Jinja2 HTML templates
│   ├── base.html
│   ├── dashboard.html
│   ├── guild.html
│   ├── landing.html
│   ├── worker.html
│   ├── workspace_base.html       + 10 workspace_*.html pages
│   └── workspace_*.html
│
├── static/                 Static assets
│   ├── dashboard.css
│   ├── landing_v2.css
│   ├── workspace.css
│   ├── chart.umd.min.js
│   └── fonts/
│
├── migrations/             Alembic migrations
├── tests/                  Pytest test suite
├── docs/                   Project documentation
│
├── .env.example            Environment variable template
├── .gitignore
├── README.md
└── SUBMISSION_NOTES.md
```

---

## Workspace Module

SkillSync includes a **private workspace dashboard** for organisations, completely
separate from the Discord OAuth dashboard.

### Pages

| Route | Purpose |
|---|---|
| `/workspace/register` | Create a new organisation |
| `/workspace/login` | Email/password login with rate limiting |
| `/workspace/` | Org dashboard with worker/task/health overview |
| `/workspace/workers` | Worker directory |
| `/workspace/workers/<id>` | Worker profile, scores, anomalies |
| `/workspace/workers/<id>/summary` | 30-day auto-generated performance summary |
| `/workspace/leaderboard` | Points ranking (7d/30d/all) |
| `/workspace/team-health` | Per-worker traffic-light health indicators |
| `/workspace/tasks/create` | Create and assign tasks |
| `/workspace/work/review` | Review auto-judged ScoreLog entries |
| `/workspace/identities` | Link Discord users ↔ worker profiles |
| `/workspace/overrides` | View anomalies/burnout + issue corrections |
| `/workspace/members` | Invite/remove org members, manage roles |
| `/workspace/settings` | Org settings, Jira config, Slack webhook |

### Auth
- Three roles: **admin**, **hr** (review+correct), **member** (read-only).
- Rate-limited login: 5 attempts / 15 min.
- Sessions isolated per organisation.

## Data Flow

```
Discord Events
     │
     ▼
bot_core/events_*.py  ──►  API Client  ──►  Flask /api/observer/* endpoints
     │                                              │
     │                                              ▼
     │                                         database.py
     │                                         (PostgreSQL / SQLite)
     │                                              │
     ▼                                              ▼
bot_commands.py  (slash commands)             ml/ modules (training)
                                                    │
                                                    ▼
                                              scoring.py / corrector.py
                                                    │
                                                    ▼
                                              dashboard routes
                                              (Jinja2 templates)
```

---

## Key Design Decisions

1. **Metadata-first tracking** — raw Discord message content is never stored;
   only derived metrics (counts, rates, timestamps, flags).

2. **Dual auth** — Discord OAuth for dashboard access; email/password for
   workspace pages.

3. **Forecast v2** — every forecast prediction log carries `resolution_version: 2`
   and `actual_granularity: "hourly"` so accuracy metrics only use properly
   resolved hourly data.

4. **SSRF-safe Jira** — `_validate_jira_url()` in the connector blocks private
   IPs, loopback, link-local, and cloud metadata endpoints.

5. **Token encryption at rest** — Jira API tokens are encrypted with Fernet
   before storage; decrypted on use.

6. **Academic prototype** — designed as a final-year project, not production HR
   software. Scoring and ML modules are illustrative and should not be used for
   real personnel decisions.

---

## Running Locally

```bash
# 1. Create virtual environment
python -m venv .venv
.venv\Scripts\activate    # Windows
source .venv/bin/activate # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env with your settings

# 4. Run the dashboard
python run_dashboard.py

# 5. (Optional) Run the bot
python run_bot.py

# 6. Run tests
python -m pytest tests/ -v
```

---

## Developer Notes

- **Test database** — tests use SQLite; the `conftest.py` handles app context.
- **SQLite lock issues** — all DB ops for a test must be in a single
  `with app.app_context():` block.
- **Two blueprints share `/api` prefix** — `api_bp` and `community_bp` in
  `app.py`. Works currently but is fragile.
