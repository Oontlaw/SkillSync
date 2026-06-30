# SkillSync — Smart Workforce Intelligence System

SkillSync is a **dual-engine AI-powered platform** for automated performance
assessment. It operates on two fronts: a **Community Engine** that analyzes
behavioral data from platforms like Discord, and a **Work Engine** that integrates
with internal task management systems (Jira, etc.). By combining community
signals with enterprise task data, SkillSync builds a unified, continuously
learning reputation score per individual.

Built as a final-year academic project.

---

## How It Works

```
┌──────────────────────┐     ┌──────────────────────────────┐
│   Community Engine   │     │        Work Engine           │
│   (Discord Bot)      │     │   (Jira / Task Systems)      │
│                      │     │                              │
│ • Message activity   │     │ • Task assignment tracking   │
│ • Moderation actions │     │ • Deadline compliance        │
│ • Rule adherence     │     │ • Completion scoring         │
│ • Staff interactions │     │ • Anomaly detection          │
│ • Public engagement  │     │ • Problem-solving bonuses    │
└──────────┬───────────┘     └──────────────┬───────────────┘
           │                                │
           └──────────┬─────────────────────┘
                      ▼
            ┌──────────────────┐
            │  Scoring Engine  │
            │  (Real-time)     │
            │  + Corrector ML  │
            └────────┬─────────┘
                     │
                     ▼
            ┌──────────────────┐
            │  Admin Dashboard │
            │  (Review /       │
            │   Override)      │
            └────────┬─────────┘
                     │
          (Corrections fed back
           as training data)
                     │
                     ▼
            ┌──────────────────┐
            │  ML Retrain      │
            │  (Closed-loop    │
            │   improvement)   │
            └──────────────────┘
```

- **Community Engine** — A Discord bot that listens to server events (message
  activity, moderation actions, member presence, voice activity) and builds
  behavioral profiles per user without storing raw message content.
- **Work Engine** — Connects to company task systems via API (Jira, Trello, or
  custom integrations) to track assignments, deadlines, completions, and
  spontaneous problem-solving.
- **Scoring Engine** — Awards points for task completion, deducts for anomalies
  or missed tasks, and grants bonus points for work beyond assigned duties. All
  in real time.
- **Admin Dashboard** — Web-based interface for HR/Admin personnel to view
  scores, investigate flagged anomalies, and issue manual overrides. Each
  override is stored as a labeled correction record.
- **Feedback Loop** — Admin corrections are fed back into the ML model as
  training examples. A periodic retraining pipeline fine-tunes scoring thresholds
  and weights, making the system more accurate over time.
- **Data Privacy** — Federated learning ensures raw company data never leaves
  the internal network. Only anonymized behavioral patterns are used for model
  training.

---

## Features

- **Dual-engine architecture** — Community behavioral data + enterprise task data
  combined into unified per-user reputation scores.
- **Automated scoring** — Points for task completion, deductions for anomalies,
  bonuses for problem-solving beyond assigned duties.
- **Admin override & feedback loop** — Every manual correction trains the model;
  accuracy improves iteratively over time.
- **Anomaly detection** — Isolation Forest flags unusual activity changes for
  human review.
- **Burnout risk scoring** — Weighted signal scoring identifies early signs of
  disengagement.
- **Activity forecasting** — Daily volume prediction with 24-hour distribution
  using Random Forest and error-profile correction.
- **Federated learning** — Per-guild models trained locally and aggregated into
  a global model. Company data stays on-premises.
- **Score corrector** — Ridge regression adjusts raw scores against known
  admin-corrected outcomes.
- **Jira connector** — Poll issues, sync work logs, and score tasks. SSRF-safe
  URL validation and token encryption at rest.
- **Metadata-first** — Raw Discord message content is never stored. Only derived
  metrics (counts, rates, timestamps) are persisted.

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
├── bot.py                 Discord bot instance + event wiring
├── bot_commands.py        Slash/prefix commands (Moderation cog)
├── config.py              Configuration (reads from .env)
├── database.py            SQLAlchemy models (25+ tables)
├── scoring.py             Core scoring logic
├── requirements.txt
│
├── run_dashboard.py       Entry: start the Flask dev server
├── run_bot.py             Entry: start the Discord bot
├── start_services.py      Combined launcher (dashboard + bot + ngrok)
├── scripts/               Helper scripts (migrate, ngrok watchdog)
├── archive/               Legacy launchers (start/stop batch files)
│
├── routes/                Flask blueprints (API endpoints + pages)
│   ├── dashboard.py       Main dashboard
│   ├── api.py / community.py  Public API endpoints
│   ├── auth.py            Discord OAuth + workspace auth
│   ├── observer.py        Bot-facing endpoints + ML control
│   ├── workspace.py       Company workspace pages
│   ├── work.py            Work engine API
│   └── security.py        CSRF protection helpers
│
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
│   ├── state.py           In-memory caches (prefix, retrain flags)
│   └── tasks.py           Scheduled background tasks
│
├── ml/                    Machine learning modules
│   ├── anomaly.py         Isolation Forest anomaly detection
│   ├── burnout.py         Burnout risk detection
│   ├── corrector.py       Score corrector (Ridge regression)
│   ├── engine.py          Orchestrator: train_all, status, accuracy
│   ├── features.py        Feature engineering
│   ├── federated.py       Federated learning (round-based)
│   ├── forecast.py        Daily-total forecast + hourly distribution
│   ├── growth.py          Guild/user growth model
│   ├── work_anomaly.py    Work-engine anomaly detection
│   └── work_features.py   Work-engine feature extraction
│
├── work_engine/           Jira / external task integration
│   ├── connector_jira.py  Jira API client (SSRF-safe)
│   ├── scoring.py         Task scoring
│   └── webhook.py         Webhook receiver
│
├── services/              Utility services
│   └── slack.py           Slack webhook notifications
│
├── templates/             Jinja2 HTML templates
│   ├── dashboard.html     Main dashboard
│   ├── guild.html         Guild detail + forecast chart
│   ├── landing.html       Landing page
│   ├── worker.html        Worker detail
│   ├── workspace_*.html   10+ workspace pages
│   └── base.html          Base layout
│
├── static/                Static assets
│   ├── dashboard.css, workspace.css, landing_v2.css
│   ├── chart.umd.min.js
│   └── fonts/
│
├── migrations/            Alembic database migrations (30+ versions)
├── tests/                 Pytest test suite
├── docs/                  Project documentation
│
├── .env.example           Environment variable template
├── .gitignore
├── README.md
└── SUBMISSION_NOTES.md
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
# Edit .env with your Discord bot token and other settings

# 4. Run the dashboard
python run_dashboard.py

# 5. (Optional) Run the bot
python run_bot.py

# 6. Open http://localhost:5000
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

- **Metadata-first**: Raw Discord message content is never stored by default.
  Only message counts, rates, and metadata (timestamps, channel IDs) are recorded.
- **Federated learning**: Raw company data never leaves the internal network.
  Only anonymized behavioral patterns contribute to the global model.
- **Admin-in-the-loop**: Every automated decision is reviewable and overridable
  by human administrators. The AI is an assistant, not a replacement.
- **Token encryption**: Jira API tokens are encrypted at rest using Fernet
  symmetric encryption.
- **Academic prototype**: ML scoring outputs are illustrative. Do not use for
  real personnel or moderation decisions without thorough validation.

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
