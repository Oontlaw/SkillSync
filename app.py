from flask import Flask
from database import db
from routes.dashboard import dashboard_bp
from routes.api import api_bp
from routes.community import community_bp
from routes.observer import observer_bp
from config import Config

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)

app.register_blueprint(dashboard_bp)
app.register_blueprint(api_bp, url_prefix='/api')
app.register_blueprint(community_bp, url_prefix='/api')
app.register_blueprint(observer_bp, url_prefix='/api')

with app.app_context():
    db.create_all()
    print("✅ Database tables created.")

if __name__ == '__main__':
    app.run(debug=True)
