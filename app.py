import os
from dotenv import load_dotenv
load_dotenv()

from flask import Flask
from flask_migrate import Migrate
from database import db
from routes.dashboard import dashboard_bp
from routes.auth import auth_bp
from routes.api import api_bp
from routes.community import community_bp
from routes.observer import observer_bp
from config import Config

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
migrate = Migrate(app, db, directory=os.path.join(os.path.dirname(__file__), 'migrations'))

app.register_blueprint(dashboard_bp)
app.register_blueprint(auth_bp, url_prefix='/auth')
app.register_blueprint(api_bp, url_prefix='/api')
app.register_blueprint(community_bp, url_prefix='/api')
app.register_blueprint(observer_bp, url_prefix='/api')

with app.app_context():
    if os.path.isdir(migrate.directory):
        from flask_migrate import upgrade
        upgrade()
        print("[OK] Database migrations applied.")
    else:
        db.create_all()
        print("[OK] Database tables created.")

if __name__ == '__main__':
    app.run(debug=os.getenv('FLASK_ENV') == 'development')
