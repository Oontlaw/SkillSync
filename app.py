import os

from dotenv import load_dotenv

load_dotenv()

from flask import Flask
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config
from database import db
from routes.api import api_bp
from routes.auth import auth_bp
from routes.community import community_bp
from routes.dashboard import dashboard_bp
from routes.observer import observer_bp
from routes.security import ensure_csrf_token, validate_csrf
from routes.work import work_bp
from routes.workspace import workspace_bp

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.from_object(Config)

db.init_app(app)
migrate = Migrate(
    app, db, directory=os.path.join(os.path.dirname(__file__), "migrations")
)

app.register_blueprint(dashboard_bp)

app.register_blueprint(auth_bp, url_prefix="/auth")
app.register_blueprint(api_bp, url_prefix="/api")
app.register_blueprint(community_bp, url_prefix="/api")
app.register_blueprint(observer_bp, url_prefix="/api")
app.register_blueprint(work_bp, url_prefix="/api")
app.register_blueprint(workspace_bp)


@app.before_request
def csrf_check():
    return validate_csrf()


@app.template_filter("multiply")
def multiply_filter(value, arg):
    try:
        return value * arg
    except:
        return 0


@app.route("/health")
def health():
    """Simple health check — returns JSON. Useful for monitoring and smoke tests."""
    from database import db

    db_ok = False
    try:
        db.session.execute(db.text("SELECT 1"))
        db.session.commit()
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok",
        "database": "ok" if db_ok else "unreachable",
        "version": "academic-prototype",
    }


@app.after_request
def add_security_headers(response):
    # Cache control
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    # Clickjacking protection
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # MIME sniffing protection
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Referrer leakage reduction
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Basic CSP — allows same-origin scripts/styles + CDN for Chart.js + fonts
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'self';"
    )
    return response


@app.context_processor
def inject_csrf_token():
    """Make csrf_token available in every template automatically."""
    return {"csrf_token": ensure_csrf_token()}


with app.app_context():
    if os.getenv("FLASK_SKIP_MIGRATIONS") == "1":
        db.create_all()
        print("[OK] Database tables created (test mode).")
    elif os.path.isdir(migrate.directory):
        from flask_migrate import upgrade

        upgrade()
        print("[OK] Database migrations applied.")
    else:
        db.create_all()
        print("[OK] Database tables created.")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_ENV") == "development")
