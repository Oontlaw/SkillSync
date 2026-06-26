from sqlalchemy import inspect

from app import app
from database import db

with app.app_context():
    inspector = inspect(db.engine)
    columns = [col["name"] for col in inspector.get_columns("prediction_logs")]
    print("Columns in prediction_logs:", columns)
