"""
Route health check — verifies all major pages and API endpoints
respond without Internal Server Error.

Usage:
    python tests/route_check.py

Prints status for each route. Exits with code 1 if any 500 error is found.
"""

import os
import sys

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["FLASK_SKIP_MIGRATIONS"] = "1"
os.environ["SECRET_KEY"] = "test-secret-key-for-route-check"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["API_KEY"] = "test-api-key"
os.environ["DISCORD_CLIENT_ID"] = "000000"
os.environ["DISCORD_CLIENT_SECRET"] = "test-secret"
os.environ["DISCORD_TOKEN"] = ""  # bot not needed for route check

from app import app
from database import db


def check_routes():
    """Run through critical routes and report status."""
    client = app.test_client()

    # Routes to check: (method, path, description, expected_statuses)
    # Some routes need auth and will redirect or 401 — that's OK.
    routes = [
        ("GET", "/", "Home / dashboard", {200, 302}),
        ("GET", "/health", "Health endpoint", {200}),
        ("GET", "/api/summary", "API summary", {200, 401, 403}),
        ("GET", "/api/leaderboard", "API leaderboard", {200, 401}),
        ("GET", "/workspace/login", "Workspace login page", {200, 302}),
        ("GET", "/workspace/register", "Workspace register page", {200, 302}),
        ("GET", "/observer/ml/status", "ML status (requires API key)", {200, 401}),
    ]

    all_passed = True
    print("=" * 60)
    print("ROUTE HEALTH CHECK")
    print("=" * 60)

    for method, path, desc, expected in routes:
        try:
            if method == "GET":
                resp = client.get(path, follow_redirects=False)
            elif method == "POST":
                resp = client.post(path, follow_redirects=False)
            else:
                continue

            status = resp.status_code
            status_ok = status in expected or status not in {500, 404}

            if status == 500:
                print(f"  ❌ 500 INTERNAL SERVER ERROR  {desc:40s}  {path}")
                all_passed = False
            elif status == 404:
                print(f"  ⚠  404 Not Found            {desc:40s}  {path}")
            elif status in (302, 301):
                print(f"  ➡  {status} Redirect               {desc:40s}  {path}")
            else:
                print(f"  ✅ {status} OK                     {desc:40s}  {path}")
        except Exception as e:
            print(f"  💥 Exception                  {desc:40s}  {path}: {e}")
            all_passed = False

    print("=" * 60)
    if all_passed:
        print("✅ All routes checked — no 500 errors.")
    else:
        print("❌ Some routes returned 500 errors.")
    print("=" * 60)
    return all_passed


def can_import_modules():
    """Verify all major modules compile without ImportError."""
    modules = [
        "config",
        "database",
        "scoring",
        "app",
        "routes.api",
        "routes.auth",
        "routes.community",
        "routes.dashboard",
        "routes.observer",
        "routes.security",
        "routes.work",
        "routes.workspace",
        "ml.engine",
        "ml.anomaly",
        "ml.burnout",
        "ml.corrector",
        "ml.features",
        "ml.federated",
        "ml.forecast",
        "work_engine.connector_jira",
        "bot_core.config",
        "bot_core.state",
        "bot_core.events_messages",
        "bot_core.events_moderation",
        "bot_core.events_presence",
        "bot_core.events_ready",
    ]
    print("\n" + "=" * 60)
    print("MODULE IMPORT CHECK")
    print("=" * 60)
    all_ok = True
    for mod_name in modules:
        try:
            __import__(mod_name)
            print(f"  ✅ {mod_name}")
        except Exception as e:
            print(f"  ❌ {mod_name}: {e}")
            all_ok = False
    print("=" * 60)
    return all_ok


if __name__ == "__main__":
    with app.app_context():
        db.create_all()

    routes_ok = check_routes()
    imports_ok = can_import_modules()

    print()
    if routes_ok and imports_ok:
        print("✅ ALL CHECKS PASSED")
        sys.exit(0)
    else:
        print("❌ SOME CHECKS FAILED")
        sys.exit(1)
