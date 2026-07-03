# SkillSync — Architecture

## Overview

SkillSync is a **dual-engine AI-powered workforce intelligence platform**. Two engines
feed into a shared ML pipeline, and their combined output is consumed by a private
company dashboard called the **Workspace**.

The core idea: bridge **community behavioral signals** (from platforms like Discord)
with **enterprise task data** (from systems like Jira) to build a unified, continuously
learning reputation score per individual — without storing raw message content or
exposing company data externally.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         INPUT LAYER                                 │
│                                                                     │
│  ┌─────────────────────────┐      ┌──────────────────────────────┐  │
│  │    COMMUNITY ENGINE     │      │         WORK ENGINE          │  │
│  │    (Discord Bot)        │      │   (Jira / Task Systems)      │  │
│  │                         │      │                              │  │
│  │  bot_core/ event loop   │      │  connector_jira.py polls     │  │
│  │  30s flush → observer   │      │  webhook.py receives events  │  │
│  └───────────┬─────────────┘      └──────────────┬───────────────┘  │
└──────────────┼───────────────────────────────────┼──────────────────┘
               │                                   │
               ▼                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      EMBEDDING LAYER                                │
│                                                                     │
│  Raw events → Numerical feature vectors                             │
│                                                                     │
│  • MessageRecord → 28-dim vector (hourly profile + message stats)   │
│  • Task + ScoreLog → 10-dim vector (completion, priority, streak)   │
│  • Baselines: 90d historical + 7d current drift                     │
│                                                                     │
│  All stored in PostgreSQL / SQLite. No raw content preserved.       │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      ML PIPELINE (7 models)                         │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────┐  │
│  │   Anomaly    │  │   Burnout    │  │  Forecast    │  │Corrector│  │
│  │ IsolationForest│ Weighted(5sig)│  │ RandomForest │  │ Ridge+ │  │
│  │  32-dim feat │  │ 90d baseline │  │ 30d train    │  │Logistic│  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └───┬────┘  │
│         │                 │                 │               │       │
│  ┌──────┴─────────────────┴─────────────────┴───────────────┴────┐  │
│  │              Federated Learning (FedAvg)                       │  │
│  │         Cross-guild pattern aggregation, privacy-preserving    │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────┐  ┌──────────────────┐                            │
│  │   Growth     │  │  Work Anomaly    │                            │
│  │ RandomForest │  │ IsolationForest  │                            │
│  │ 90d train    │  │ per-org, 10-dim  │                            │
│  └──────────────┘  └──────────────────┘                            │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      OUTPUT LAYER                                   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                        WORKSPACE                             │   │
│  │               (Private Company Dashboard)                    │   │
│  │                                                              │   │
│  │  routes/workspace.py — 14 pages                              │   │
│  │  Auth: email/password, 3 roles (admin/hr/member)             │   │
│  │  CSRF-protected, rate-limited login (5/15min)                │   │
│  │                                                              │   │
│  │  Pages: Dashboard, Workers, Tasks, Leaderboard,              │   │
│  │  Team Health, Overrides, Identities, Members, Settings       │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                             │                                      │
│                             ▼                                      │
│              Admin Correction → ML Retrain (closed-loop)           │
└─────────────────────────────────────────────────────────────────────┘
```

---

## The Two Engines

### 1. Community Engine (Discord Bot)

**Purpose:** Collect behavioral signals from Discord communities.

The bot runs as a single process using `discord.py 2.7`. It listens to guild events
and buffers them in-memory for 30 seconds before flushing to the Flask observer API.

| Event | Handler | Buffered | Stored As |
|---|---|---|---|
| Message sent | `events_messages.py` | Yes (30s) | `MessageRecord` (count, length, channel, hour) |
| Message edit/delete | `events_messages.py` | Yes | Metadata update |
| Member ban/unban | `events_moderation.py` | Yes | `PendingBan` |
| Member timeout | `events_moderation.py` | Yes | `PendingTimeout` |
| Role change | `events_moderation.py` | Yes | `RoleChangeLog` |
| Presence update | `events_presence.py` | Yes | `GuildMember` (is_online) |
| Member join/leave | `events_presence.py` | Yes | `MemberJoinLeave` |
| Voice state | `events_presence.py` | Yes | `VoiceActivity` |

**Background tasks** (all in `tasks.py`):

| Task | Interval | What it does |
|---|---|---|
| Flush buffers | 30s | Send buffered events to observer API |
| Reverse actions | 1h | Check/expire pending timeouts and bans |
| Run ML forecast | 1h | `predict_next_24h(guild_id, log_prediction=True)` |
| Poll Jira | 1h | `poll_and_sync_for_org()` per configured org |
| Cleanup stale data | 6h | Prune old records |
| Rescan guilds | 6h | Update guild info, roles, channels |
| Check overdue tasks | 6h | Auto-miss penalty for expired tasks |
| Health digest | 168h (1w) | Send weekly Slack summary |

### 2. Work Engine (Jira Integration)

**Purpose:** Synchronize enterprise task data into the workspace.

The Work Engine connects to Jira via REST API. It polls configured projects per
organisation, maps Jira issues to linked workers (via `WorkerIdentity`), and creates
`Task` + `ScoreLog` entries automatically.

**Security:**
- All Jira URLs validated by `_validate_jira_url()` before any HTTP request
- Blocks private IPs, loopback, link-local, AWS/GCP metadata endpoints
- API tokens encrypted at rest using Fernet symmetric encryption
- Graceful fallback if `JIRA_ENCRYPTION_KEY` is not set (dev mode)

---

## The ML Pipeline

### Model Summary

| Module | Algorithm | Input Features | Output | Retrain Trigger |
|---|---|---|---|---|
| `anomaly.py` | Isolation Forest | 32-dim (message profile + hourly) | Per-user anomaly score + severity | `train_all()` or manual |
| `burnout.py` | Weighted signals | 5 signals (volume, gaps, latency, streak, engagement) | Burnout score 0-100 | `train_all()` |
| `forecast.py` | Random Forest + hourly profile | 30d hourly history + day-of-week | 24h prediction + daily total | Hourly via bot task |
| `corrector.py` | Ridge + LogisticRegression | Admin correction history | Predicted score adjustment | `consume_retrain_request()` |
| `federated.py` | LogisticRegression (FedAvg) | Per-guild off-hours features | Cross-guild classification | Round-based aggregation |
| `growth.py` | Random Forest | 90d join/leave rates | 7d forecast | `train_all()` |
| `work_anomaly.py` | Isolation Forest | 10-dim work features | Per-worker anomaly | `train_all()` |

### The Self-Correction Loop (Forecast)

The forecast model is the most actively self-correcting component:

```
1. predict_next_24h(guild_id, log_prediction=True)
   → logs 24 PredictionLog rows (v2: resolution_version=2, actual_granularity=hourly)
   → applies error-profile correction (capped ±30% per hour to prevent overfit)

2. After 25h: resolve_outcomes() is called
   → queries MessageRecord for actual hourly counts in that window
   → stores actual_value per hour (NOT daily total in hourly rows)
   → marks was_correct per hour and per day
   → stamps resolution_version=2 and stores metadata (daily totals, signed errors)

3. On next retrain: _build_error_profile(guild_id)
   → computes per-hour bias and MAE from v2 resolved logs only
   → legacy v1 logs (stored daily totals in hourly rows) are excluded

4. Next prediction: applies bias correction from step 3
   → if consistently over-predicting hour 14 → reduce prediction for that hour
   → if consistently under-predicting hour 20 → increase prediction
   → correction is conservative (capped) to prevent one bad day from overfitting
```

### Accuracy Metrics

| Metric | Calculation | Tolerance |
|---|---|---|
| Daily volume accuracy | `abs(predicted_daily - actual_daily) <= max(actual * 0.15, 25)` | Adaptive |
| Hourly accuracy | `abs(predicted_hour - actual_hour) <= max(actual_hour * 0.25, 10)` | Per-hour adaptive |
| MAE (daily) | Mean absolute error across all resolved runs | — |
| MAE (hourly) | Mean absolute error across all resolved hours | — |

All metrics filter to v2 logs only (`resolution_version=2, actual_granularity=hourly`).
Legacy v1 logs are excluded from hourly accuracy (they stored daily totals in hourly rows).

---

## The Workspace (Output Layer)

The workspace is the **primary interface** of SkillSync. It is a private company dashboard
completely separate from Discord OAuth — organisations register independently.

### Key Design

- **14 pages** under `/workspace/` route prefix (`routes/workspace.py`)
- **Auth**: email/password registration, bcrypt password hashing, rate-limited login
- **Roles**: admin (full control), hr (review + correct), member (read-only)
- **Security**: CSRF tokens on all mutation endpoints, session isolation per org

### Data Flow Inside Workspace

```
Org registers → Admin invites members → Workers linked via Identities
                                              │
                    ┌─────────────────────────┼─────────────────────────┐
                    │                         │                         │
                    ▼                         ▼                         ▼
            Community signals           Jira tasks              Admin corrections
            (via identity link)         (auto-created)          (manual overrides)
                    │                         │                         │
                    └─────────────────────────┼─────────────────────────┘
                                              │
                                              ▼
                                      Scoring Engine
                                    (points + ML corrector)
                                              │
                                              ▼
                                    Dashboard / Overrides
                                              │
                                              ▼
                                    Admin reviews + corrects
                                              │
                                              ▼
                                    ML retrains (closed loop)
```

---

## Database Schema

SkillSync uses 28 SQLAlchemy models. Key models:

| Model | Stores | Estimated Rows (production) |
|---|---|---|
| `MessageRecord` | Per-message metadata (no content) | 52,400+ |
| `Worker` | Worker profiles, scores | 1,000+ |
| `Organisation` | Company/workspace accounts | 1-5 |
| `OrgMember` | Workspace user accounts | 5-20 |
| `WorkerIdentity` | Discord ↔ Jira identity links | 1,000+ |
| `Task` | Assigned work items | 100+ |
| `ScoreLog` | Score change history | 5,000+ |
| `AdminCorrection` | Manual override history | 15+ |
| `PredictionLog` | ML prediction + outcome history | 2,800+ v2 rows |
| `BehavioralAnomaly` | Flagged anomalies | 200+ |
| `BurnoutRisk` | Burnout predictions | 50+ |
| `GuildInfo` | Discord server metadata | 4 |
| `GuildMember` | Per-member guild state | 14,000+ |
| `UserBehaviorBaseline` | Per-user historical profiles | 135+ |

---

## Security Architecture

| Layer | Protection |
|---|---|
| **Network** | SSRF validation on all outbound requests (Jira) |
| **Storage** | Fernet-encrypted Jira API tokens at rest |
| **API** | Bearer token auth on observer + work engine endpoints |
| **API rate limits** | 300 req/60s (observer), 200 req/60s (work engine) |
| **Web** | CSP headers, X-Frame-Options, X-Content-Type-Options, Referrer-Policy |
| **Auth** | bcrypt password hashing, rate-limited login (5/15min), session isolation |
| **CSRF** | Token-based CSRF protection on all mutation endpoints |
| **Request size** | 8MB maximum payload (MAX_CONTENT_LENGTH) |

---

## Deployment

SkillSync is designed for local server deployment. All data stays on-premises.

### Current Deployment

- **Flask dashboard**: Running on port 5000
- **Discord bot**: Connected to 4 Discord servers (largest: ~13,000 members)
- **Database**: SQLite (dev), PostgreSQL-ready for production
- **ngrok tunnel**: Optional — used for bot → Flask communication during dev

### Entry Points

| Command | What it starts |
|---|---|
| `python run_dashboard.py` | Flask dev server on :5000 |
| `python run_bot.py` | Discord bot (requires DISCORD_TOKEN in .env) |
| `python start_services.py` | Flask + bot + ngrok (detached Windows processes) |
| `start.bat` | Batch launcher for all 3 services |

---

## Key Design Decisions

1. **Workspace-first architecture** — The workspace is the primary interface. The Discord bot is a data source, not a standalone product.
2. **Dual-engine, unified ML** — Both Community and Work engines feed the same ML pipeline. No separate training per source.
3. **Metadata-first tracking** — Raw message content never stored. Only counts, rates, timestamps.
4. **Admin-in-the-loop** — Every ML decision is reviewable and overridable. Corrections become training data.
5. **Closed-loop learning** — Admin corrections → retrain → better predictions → fewer corrections needed.
6. **Privacy by design** — Federated learning shares only patterns, not raw data. SSRF validation on all outbound calls.
7. **Honest metrics** — v2-only accuracy reporting with clear sample counts. No mixing of granularities.
8. **Academic prototype** — Designed as a final-year project. ML outputs should not be used for real personnel decisions.
