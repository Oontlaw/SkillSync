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
from routes.dashboard_v2 import dashboard_v2_bp
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
app.register_blueprint(dashboard_v2_bp, url_prefix="/v2")
app.register_blueprint(auth_bp, url_prefix="/auth")
app.register_blueprint(api_bp, url_prefix="/api")
app.register_blueprint(community_bp, url_prefix="/api")
app.register_blueprint(observer_bp, url_prefix="/api")
app.register_blueprint(work_bp, url_prefix="/api")
app.register_blueprint(workspace_bp)


@app.before_request
def csrf_check():
    return validate_csrf()


@app.template_filter('multiply')
def multiply_filter(value, arg):
    try:
        return value * arg
    except:
        return 0


@app.after_request
def add_no_cache(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
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
