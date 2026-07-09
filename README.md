# SkillSync — Smart Workforce Intelligence System

SkillSync is a **dual-engine AI-powered workforce intelligence platform** for automated performance assessment. It combines community behavioral signals with enterprise task data to build a unified, continuously learning reputation score per individual — without storing raw message content.

Two engines feed into a shared ML pipeline:

1. **Community Engine** — A Discord bot that collects behavioral signals (messages, moderation, presence, voice)
2. **Work Engine** — A Jira connector that syncs enterprise task data

The **Workspace** (private company dashboard) is where everything comes together: admins manage workers, assign tasks, review ML-generated insights, and issue corrections that retrain the model.

Built as a final-year academic project.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                     INPUT LAYER                                   │
│                                                                   │
│  ┌─────────────────────┐    ┌──────────────────────────────────┐  │
│  │   COMMUNITY ENGINE  │    │         WORK ENGINE              │  │
│  │   (Discord Bot)     │    │   (Jira / Task Integration)      │  │
│  │                     │    │                                  │  │
│  │ • Message activity  │    │ • Task assignment tracking       │  │
│  │ • Moderation events │    │ • Deadline compliance            │  │
│  │ • Presence / voice  │    │ • Completion scoring             │  │
│  │ • Engagement rates  │    │ • Problem-solving bonuses        │  │
│  └──────────┬──────────┘    └──────────────┬───────────────────┘  │
└─────────────┼──────────────────────────────┼──────────────────────┘
              │                              │
              ▼                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                      EMBEDDING LAYER                              │
│       (Buffered batch processing — bot_core / work_engine)        │
│    Raw events → numerical feature vectors (28-dim / 10-dim)      │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                      ML PIPELINE (7 models)                       │
│                                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ Anomaly  │  │ Burnout  │  │Forecast  │  │Score Corrector   │  │
│  │Detection │  │  Risk    │  │(24h vol) │  │(Ridge Regression)│  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────────┬─────────┘  │
│       │             │             │                  │            │
│  ┌────┴─────────────┴─────────────┴──────────────────┴─────────┐  │
│  │         Federated Learning (cross-guild aggregation)        │  │
│  └─────────────────────────────────────────────────────────────┘  │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                      OUTPUT LAYER                                 │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │                     WORKSPACE                             │    │
│  │            (Private Company Dashboard)                    │    │
│  │                                                           │    │
│  │  Workers & IDs  │  Tasks & Review  │  Scoring & Overrides │    │
│  │  Team Health    │  Leaderboard     │  Settings / Jira     │    │
│  └──────────────────────────────────────────────────────────┘    │
│                             │                                     │
│                             ▼                                     │
│              Admin Correction → ML Retrain (closed-loop)          │
└──────────────────────────────────────────────────────────────────┘
```

The system works as a **closed loop**:

1. **Data is collected** — The Community Engine and Work Engine continuously feed behavioral and task data into the database
2. **ML models train** — 7 models (anomaly detection, burnout risk, activity forecast, score correction, federated learning, growth prediction, work anomaly) train on accumulated data
3. **Insights are surfaced** — The Workspace displays scores, anomalies, forecasts, and health indicators to admins
4. **Admins review and correct** — Every ML decision is reviewable. Corrections are stored as labeled training examples
5. **Models improve** — A retrain cycle incorporates admin corrections, making the system more accurate over time

---

## Deployment Status

SkillSync is deployed and actively collecting data:

| Server | Members | Purpose |
|---|---|---|
| Main production server | ~13,000 | Community activity, moderation, messages |
| Secondary test server | ~500 | Development testing |
| Debug / testing guild | ~50 | ML debugging |
| Additional test guild | ~50 | Integration testing |

**Data collected:** 52,400+ messages processed across all guilds

**ML Pipeline Status:**
- Anomaly detection: 135 users profiled, Isolation Forest trained
- Burnout scoring: 5-signal composite with admin validation
- Forecast: 2 main guilds trained, daily + hourly accuracy metrics
- Score corrector: Ridge regression trained on admin correction history
- Federated learning: 45 completed rounds, cross-guild pattern aggregation

---

## The Workspace (Primary Interface)

The Workspace is the heart of SkillSync — a private, auth-protected dashboard where organizations manage their workforce intelligence. It is completely separate from Discord OAuth; organizations register with email/password and get their own isolated environment.

### Key Pages

| Route | Page | Description |
|---|---|---|
| `/workspace/register` | Register | Create a new organisation (admin account) |
| `/workspace/login` | Login | Email/password auth with rate limiting (5 attempts / 15 min) |
| `/workspace/` | Dashboard | Worker counts, task stats, recent points, health snapshot |
| `/workspace/workers` | Workers | Browse all workers linked to Discord + Jira identities |
| `/workspace/workers/<id>` | Worker Detail | Full profile, points history, activity graph, flagged anomalies |
| `/workspace/workers/<id>/summary` | Summary | Auto-generated 30-day performance report |
| `/workspace/leaderboard` | Leaderboard | Points ranking (7d / 30d / all-time) |
| `/workspace/team-health` | Team Health | Traffic-light health indicators per worker (ML-driven) |
| `/workspace/tasks/create` | Create Task | Assign tasks with due dates and point values |
| `/workspace/work/review` | Review Work | Confirm or correct auto-judged ScoreLog entries |
| `/workspace/identities` | Identities | Link Discord users ↔ worker profiles (bridges both engines) |
| `/workspace/overrides` | Overrides | View ML-flagged anomalies / burnout risks and issue corrections |
| `/workspace/members` | Members | Invite, role-manage, and remove org members |
| `/workspace/settings` | Settings | Org config, Jira integration, Slack webhook, privacy flags |

### Auth Model

- Three roles: **admin** (full control), **hr** (review + correct), **member** (read-only)
- Login rate-limited: 5 attempts / 15 min per email
- Sessions isolated per organisation — no cross-org data leakage
- CSRF protection on all mutation endpoints

---

## The Community Engine (Discord Bot)

The Community Engine is **not a standalone moderation bot** — it is a data source that feeds behavioral signals into the ML pipeline. Its sole purpose is to collect the raw signals that the workspace uses to generate intelligence.

### What It Captures

| Data Point | What's Stored | What's NOT Stored |
|---|---|---|
| Messages | Count, length, channel, timestamp | Raw message content |
| Moderation | Ban/kick/timeout/warn actions | Reason text |
| Presence | Online/offline transitions | IP / device info |
| Voice | Session start, duration | Audio content |
| Roles | Role changes, member joins/leaves | N/A |

### How It Works

1. **Event listeners** in `bot_core/` capture Discord events in real time
2. **Buffered flush** — events are collected in-memory for 30 seconds, then batch-sent to the observer API
3. **Metadata-first** — only counts, rates, and timestamps are persisted
4. **Privacy filters** — messages from private channels and known bot accounts are excluded
5. **8 background tasks** run on intervals: buffer flush (30s), ML forecast (1h), Jira poll (1h), cleanup (6h), health digest (168h)

### Bot Commands

The bot supports prefix commands for server management:

| Command | Description | Permission |
|---|---|---|
| `!ss prefix add/remove/list` | Custom prefix management | Admin |
| `!ss ban/kick/timeout/warn/purge` | Moderation actions | Moderate members |
| `!ss scan` | Full guild scan (roles, channels, members) | Admin |
| `!ss inrole @role` | List members with a specific role | Moderate members |
| `!ss avatar @user` | Show user's avatar | Everyone |

---

## The Work Engine (Jira Integration)

The Work Engine connects to Jira to synchronize enterprise task data into the workspace.

- **SSRF-safe URL validation** on all Jira endpoints (blocks private IPs, loopback, metadata endpoints)
- **API tokens encrypted at rest** using Fernet symmetric encryption (graceful fallback if key not set)
- **Per-organisation configuration** via Workspace Settings page
- **Auto-creates worker identities** from Jira account IDs
- **Priority-based scoring** with deadline compliance tracking

---

## ML Pipeline

Seven models trained on community behavioral data and applied within the workspace:

| Module | Model | Window | What It Does |
|---|---|---|---|
| **Anomaly Detection** | Isolation Forest | 30d | Flags cross-user behavioral outliers (volume spikes, pattern shifts) |
| **Burnout Risk** | 5 weighted signals | 90d baseline | Composite burnout score (0-100) from engagement decline, gaps, response time, etc. |
| **Activity Forecast** | Random Forest + hourly profile | 30d train, all-time eval | Predicts daily message volume + 24h hourly distribution |
| **Score Corrector** | Ridge + LogisticRegression | All admin history | Predicts correct score change from past admin overrides |
| **Federated Learning** | LogisticRegression (FedAvg) | 30d | Cross-guild off-hours behavior classification (privacy-preserving) |
| **Growth Prediction** | Random Forest | 90d | 7-day join/leave forecast and anomalous growth detection |
| **Work Anomaly** | Isolation Forest (per-org) | 30d | Work behavior anomalies (completion rate, deadlines, priority handling) |

### Accuracy & Honest Reporting

- **Daily volume accuracy**: Uses absolute error with `max(actual * 0.15, 25)` tolerance
- **Hourly accuracy**: Per-hour comparison with `max(actual_hour * 0.25, 10)` tolerance
- **v2 logging only**: Legacy v1 logs (which stored daily totals in hourly rows) are excluded from metrics
- **Sample counts**: All accuracy metrics display their sample size alongside the percentage
- **No page-created logs**: Guild page views read forecasts without writing new PredictionLog rows

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.14, Flask 3.1 |
| Database | PostgreSQL (production), SQLite (dev / tests) |
| ORM | SQLAlchemy 2.0 + Alembic migrations |
| ML / AI | scikit-learn (Random Forest, Isolation Forest, Ridge, Logistic Regression) |
| Discord Bot | discord.py 2.7 |
| Auth | Discord OAuth2 + workspace email/password (bcrypt + rate-limited) |
| Encryption | Fernet (cryptography) for Jira API tokens |
| Frontend | Jinja2 templates + Chart.js + vanilla CSS |
| Task Integration | Jira REST API + custom webhook receiver |
| Notifications | Slack webhook (9 notification types) |
| Deployment | Local server / Docker-ready |

---

## Folder Structure

```
SkillSync/
├── app.py                 Flask app factory + blueprint registration + security headers
├── bot.py                 Discord bot instance + event handlers
├── bot_commands.py        Prefix / slash bot commands (moderation, utility)
├── config.py              Configuration (reads from .env)
├── database.py            SQLAlchemy models (28 tables) + Fernet encrypt/decrypt helpers
├── scoring.py             Core scoring logic (community + work)
├── requirements.txt       Python dependencies
│
├── run_dashboard.py       Entry: start Flask dev server
├── run_bot.py             Entry: start Discord bot
├── start_services.py      Combined launcher (Flask + bot + ngrok)
├── scripts/               Helper scripts (migrate, ngrok watchdog)
├── archive/               Legacy launchers
│
├── routes/                Flask blueprints
│   ├── workspace.py       Primary interface — ~14 workspace pages
│   ├── observer.py        Bot-facing endpoints + ML control (~60 routes)
│   ├── dashboard.py       Main guild dashboard
│   ├── work.py            Work engine API
│   ├── api.py             Public API (workers, tasks, leaderboard, corrections)
│   ├── auth.py            Discord OAuth2 login
│   ├── community.py       Community event logging
│   └── security.py        CSRF, auth decorators, guild/worker scope helpers
│
├── bot_core/              Discord bot internals
│   ├── api_client.py      HTTP client → Flask observer endpoints
│   ├── config.py          Bot config (intents, token, buffer limits)
│   ├── events_messages.py on_message handler
│   ├── events_moderation.py  Ban/unban/update events
│   ├── events_presence.py Presence / join / voice events
│   ├── events_ready.py    on_ready + on_guild_join + 8 background task loops
│   ├── heartbeat.py       Periodic health-check loop
│   ├── logging.py         Bot-side logging
│   ├── parsers.py         Mod bot embed parsing, AutoMod alert parsing
│   ├── privacy.py         Metadata-first filtering rules
│   ├── scanner.py         Per-guild scan (roles, channels, members, AutoMod)
│   ├── state.py           In-memory caches, buffers, prefix cache
│   └── tasks.py           8 background scheduled tasks
│
├── work_engine/           Jira integration
│   ├── connector_jira.py  Jira API client (SSRF-safe, Fernet-decrypted tokens)
│   ├── scoring.py         Task scoring (priority multipliers, auto-miss penalty)
│   └── webhook.py         Generic webhook payload parser
│
├── ml/                    Machine learning modules
│   ├── engine.py          Orchestrator: train_all, model status, health, drift detection
│   ├── anomaly.py         Isolation Forest anomaly detection
│   ├── burnout.py         5-signal weighted burnout scoring
│   ├── forecast.py        Random Forest daily + hourly prediction
│   ├── corrector.py       Ridge + LogisticRegression score correction
│   ├── federated.py       FedAvg cross-guild aggregation
│   ├── growth.py          Join/leave prediction + anomalous growth detection
│   ├── features.py        28-dim feature engineering from MessageRecord
│   ├── work_anomaly.py    Per-org work behavior anomaly detection
│   └── work_features.py   10-dim feature extraction from Task + ScoreLog
│
├── services/              Utility services
│   └── slack.py           9 notification types (Slack webhook)
│
├── templates/             Jinja2 HTML templates
│   ├── workspace_*.html   13+ workspace pages
│   ├── dashboard.html     Main guild dashboard
│   ├── guild.html         Guild detail + forecast chart
│   ├── landing.html       Landing page
│   ├── worker.html        Worker detail page
│   └── base.html          Base layout template
│
├── static/                CSS, Chart.js bundle, fonts
├── migrations/            Alembic database migration scripts
├── tests/                 Pytest test suite
│   ├── test_forecast.py   Forecast accuracy, SSRF, legacy filtering (passes)
│   ├── test_observer_and_bot.py  Observer endpoint behavior
│   ├── test_security_and_routes.py  Auth, CSRF, rate limiting
│   └── route_check.py     Route health scanner
│
├── docs/                  Documentation
│   ├── architecture.md    System architecture deep-dive
│   └── screenshots/       Dashboard screenshots
│
├── .env.example           Environment variable template (no real secrets)
├── .gitignore
└── README.md
```

---

## Local Setup

### Prerequisites

- Python 3.14+
- PostgreSQL (optional — SQLite works for development)
- Discord bot token ([Discord Developer Portal](https://discord.com/developers/applications))
- (Optional) Jira instance for Work Engine testing
- (Optional) ngrok for Discord → Flask tunneling

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
# Edit .env with your settings (minimal: SECRET_KEY + DATABASE_URL)

# 4. Run the dashboard
python run_dashboard.py

# 5. Open http://localhost:5000
# Register an organisation at /workspace/register to get started
```

### Running the Bot

```bash
# Set DISCORD_TOKEN in .env, then:
python run_bot.py
```

### Combined Launcher (Windows)

```bash
python start_services.py    # Starts Flask + bot + ngrok as detached processes
```

### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | ✅ | — | Flask session key. Generate: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_URL` | ✅ | `sqlite:///skillsync.db` | PostgreSQL for production |
| `DISCORD_TOKEN` | For bot | — | Discord bot token |
| `DISCORD_CLIENT_ID` | For OAuth | — | Discord OAuth2 client ID |
| `DISCORD_CLIENT_SECRET` | For OAuth | — | Discord OAuth2 client secret |
| `API_KEY` | For bot | — | Shared secret for bot→Flask API calls |
| `JIRA_ENCRYPTION_KEY` | Optional | — | Fernet key. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `SLACK_WEBHOOK_URL` | Optional | — | Slack incoming webhook URL |
| `FLASK_ENV` | Optional | `production` | Set to `development` for debug mode |
| `SESSION_COOKIE_SECURE` | Optional | `False` | Set `True` for HTTPS |

---

## Running Tests

```bash
# Run forecast tests (passing)
python -m pytest tests/test_forecast.py -v

# Full suite (some pre-existing failures unrelated to forecast changes)
python -m pytest tests/
```

---

## Scripts Reference

| Command | What it does |
|---|---|
| `python run_dashboard.py` | Start Flask dashboard on :5000 |
| `python run_bot.py` | Start Discord bot |
| `python app.py` | Legacy — same as run_dashboard.py |
| `python bot.py` | Legacy — same as run_bot.py |
| `python start_services.py` | Launch dashboard + bot + ngrok (detached Windows) |
| `start.bat` | Windows batch — starts all 3 in background windows |
| `stop.bat` | Kills all SkillSync processes |
| `python -m pytest tests/` | Run test suite |

---

## Privacy & Ethics

- **Metadata-first**: Raw Discord message content is never stored. Only counts, rates, and timestamps are recorded.
- **Federated learning**: Raw company data never leaves the internal network. Only anonymized behavioral patterns contribute to the global model.
- **Admin-in-the-loop**: Every automated decision is reviewable and overridable by human administrators. Corrections retrain the model.
- **Token encryption**: Jira API tokens are encrypted at rest using Fernet symmetric encryption. Graceful fallback if key not configured.
- **SSRF protection**: All outbound HTTP requests validate URLs against private IP ranges.
- **Rate limiting**: API endpoints (observer, work engine) are rate-limited per key (300/60s and 200/60s).
- **Academic prototype**: ML scoring outputs are illustrative. Do not use for real personnel decisions without thorough validation.

---

## Limitations

- **Blueprint prefix collision**: `api_bp`, `community_bp`, `observer_bp`, and `work_bp` all register on `/api`. Works currently but is fragile.
- **Single-process rate limiter**: In-memory `defaultdict(list)` — does not work across multiple workers.
- **SQLite lock issues in tests**: All database operations for a test must be inside a single `app.app_context()` block.
- **Forecast accuracy (v2)**: Based on limited resolved runs. Accuracy is honest but low-sample; stabilizes as daily runs accumulate.
- **Anomaly detection is cross-user**: Finds outliers vs. the population, not per-user behavior change over time. Per-user lifetime tracking is a future direction.

---

## References

1. Goodfellow, I., Bengio, Y., & Courville, A. (2016). *Deep Learning*. MIT Press.
2. McMahan, B., et al. (2017). Communication-Efficient Learning of Deep Networks from Decentralized Data. *Proceedings of AISTATS*.
3. Discord Developer Documentation. https://discord.com/developers/docs
4. PostgreSQL Global Development Group. (2024). *PostgreSQL Documentation*. https://www.postgresql.org/docs/
5. Flask Documentation. https://flask.palletsprojects.com/
6. Scikit-learn: Machine Learning in Python. Pedregosa et al., *JMLR* 12, pp. 2825–2830, 2011.

---
