from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Worker(db.Model):
    __tablename__ = 'workers'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    discord_id = db.Column(db.String(50), unique=True, nullable=True)
    role = db.Column(db.String(50), default='worker')  # worker / admin / hr
    score = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    tasks = db.relationship('Task', backref='worker', lazy=True)
    score_logs = db.relationship('ScoreLog', backref='worker', lazy=True)

    def __repr__(self):
        return f'<Worker {self.name} | Score: {self.score}>'


class Task(db.Model):
    __tablename__ = 'tasks'

    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(db.Integer, db.ForeignKey('workers.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), default='pending')  # pending / completed / missed / anomaly
    points_awarded = db.Column(db.Float, default=0.0)
    assigned_at = db.Column(db.DateTime, default=datetime.utcnow)
    due_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    extra_contribution = db.Column(db.Boolean, default=False)  # did worker go beyond task?
    extra_notes = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f'<Task {self.title} | {self.status}>'


class ScoreLog(db.Model):
    __tablename__ = 'score_logs'

    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(db.Integer, db.ForeignKey('workers.id'), nullable=False)
    change = db.Column(db.Float, nullable=False)       # positive or negative
    reason = db.Column(db.String(300), nullable=False)
    source = db.Column(db.String(50), default='system')  # system / admin / discord
    admin_correction = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<ScoreLog worker={self.worker_id} change={self.change}>'


class CommunityEvent(db.Model):
    __tablename__ = 'community_events'

    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(50), nullable=False)
    event_type = db.Column(db.String(100), nullable=False)  # message / moderation / rule_break / helpful
    detail = db.Column(db.Text, nullable=True)
    score_impact = db.Column(db.Float, default=0.0)
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<CommunityEvent {self.event_type} | discord={self.discord_id}>'


class AdminCorrection(db.Model):
    __tablename__ = 'admin_corrections'

    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(db.Integer, db.ForeignKey('workers.id'), nullable=False)
    original_score_change = db.Column(db.Float, nullable=False)
    corrected_score_change = db.Column(db.Float, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    corrected_by = db.Column(db.String(100), nullable=False)  # admin name
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<AdminCorrection worker={self.worker_id} by={self.corrected_by}>'
