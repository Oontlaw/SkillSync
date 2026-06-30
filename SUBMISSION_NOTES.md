# Submission Notes — SkillSync

## For the Evaluator

**Project Title:** SkillSync — Smart Workforce Intelligence System

SkillSync is a **dual-engine AI-powered platform** for automated performance
assessment. It demonstrates:

### What the Project Does

1. **Community Engine** — A Discord bot that collects and analyzes behavioral
   patterns: message activity, moderation actions, staff-member interactions,
   rule adherence, and public engagement.

2. **Work Engine** — Integration with enterprise task management (Jira) to
   automatically track, score, and detect anomalies in task assignments.

3. **Intelligent Scoring** — Real-time reputation scoring: points for task
   completion, deductions for anomalies, and bonus points for problem-solving
   beyond assigned duties.

4. **Admin Dashboard & Feedback Loop** — Web interface for HR/Admin personnel
   to review scores, flag anomalies, and issue corrections. Each correction is
   stored as labeled training data and fed back into the ML model for iterative
   improvement.

5. **Data Privacy** — Federated learning keeps raw company data on-premises.
   Only anonymized behavioral patterns are used for cross-server model training.

### Key Technical Achievements

- **Full-stack Python** — Flask web app with 30+ Jinja2 templates, SQLAlchemy
  ORM with 25+ models, Alembic migrations.
- **Discord bot** — Real-time event processing across 6 event types with
  metadata-first privacy filtering.
- **ML pipeline** — Anomaly detection (Isolation Forest), burnout prediction,
  activity forecasting (Random Forest + hourly distribution), federated
  learning, score correction (Ridge regression).
- **Security** — Rate limiting, CSP headers, CSRF protection, SSRF-safe URL
  validation, Fernet token encryption at rest.
- **Testing** — 17+ Pytest tests covering forecast accuracy, route security,
  and observer behavior.

### Technology Stack

| Layer         | Technology                                    |
|---------------|-----------------------------------------------|
| Backend       | Python 3.14, Flask 3.1                        |
| Database      | PostgreSQL + SQLite (dev)                     |
| ORM           | SQLAlchemy 2.0 + Alembic                      |
| ML / AI       | scikit-learn (Random Forest, Isolation Forest) |
| Discord Bot   | discord.py 2.7                                |
| Frontend      | Jinja2, Chart.js, vanilla CSS                 |
| Task Systems  | Jira REST API                                 |

### How to Run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your settings

# Start the dashboard:
python run_dashboard.py
# Open http://localhost:5000

# (Optional) Start the bot:
python run_bot.py
```

### Test Status

All tests pass:
```bash
python -m pytest tests/ -v
```

### References

1. Goodfellow, I., Bengio, Y., & Courville, A. (2016). *Deep Learning*. MIT Press.
2. McMahan, B., et al. (2017). Communication-Efficient Learning of Deep Networks
   from Decentralized Data. *Proceedings of AISTATS*.
3. Discord Developer Documentation. https://discord.com/developers/docs
4. PostgreSQL Documentation. https://www.postgresql.org/docs/
5. Flask Documentation. https://flask.palletsprojects.com/
6. Scikit-learn: Machine Learning in Python. Pedregosa et al., *JMLR* 12,
   pp. 2825–2830, 2011.

### Notes

- Raw Discord message content is never stored. Only metadata is recorded.
- Jira API tokens are encrypted at rest using Fernet.
- This is an academic prototype. ML outputs should not be used for real
  personnel decisions without thorough validation.
