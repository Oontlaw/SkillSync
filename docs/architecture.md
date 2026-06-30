# SkillSync Architecture

## Overview

SkillSync is a **workforce intelligence platform** organised around three layers:

1. **Workspace Layer** — Private company dashboards where organisations manage
   workers, assign tasks, review ML-generated insights, and correct scores.
2. **Data Layer** — Two engines feed data upward:
   - **Community Engine** (Discord bot) — captures public behavioral signals.
   - **Work Engine** (Jira connector) — syncs enterprise task data.
3. **ML Layer** — Trained on community behavioral patterns and applied within
   the workspace to detect anomalies, predict burnout, forecast activity, and
   correct scores.

---

## High-Level Structure

```
SkillSync/
│
├── app.py                  Flask app factory + blueprint registration
├── bot.py                  Discord bot instance (Community Engine)
├── bot_commands.py         Discord slash/prefix commands
├── config.py               Flask config (reads from .env)
├── database.py             SQLAlchemy models
├── scoring.py              Core scoring logic
├── requirements.txt
│
├── run_dashboard.py        Top-level entry: start the Flask dev server
├── run_bot.py              Top-level entry: start the Discord bot
├── start_services.py       Combined launcher (dashboard + bot + ngrok)
├── scripts/                Helper scripts (migrate, ngrok watchdog)
├── archive/                Legacy launchers
│
├── routes/                 Flask blueprints
│   ├── workspace.py        Primary interface — workspace pages
│   ├── work.py             Work engine API
│   ├── dashboard.py        Main dashboard
│   ├── observer.py         Bot-facing endpoints + ML control
│   ├── api.py              Public API
│   ├── auth.py             Discord OAuth2
│   ├── community.py        Community API
│   └── security.py         CSRF protection helpers
│
├── work_engine/            Jira / external task integration
│   ├── connector_jira.py   Jira API client (SSRF-safe)
│   ├── scoring.py          Task scoring for work engine
│   └── webhook.py          Webhook receiver
│
├── bot_core/               Discord bot internals (Community Engine)
│   ├── api_client.py       HTTP client to send data to Flask observer
│   ├── config.py           Bot config (intents, token loader)
│   ├── events_messages.py  on_message handler
│   ├── events_moderation.py  on_member_ban/unban/update/remove
│   ├── events_presence.py  on_presence_update / on_member_join / voice
│   ├── events_ready.py     on_ready + on_guild_join
│   ├── heartbeat.py        Periodic health-check loop
│   ├── logging.py          Bot-side logging
│   ├── parsers.py          Discord message parsing
│   ├── privacy.py          Metadata-first filtering rules
│   ├── scanner.py          Per-guild message scan
│   ├── state.py            In-memory caches
│   └── tasks.py            Scheduled background tasks
│
├── ml/                     ML modules (trained from community data)
│   ├── anomaly.py          Isolation Forest anomaly detection
│   ├── burnout.py          Burnout risk detection (weighted signals)
│   ├── corrector.py        Score corrector (Ridge regression)
│   ├── engine.py           Orchestrator: train_all, status, accuracy
│   ├── features.py         Feature engineering from community data
│   ├── federated.py        Federated learning (round-based aggregation)
│   ├── forecast.py         Activity forecast (Random Forest + hourly profile)
│   ├── growth.py           Guild/user growth model
│   ├── work_anomaly.py     Work-engine anomaly detection
│   └── work_features.py    Work-engine feature extraction
│
├── services/               Utility services
│   └── slack.py            Slack webhook notifications
│
├── templates/              Jinja2 HTML templates
├── static/                 CSS, fonts, Chart.js
├── migrations/             Alembic database migrations
├── tests/                  Pytest test suite
└── docs/                   Project documentation
```

---

## Workspace Module

The workspace is the **primary interface** of SkillSync. Each organisation gets
a private, auth-protected environment completely separate from the Discord
OAuth dashboard.

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

- Three roles: **admin** (full control), **hr** (review+correct), **member** (read-only).
- Rate-limited login: 5 attempts / 15 min per email.
- Sessions isolated per organisation.

---

## The Community Engine (Data Source)

The Discord bot is a **supporting component** that feeds behavioral data into
the ML pipeline. It does not have its own dashboard or standalone value — its
sole purpose is to generate the signals that the workspace uses.

- **Metadata-first**: Only counts, rates, and timestamps are stored. Raw
  message content is never persisted.
- **Event types**: Messages, moderation actions, member joins/leaves, presence
  updates, voice state changes.
- **Privacy**: Messages from private channels and bots are filtered out.

---

## The Work Engine (Jira Integration)

The Work Engine connects to Jira to automate task tracking within the workspace:

- Polls configured Jira projects per organisation.
- Maps Jira issues to linked workers.
- Creates ScoreLog entries for admin review.
- SSRF-safe URL validation on all Jira endpoints.
- API tokens encrypted at rest with Fernet.

---

## ML Pipeline

All ML modules are trained on community behavioral data and their outputs are
consumed by the workspace:

| Module | Model | Consumed By |
|---|---|---|
| Anomaly Detection | Isolation Forest | Workspace Overrides page |
| Burnout Risk | Weighted signal scoring | Workspace Team Health page |
| Activity Forecast | Random Forest | Guild dashboard (read-only) |
| Score Corrector | Ridge regression | Workspace scoring engine |
| Federated Learning | Round-based aggregation | Global model training |

---

## Data Flow

```
Community Engine (Discord Bot)
       │
       ▼  (HTTP POST via API client)
Observer API Endpoints
       │
       ▼
Database (PostgreSQL / SQLite)
       │
       ├──► ML Training (anomaly, burnout, forecast, corrector)
       │         │
       │         ▼
       ├──► Workspace Routes (dashboard, workers, overrides)
       │         │
       │         ▼
       └──► Jinja2 Templates (workspace pages)
                 │
                 ▼
            Admin Dashboard
                 │
                 ▼
            Admin Corrections ──► ML Retrain (closed-loop)
```

---

## Key Design Decisions

1. **Workspace-first architecture** — The workspace is the primary interface.
   The Discord bot is a data source, not a standalone product.
2. **Metadata-first tracking** — Raw message content never stored.
3. **Admin-in-the-loop** — Every ML decision is reviewable and overridable.
   Corrections feed back into model retraining.
4. **Federated learning** — Company data stays on-premises.
5. **Security** — SSRF-safe Jira URLs, Fernet token encryption, rate-limited
   login, CSP headers.
6. **Academic prototype** — Designed as a final-year project. ML scoring should
   not be used for real personnel decisions.

---

## Running Locally

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your settings

python run_dashboard.py    # Start Flask
# or
python run_bot.py          # Start Discord bot
```

---

## Developer Notes

- **SQLite lock issues** — all DB ops for a test must be inside a single
  `with app.app_context():` block.
- **Two blueprints share `/api` prefix** — `api_bp` and `community_bp`.
  Works currently but is fragile.
- **Bot imports are safe** — importing bot modules does not start the bot.
  Token is only used when `bot.start()` is called.
