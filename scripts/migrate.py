#!/usr/bin/env python
"""
Explicit migration runner.
Usage: python migrate.py
Run this once on startup/deployment instead of auto-running on import.
"""
import os
import sys

os.environ.setdefault('FLASK_ENV', 'production')

from app import app, db
from flask_migrate import upgrade

if __name__ == '__main__':
    with app.app_context():
        try:
            if os.path.isdir(os.path.join(os.path.dirname(__file__), 'migrations')):
                upgrade()
                print("[OK] Database migrations applied.")
                sys.exit(0)
            else:
                db.create_all()
                print("[OK] Database tables created.")
                sys.exit(0)
        except Exception as e:
            print(f"[ERROR] Migration failed: {e}", file=sys.stderr)
            sys.exit(1)