"""
SkillSync Dashboard — Flask entry point.

Usage:
    python run_dashboard.py

Runs the Flask development server on http://localhost:5000.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# ── Import the Flask app ──
# The app variable is created in app.py by importing config, database,
# and all route blueprints.  We import it here so the top-level entry
# point is clean and readable.
from app import app

if __name__ == "__main__":
    debug = os.getenv("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=5000, debug=debug)
