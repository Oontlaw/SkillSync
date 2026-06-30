# SkillSync — Smart Workforce Intelligence System

SkillSync is an **AI-powered workforce intelligence platform** that helps
organisations measure, track, and improve team performance. It combines
enterprise task data with community behavioral signals to build a unified
reputation score per individual — without storing raw message content.

The core of the system is the **Workspace**: a private company dashboard where
admins manage workers, assign tasks, review auto-judged scores, and correct
ML predictions. The **Community Engine** (a Discord bot) feeds behavioral
signals into the platform, and the **Work Engine** syncs with Jira for task
automation.

Built as a final-year academic project.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                     WORKSPACE                            │
│            (Private Company Dashboard)                   │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│  │ Workers  │  │  Tasks   │  │Scoring & │  │  Team    │ │
│  │ & IDs    │  │ & Review │  │Overrides │  │  Health  │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘ │
│       │             │             │             │        │
└───────┼─────────────┼─────────────┼─────────────┼────────┘
        │             │             │             │
        ▼             ▼             ▼             ▼
┌─────────────────────────────────────────────────────────┐
│                     SCORING ENGINE                       │
│      (Real-time points + Ridge Regression Corrector)     │
└──────────────┬──────────────────────────┬────────────────┘
               │                          │
               ▼                          ▼
┌─────────────────────┐    ┌──────────────────────────────┐
│   COMMUNITY ENGINE  │    │        WORK ENGINE           │
│   (Discord Bot)     │    │   (Jira / Task Systems)      │
│                     │    │                              │
│ • Message activity  │    │ • Task assignment tracking   │
│ • Moderation events │    │ • Deadline compliance        │
│ • Presence/voice    │    │ • Completion scoring         │
│ • Engagement rates  │    │ • Problem-solving bonuses    │
└──────────┬──────────┘    └──────────────┬───────────────┘
           │                              │
           └──────────┬───────────────────┘
                      ▼
            ┌──────────────────┐
            │  ML PIPELINE     │
            │ (Trained from    │
            │  community data) │
            │                  │
            │ • Anomaly        │
            │ • Burnout risk   │
            │ • Forecasting    │
            │ • Score correct  │
            └──────────────────┘
```

The platform is organised around **three layers**:

1. **Workspace Layer** — The primary interface. Organisations register, invite
   members, link workers, assign tasks, and review ML-generated insights.
2. **Data Layer** — Two engines feed data upward:
   - **Community Engine** (Discord bot) — captures public behavioral signals.
   - **Work Engine** (Jira connector) — syncs enterprise task data.
3. **ML Layer** — Trained primarily on community behavioral patterns, then
   applied within the workspace to detect anomalies, predict burnout, forecast
   activity, and correct scores.

---

## The Workspace (Primary Interface)

The Workspace is a private, auth-protected dashboard for each organisation.
It is completely separate from the Discord OAuth dashboard — organisations
register with email/password and get their own isolated environment.

### Key Pages

| Route | Page | Description |
|---|---|---|
| `/workspace/register` | Register | Create a new organisation |
| `/workspace/login` | Login | Email/password auth with rate limiting |
| `/workspace/` | Dashboard | Worker counts, task stats, recent points, health snapshot |
| `/workspace/workers` | Workers | Browse all linked workers |
| `/workspace/workers/<id>` | Worker Detail | Full profile, points, activity, anomalies |
| `/workspace/workers/<id>/summary` | Summary | Auto-generated 30-day performance report |
| `/workspace/leaderboard` | Leaderboard | Points ranking (7d / 30d / all-time) |
| `/workspace/team-health` | Team Health | Traffic-light health indicators per worker |
| `/workspace/tasks/create` | Create Task | Assign tasks with due dates and point values |
| `/workspace/work/review` | Review Work | Confirm or correct auto-judged ScoreLog entries |
| `/workspace/identities` | Identities | Link Discord users to worker profiles |
| `/workspace/overrides` | Overrides | View anomalies/burnout risks and issue corrections |
| `/workspace/members` | Members | Invite, role-manage, and remove org members |
| `/workspace/settings` | Settings | Org config, Jira integration, Slack webhook |

### How the Workspace Works

1. An **organisation registers** at `/workspace/register` with a name and unique
   slug. The first account is an admin.
2. The **admin invites members** — each gets login credentials for the
   workspace.
3. **Workers are linked** to Discord users via the Identities page. This
   bridges community behavior and workplace data into a single profile.
4. **Tasks are created** and assigned to workers. Completions award points;
   missed tasks deduct. The Work Engine can auto-create tasks from Jira.
5. **ML monitors everyone** — the Community Engine feeds behavioral data into
   the ML pipeline, which flags anomalies, burnout risks, and activity changes.
6. **Admins review and correct** — flagged items appear on the Overrides page.
   Each correction is stored as a labeled training example.
7. **The model improves** over time as corrections accumulate. Admins can
   trigger a retrain from the workspace dashboard.

### Auth Model

- Three roles: **admin** (full control), **hr** (review + correct), **member**
  (read-only).
- Login is rate-limited: 5 attempts per 15-minute window per email.
- Sessions are isolated per organisation — no cross-org data leakage.

---

## The Community Engine (Data Source)

The Community Engine is a **Discord bot** that feeds behavioral data into the
workspace. It is a supporting component — its sole purpose is to collect the
signals that the ML pipeline needs to generate workspace insights.

- Listens to: messages, moderation actions, member joins/leaves, presence
  updates, voice state changes.
- Stores only **metadata** — counts, rates, timestamps. Raw message content
  is never persisted.
- All collected data flows into the ML pipeline via the observer API.

---

## The Work Engine (Jira Integration)

The Work Engine connects to **Jira** to sync enterprise tasks into the workspace
automatically. It polls configured Jira projects, maps issues to workers, and
creates ScoreLog entries that admins can review.

- SSRF-safe URL validation on all Jira endpoints.
- API tokens encrypted at rest using Fernet symmetric encryption.
- Per-organisation Jira configuration via workspace settings.

---

## ML Pipeline

Trained primarily on community behavioral patterns, then applied within the
workspace context:

| Module | Model | Purpose |
|---|---|---|
| Anomaly Detection | Isolation Forest | Flags unusual activity changes per user |
| Burnout Risk | Weighted signal scoring | Identifies early disengagement signals |
| Activity Forecast | Random Forest + hourly profile | Predicts daily volume and 24h distribution |
| Score Corrector | Ridge regression | Adjusts raw scores using admin corrections |
| Federated Learning | Round-based aggregation | Cross-organisation pattern learning |

---

## Tech Stack

| Layer         | Technology                                                  |
|---------------|-------------------------------------------------------------|
| Backend       | Python 3.14, Flask 3.1                                      |
| Database      | PostgreSQL (primary), SQLite (dev/tests)                    |
| ORM           | SQLAlchemy 2.0 + Flask-Migrate (Alembic)                    |
| ML / AI       | scikit-learn (Random Forest, Isolation Forest, Ridge, etc.) |
| Discord Bot   | discord.py 2.7                                               |
| Auth          | Discord OAuth2 + workspace email/password                   |
| Frontend      | Jinja2 templates, Chart.js, vanilla CSS                     |
| Task Systems  | Jira REST API, custom webhooks                              |
| Deploy        | Local server / Docker-ready                                 |

---

## Folder Structure

```
SkillSync/
├── app.py                 Flask app factory + blueprint registration
├── bot.py                 Discord bot instance (Community Engine)
├── bot_commands.py        Discord slash/prefix commands
├── bot_core/              Discord bot internals
│   ├── api_client.py      HTTP client → Flask observer endpoints
│   ├── config.py          Bot config (intents, token)
│   ├── events_messages.py on_message handler
│   ├── events_moderation.py  Ban/unban/update events
│   ├── events_presence.py Presence / join / voice events
│   ├── events_ready.py    on_ready + guild_join
│   ├── heartbeat.py       Periodic health-check loop
│   ├── logging.py         Bot-side logging
│   ├── parsers.py         Message parsing utilities
│   ├── privacy.py         Metadata-first filtering rules
│   ├── scanner.py         Per-guild message scan
│   ├── state.py           In-memory caches
│   └── tasks.py           Scheduled background tasks
│
├── config.py              Configuration (reads from .env)
├── database.py            SQLAlchemy models
├── scoring.py             Core scoring logic
├── requirements.txt
│
├── run_dashboard.py       Entry: start the Flask dev server
├── run_bot.py             Entry: start the Discord bot
├── start_services.py      Combined launcher
├── scripts/               Helper scripts (migrate, ngrok watchdog)
├── archive/               Legacy launchers
│
├── routes/                Flask blueprints
│   ├── dashboard.py       Main dashboard
│   ├── workspace.py       Workspace pages (primary interface)
│   ├── work.py            Work engine API
│   ├── observer.py        Bot-facing endpoints + ML control
│   ├── api.py             Public API
│   ├── auth.py            Discord OAuth
│   ├── community.py       Community API
│   └── security.py        CSRF protection
│
├── work_engine/           Jira integration
│   ├── connector_jira.py  Jira API client (SSRF-safe)
│   ├── scoring.py         Task scoring
│   └── webhook.py         Webhook receiver
│
├── ml/                    Machine learning modules
│   ├── anomaly.py         Isolation Forest anomaly detection
│   ├── burnout.py         Burnout risk scoring
│   ├── corrector.py       Score corrector (Ridge regression)
│   ├── engine.py          Orchestrator
│   ├── features.py        Feature engineering
│   ├── federated.py       Federated learning
│   ├── forecast.py        Activity forecasting
│   ├── growth.py          Growth model
│   ├── work_anomaly.py    Work-specific anomaly detection
│   └── work_features.py   Work-specific feature extraction
│
├── services/              Utility services
│   └── slack.py           Slack webhook notifications
│
├── templates/             Jinja2 HTML templates
│   ├── workspace_*.html   10+ workspace pages
│   ├── dashboard.html     Main dashboard
│   ├── guild.html         Guild detail + forecast chart
│   ├── landing.html       Landing page
│   ├── worker.html        Worker detail
│   └── base.html          Base layout
│
├── static/                CSS, fonts, Chart.js
├── migrations/            Alembic database migrations
├── tests/                 Pytest test suite
├── docs/                  Documentation
│
├── .env.example           Environment variable template
├── .gitignore
└── README.md
```

---

## Local Setup

### Prerequisites

- Python 3.14+
- PostgreSQL (optional — SQLite works for development)
- Discord bot token ([Discord Developer Portal](https://discord.com/developers/applications))

### Quick Start

```bash
# 1. Create virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env with your settings

# 4. Run the dashboard
python run_dashboard.py

# 5. (Optional) Run the bot
python run_bot.py

# 6. Open http://localhost:5000
# Register an organisation at /workspace/register to get started
```

### Environment Variables

| Variable              | Required   | Default                   | Description                            |
|-----------------------|------------|---------------------------|----------------------------------------|
| `SECRET_KEY`          | ✅         | —                         | Flask session encryption key           |
| `DATABASE_URL`        | ✅         | `sqlite:///skillsync.db`  | Database connection string             |
| `DISCORD_TOKEN`       | For bot    | —                         | Discord bot token                      |
| `DISCORD_CLIENT_ID`   | For OAuth  | —                         | Discord OAuth2 client ID               |
| `DISCORD_CLIENT_SECRET` | For OAuth | —                        | Discord OAuth2 client secret           |
| `API_KEY`             | For bot    | —                         | Shared secret for bot→Flask API calls  |
| `JIRA_ENCRYPTION_KEY` | Optional   | —                         | Fernet key for Jira token encryption   |

See `.env.example` for the full list.

---

## Running Tests

```bash
python -m pytest tests/ -v
```

Key test files:
- `tests/test_forecast.py` — Forecast accuracy, SSRF validation, legacy log filtering
- `tests/test_security_and_routes.py` — Auth, CSRF, rate limiting
- `tests/test_observer_and_bot.py` — Observer endpoint behavior

---

## Scripts Reference

| Command                        | What it does                          |
|--------------------------------|---------------------------------------|
| `python run_dashboard.py`      | Start the Flask dashboard on :5000    |
| `python run_bot.py`            | Start the Discord bot                 |
| `python app.py`                | Legacy — same as run_dashboard.py     |
| `python bot.py`                | Legacy — same as run_bot.py           |
| `python start_services.py`     | Launch dashboard + bot + ngrok tunnel |
| `python -m pytest tests/`      | Run the test suite                    |

---

## Privacy & Ethics

- **Metadata-first**: Raw Discord message content is never stored. Only message
  counts, rates, and metadata (timestamps, channel IDs) are recorded.
- **Federated learning**: Raw company data never leaves the internal network.
  Only anonymized behavioral patterns contribute to the global model.
- **Admin-in-the-loop**: Every automated decision is reviewable and overridable
  by human administrators. The AI is an assistant, not a replacement.
- **Token encryption**: Jira API tokens are encrypted at rest using Fernet
  symmetric encryption.
- **Academic prototype**: ML scoring outputs are illustrative. Do not use for
  real personnel decisions without thorough validation.

---

## Limitations

- Blueprint prefix collision: `api_bp` and `community_bp` both register on `/api`.
  Works currently but is fragile.
- SQLite lock issues in tests: all database operations for a test must be inside
  a single `app.app_context()` block.
- Single-process deployment: the in-memory rate limiter does not work across
  multiple workers.

---

## References

1. Goodfellow, I., Bengio, Y., & Courville, A. (2016). *Deep Learning*. MIT Press.
2. McMahan, B., et al. (2017). Communication-Efficient Learning of Deep Networks
   from Decentralized Data. *Proceedings of AISTATS*.
3. Discord Developer Documentation. https://discord.com/developers/docs
4. PostgreSQL Documentation. https://www.postgresql.org/docs/
5. Flask Documentation. https://flask.palletsprojects.com/
6. Scikit-learn: Machine Learning in Python. Pedregosa et al., *JMLR* 12,
   pp. 2825–2830, 2011.

---

## License

Academic project — submitted as part of a final-year degree program.
