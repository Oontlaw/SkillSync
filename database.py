import base64
import os as _os
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash


def _get_fernet():
    """
    Return a Fernet instance using JIRA_ENCRYPTION_KEY from env.
    If the key is absent or invalid, returns None — token stored/returned as-is
    (graceful degradation for dev environments without the key set).
    """
    key = _os.getenv("JIRA_ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        return None


def encrypt_token(plaintext: str) -> str:
    """Encrypt a Jira API token. Returns ciphertext or plaintext if no key set."""
    if not plaintext:
        return plaintext
    f = _get_fernet()
    if not f:
        return plaintext  # no key configured — store as-is (dev mode)
    return f.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a Jira API token. Returns plaintext or ciphertext if no key set."""
    if not ciphertext:
        return ciphertext
    f = _get_fernet()
    if not f:
        return ciphertext  # no key configured — return as-is (dev mode)
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return ciphertext  # already plaintext (migration case) — return as-is


db = SQLAlchemy()


class Worker(db.Model):
    __tablename__ = "workers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    discord_id = db.Column(db.String(50), unique=True, nullable=True, index=True)
    role = db.Column(db.String(50), default="worker")  # worker / admin / hr
    score = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    tasks = db.relationship("Task", backref="worker", lazy=True)
    score_logs = db.relationship("ScoreLog", backref="worker", lazy=True)

    def __repr__(self):
        return f"<Worker {self.name} | Score: {self.score}>"


class Organisation(db.Model):
    __tablename__ = "organisations"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email_domain = db.Column(db.String(150), nullable=True)
    api_key = db.Column(db.String(128), unique=True, nullable=False, index=True)
    plan = db.Column(db.String(30), default="free")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)

    share_feature_vectors = db.Column(db.Boolean, default=True)
    share_anomaly_types = db.Column(db.Boolean, default=True)
    store_task_content = db.Column(db.Boolean, default=False)

    jira_url = db.Column(db.String(256), nullable=True)
    jira_email = db.Column(db.String(150), nullable=True)
    jira_api_token = db.Column(db.Text, nullable=True)
    jira_project = db.Column(db.String(50), nullable=True)

    members = db.relationship("OrgMember", backref="organisation", lazy=True)
    identities = db.relationship("WorkerIdentity", backref="organisation", lazy=True)

    def __repr__(self):
        return f"<Organisation {self.slug}>"


class OrgMember(db.Model):
    __tablename__ = "org_members"

    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(
        db.Integer, db.ForeignKey("organisations.id"), nullable=False, index=True
    )
    email = db.Column(db.String(150), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    role = db.Column(db.String(30), default="member")
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)

    __table_args__ = (
        db.UniqueConstraint("org_id", "email", name="uq_org_member_email"),
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<OrgMember {self.email} @ org={self.org_id}>"


class LoginAttempt(db.Model):
    """Tracks failed login attempts per email for brute-force protection.
    Cleared automatically after LOCKOUT_WINDOW_MINUTES."""

    __tablename__ = "login_attempts"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), nullable=False, index=True)
    ip_address = db.Column(db.String(45), nullable=True)
    attempted_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    success = db.Column(db.Boolean, default=False)

    __table_args__ = (
        db.Index("ix_login_attempts_email_time", "email", "attempted_at"),
    )


class WorkerIdentity(db.Model):
    __tablename__ = "worker_identities"

    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(
        db.Integer, db.ForeignKey("workers.id"), nullable=True, index=True
    )
    org_id = db.Column(
        db.Integer, db.ForeignKey("organisations.id"), nullable=False, index=True
    )

    discord_id = db.Column(db.String(50), nullable=True, index=True)
    org_employee_id = db.Column(db.String(100), nullable=True)
    jira_account_id = db.Column(db.String(100), nullable=True)
    display_name = db.Column(db.String(150), nullable=True)
    email = db.Column(db.String(150), nullable=True)
    member_email = db.Column(db.String(150), nullable=True, index=True)

    linked_at = db.Column(db.DateTime, default=datetime.utcnow)
    linked_by = db.Column(db.String(150), nullable=True)
    is_active = db.Column(db.Boolean, default=True)

    consent_community_prior = db.Column(db.Boolean, default=True)
    consent_federated = db.Column(db.Boolean, default=True)

    __table_args__ = (
        db.UniqueConstraint("org_id", "discord_id", name="uq_identity_org_discord"),
        db.UniqueConstraint(
            "org_id", "org_employee_id", name="uq_identity_org_employee"
        ),
    )

    def __repr__(self):
        return f"<WorkerIdentity discord={self.discord_id} org={self.org_id}>"


class Task(db.Model):
    __tablename__ = "tasks"

    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(
        db.Integer, db.ForeignKey("workers.id"), nullable=False, index=True
    )
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(
        db.String(30), default="pending"
    )  # pending / completed / missed / anomaly
    points_awarded = db.Column(db.Float, default=0.0)
    assigned_at = db.Column(db.DateTime, default=datetime.utcnow)
    due_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    extra_contribution = db.Column(db.Boolean, default=False)
    extra_notes = db.Column(db.Text, nullable=True)
    # Work Engine fields
    source = db.Column(db.String(30), nullable=True)  # jira / trello / webhook
    external_id = db.Column(db.String(100), nullable=True, index=True)
    external_url = db.Column(db.String(500), nullable=True)
    priority = db.Column(
        db.String(20), default="medium"
    )  # low / medium / high / critical

    def __repr__(self):
        return f"<Task {self.title} | {self.status}>"


class ScoreLog(db.Model):
    __tablename__ = "score_logs"

    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(
        db.Integer, db.ForeignKey("workers.id"), nullable=False, index=True
    )
    change = db.Column(db.Float, nullable=False)  # positive or negative
    reason = db.Column(db.String(300), nullable=False)
    source = db.Column(db.String(50), default="system")  # system / admin / discord
    admin_correction = db.Column(db.Boolean, default=False)
    guild_id = db.Column(db.String(50), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Auto-judgment review tracking
    reviewed = db.Column(db.Boolean, default=False)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by = db.Column(db.String(150), nullable=True)

    def __repr__(self):
        return f"<ScoreLog worker={self.worker_id} change={self.change}>"


class CommunityEvent(db.Model):
    __tablename__ = "community_events"

    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(50), nullable=False, index=True)
    guild_id = db.Column(db.String(50), nullable=True, index=True)
    event_type = db.Column(
        db.String(100), nullable=False
    )  # message / moderation / rule_break / helpful
    detail = db.Column(db.Text, nullable=True)
    score_impact = db.Column(db.Float, default=0.0)
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<CommunityEvent {self.event_type} | discord={self.discord_id}>"


class AdminCorrection(db.Model):
    __tablename__ = "admin_corrections"

    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(db.Integer, db.ForeignKey("workers.id"), nullable=False)
    original_score_change = db.Column(db.Float, nullable=False)
    corrected_score_change = db.Column(db.Float, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    corrected_by = db.Column(db.String(100), nullable=False)  # admin name
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<AdminCorrection worker={self.worker_id} by={self.corrected_by}>"


class MessageRecord(db.Model):
    __tablename__ = "message_records"

    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(50), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    channel_name = db.Column(db.String(100), nullable=True)
    is_public_channel = db.Column(db.Boolean, default=True)
    message_length = db.Column(db.Integer, default=0)
    message_content = db.Column(
        db.Text, nullable=True
    )  # Only stored for public channels
    hour_of_day = db.Column(db.Integer, nullable=True)  # 0-23
    day_of_week = db.Column(db.Integer, nullable=True)  # 0=Mon, 6=Sun
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<MessageRecord {self.discord_id} | len={self.message_length}>"


class UserBehaviorBaseline(db.Model):
    """Long-term behavioral baseline per user — accumulates forever.
    Updated weekly via engine.train_all(). Never deleted.
    Used by anomaly detection to compare short-term vs long-term behavior."""

    __tablename__ = "user_behavior_baselines"

    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(50), nullable=False, index=True)
    guild_id = db.Column(db.String(50), nullable=True, index=True)

    # Long-term hourly profile (24 values, JSON list of floats, normalized)
    hourly_profile_90d = db.Column(db.JSON, nullable=True)

    # Long-term message stats
    mean_daily_msgs_90d = db.Column(db.Float, nullable=True)
    std_daily_msgs_90d = db.Column(db.Float, nullable=True)
    mean_msg_length_90d = db.Column(db.Float, nullable=True)
    off_hours_ratio_90d = db.Column(db.Float, nullable=True)

    # Short-term stats (last 7 days) — updated each training run
    mean_daily_msgs_7d = db.Column(db.Float, nullable=True)
    off_hours_ratio_7d = db.Column(db.Float, nullable=True)

    # Drift signals — computed by comparing 7d vs 90d
    volume_drift = db.Column(
        db.Float, nullable=True
    )  # (7d_mean - 90d_mean) / max(90d_std, 1)
    pattern_drift = db.Column(
        db.Float, nullable=True
    )  # cosine distance between hourly profiles
    is_drifting = db.Column(db.Boolean, default=False)  # True if either drift > 2.0

    # Cross-model signals — written by other models, read by anomaly + burnout
    recent_anomaly_count = db.Column(db.Integer, default=0)  # from anomaly.py
    recent_burnout_score = db.Column(db.Float, nullable=True)  # from burnout.py
    forecast_error_mean = db.Column(db.Float, nullable=True)  # from forecast.py

    # Confidence — grows as data accumulates
    total_msgs_seen = db.Column(db.Integer, default=0)
    baseline_confidence = db.Column(db.Float, default=0.0)  # 0.0-1.0, grows with data

    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("discord_id", "guild_id", name="uq_user_baseline_guild"),
    )

    def __repr__(self):
        drift = "DRIFTING" if self.is_drifting else "stable"
        return f"<UserBehaviorBaseline {self.discord_id} | {drift} | conf={self.baseline_confidence}>"


class GuildActivityBaseline(db.Model):
    """Long-term hourly activity baseline per guild.
    Used by forecast.py to add guild-specific features."""

    __tablename__ = "guild_activity_baselines"

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, unique=True, index=True)

    # 24-value JSON list — mean message count per hour over all history
    hourly_mean = db.Column(db.JSON, nullable=True)
    # 24-value JSON list — std dev per hour
    hourly_std = db.Column(db.JSON, nullable=True)
    # Peak hours (top 6 hours by mean activity), JSON list of ints
    peak_hours = db.Column(db.JSON, nullable=True)
    # Total messages seen (used for confidence weighting)
    total_msgs_seen = db.Column(db.Integer, default=0)
    # Days of data seen
    days_of_history = db.Column(db.Integer, default=0)

    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<GuildActivityBaseline {self.guild_id} | {self.days_of_history}d | {self.total_msgs_seen} msgs>"


class GuildInfo(db.Model):
    """Stores scanned guild/server information."""

    __tablename__ = "guild_info"

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    owner_id = db.Column(db.String(50), nullable=True)
    owner_name = db.Column(db.String(100), nullable=True)
    member_count = db.Column(db.Integer, default=0)
    online_count = db.Column(db.Integer, default=0)
    staff_count = db.Column(db.Integer, default=0)
    bot_count = db.Column(db.Integer, default=0)
    role_count = db.Column(db.Integer, default=0)
    prefix = db.Column(db.Text, default='["!ss "]')
    store_content = db.Column(db.Boolean, default=False)
    scanned_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class GuildRole(db.Model):
    """Stores role information per guild, including mod-relevant permissions."""

    __tablename__ = "guild_roles"

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    role_id = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    position = db.Column(db.Integer, default=0)
    color = db.Column(db.String(20), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    can_ban = db.Column(db.Boolean, default=False)
    can_kick = db.Column(db.Boolean, default=False)
    can_manage_messages = db.Column(db.Boolean, default=False)
    can_manage_guild = db.Column(db.Boolean, default=False)
    can_manage_roles = db.Column(db.Boolean, default=False)
    is_mod = db.Column(db.Boolean, default=False)
    is_manually_set = db.Column(db.Boolean, default=False)
    member_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def is_mod_role(self):
        """Auto-determine if this role grants moderation power."""
        return any(
            [
                self.is_admin,
                self.can_ban,
                self.can_kick,
                self.can_manage_guild,
                self.can_manage_roles,
            ]
        )


class GuildMember(db.Model):
    """Stores member information per guild with staff flags and presence tracking."""

    __tablename__ = "guild_members"

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    member_id = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    display_name = db.Column(db.String(100), nullable=True)
    joined_at = db.Column(db.DateTime, nullable=True)
    is_bot = db.Column(db.Boolean, default=False)
    is_owner = db.Column(db.Boolean, default=False)
    is_staff = db.Column(db.Boolean, default=False)
    is_manually_set = db.Column(db.Boolean, default=False)
    role_ids = db.Column(db.Text, nullable=True)
    top_role_position = db.Column(db.Integer, default=0)
    total_messages = db.Column(db.Integer, default=0)
    is_online = db.Column(db.Boolean, default=False)
    last_seen_online = db.Column(db.DateTime, nullable=True)
    last_message_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default="offline")
    activity_name = db.Column(db.String(100), nullable=True)
    activity_type = db.Column(db.String(20), nullable=True)
    # profiling consent — opt-out model (default True, existing graphs stay
    # intact); opted-out members produce no new profiling rows
    consent_optin = db.Column(db.Boolean, default=True, nullable=True)
    consent_updated_at = db.Column(db.DateTime, nullable=True)
    consent_source = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class GuildChannel(db.Model):
    """Stores channel information per guild."""

    __tablename__ = "guild_channels"

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    channel_id = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    topic = db.Column(db.Text, nullable=True)
    channel_type = db.Column(
        db.String(20), nullable=False, default="text"
    )  # text, voice, announcement, forum
    category = db.Column(db.String(100), nullable=True)
    position = db.Column(db.Integer, default=0)
    is_public = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class MentionRecord(db.Model):
    __tablename__ = "mention_records"

    id = db.Column(db.Integer, primary_key=True)
    mentioner_id = db.Column(db.String(50), nullable=False, index=True)
    mentioner_name = db.Column(db.String(100), nullable=True)
    mentioned_id = db.Column(db.String(50), nullable=False, index=True)
    mentioned_name = db.Column(db.String(100), nullable=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    channel_name = db.Column(db.String(100), nullable=True)
    reply_time_seconds = db.Column(db.Float, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<MentionRecord {self.mentioner_id} -> {self.mentioned_id} reply={self.reply_time_seconds}>"


class BehavioralAnomaly(db.Model):
    __tablename__ = "behavioral_anomalies"

    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(50), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=True)
    guild_id = db.Column(db.String(50), nullable=True)
    anomaly_type = db.Column(db.String(50), nullable=False)
    severity = db.Column(db.Float, default=0.0)
    details = db.Column(db.Text, nullable=True)
    source = db.Column(db.String(30), default="discord", index=True)
    detected_at = db.Column(db.DateTime, default=datetime.utcnow)
    cleared_at = db.Column(db.DateTime, nullable=True)
    feedback = db.Column(db.String(30), nullable=True, index=True)
    feedback_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<BehavioralAnomaly {self.anomaly_type} | {self.discord_id} | sev={self.severity}>"


class PredictionLog(db.Model):
    __tablename__ = "prediction_logs"

    id = db.Column(db.Integer, primary_key=True)
    model_name = db.Column(db.String(50), nullable=False, index=True)
    prediction_value = db.Column(db.Float, nullable=True)
    actual_value = db.Column(db.Float, nullable=True)
    error_magnitude = db.Column(db.Float, nullable=True)
    error_signed = db.Column(db.Float, nullable=True)
    features_json = db.Column(db.Text, nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)
    confidence = db.Column(db.Float, nullable=True)
    prediction_time = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    outcome_time = db.Column(db.DateTime, nullable=True)
    was_correct = db.Column(db.Boolean, nullable=True)
    hour_error_history = db.Column(db.JSON, nullable=True)

    def __repr__(self):
        resolved = "resolved" if self.was_correct is not None else "pending"
        return f"<PredictionLog {self.model_name} | {resolved}>"


class AutoModRule(db.Model):
    __tablename__ = "automod_rules"

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False)
    rule_id = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    creator_id = db.Column(db.String(50), nullable=True)
    creator_name = db.Column(db.String(100), nullable=True)
    trigger_type = db.Column(db.String(50), nullable=False)
    trigger_text = db.Column(db.Text, nullable=True)
    action_type = db.Column(db.String(50), nullable=False)
    enabled = db.Column(db.Boolean, default=True)
    exempt_roles = db.Column(db.Text, nullable=True)
    exempt_channels = db.Column(db.Text, nullable=True)
    alert_channel_id = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<AutoModRule {self.name} | {self.trigger_type} -> {self.action_type}>"


class VoiceActivity(db.Model):
    __tablename__ = "voice_activity"

    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(50), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    guild_name = db.Column(db.String(100), nullable=True)
    channel_name = db.Column(db.String(100), nullable=True)
    duration_seconds = db.Column(db.Float, default=0.0)
    hour_of_day = db.Column(db.Integer, nullable=True)
    day_of_week = db.Column(db.Integer, nullable=True)
    joined_at = db.Column(db.DateTime, nullable=True)
    left_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<VoiceActivity {self.discord_id} | {self.duration_seconds}s in {self.channel_name}>"


class PingJoinEvent(db.Model):
    """Records when a moderator pings @everyone and new members join within 20 min."""

    __tablename__ = "ping_join_events"

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    guild_name = db.Column(db.String(100), nullable=True)
    moderator_id = db.Column(db.String(50), nullable=False)
    moderator_name = db.Column(db.String(100), nullable=True)
    channel = db.Column(db.String(100), nullable=True)
    new_members = db.Column(db.Integer, default=0)
    joiners = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PingJoinEvent {self.moderator_name} | +{self.new_members} in {self.guild_name}>"


class BurnoutRisk(db.Model):
    __tablename__ = "burnout_risks"

    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(
        db.Integer, db.ForeignKey("workers.id"), nullable=False, index=True
    )
    discord_id = db.Column(db.String(50), nullable=False)
    guild_id = db.Column(db.String(50), nullable=True, index=True)
    name = db.Column(db.String(100), nullable=True)
    score = db.Column(db.Float, default=0.0, index=True)
    anomaly_freq = db.Column(db.Float, default=0.0)
    volume_volatility = db.Column(db.Float, default=0.0)
    reversal_rate = db.Column(db.Float, default=0.0)
    voice_creep = db.Column(db.Float, default=0.0)
    signals = db.Column(db.Text, nullable=True)
    detected_at = db.Column(db.DateTime, default=datetime.utcnow)
    feedback = db.Column(db.String(30), nullable=True, index=True)
    feedback_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<BurnoutRisk {self.name} | score={self.score}>"


class AutoModTrigger(db.Model):
    __tablename__ = "automod_triggers"

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    rule_id = db.Column(db.String(50), nullable=True)
    rule_name = db.Column(db.String(200), nullable=True)
    user_id = db.Column(db.String(50), nullable=True, index=True)
    user_name = db.Column(db.String(100), nullable=True)
    channel_id = db.Column(db.String(50), nullable=True)
    channel_name = db.Column(db.String(100), nullable=True)
    content_snippet = db.Column(db.Text, nullable=True)
    action_taken = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<AutoModTrigger {self.rule_name} -> {self.user_name} in #{self.channel_name}>"


class PendingBan(db.Model):
    __tablename__ = "pending_bans"

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    user_id = db.Column(db.String(50), nullable=False)
    banner_id = db.Column(db.String(50), nullable=False)
    banner_name = db.Column(db.String(100), nullable=False)
    user_name = db.Column(db.String(100), nullable=False)
    reason = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PendingBan {self.user_name} by {self.banner_name}>"


class PendingTimeout(db.Model):
    __tablename__ = "pending_timeouts"

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    user_id = db.Column(db.String(50), nullable=False)
    mod_id = db.Column(db.String(50), nullable=False)
    mod_name = db.Column(db.String(100), nullable=False)
    until = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PendingTimeout {self.mod_name} until {self.until}>"


class RoleChangeLog(db.Model):
    """Tracks staff role changes: promotions, demotions, retirement, reactivation."""

    __tablename__ = "role_change_log"

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    member_id = db.Column(db.String(50), nullable=False, index=True)
    member_name = db.Column(db.String(100), nullable=False)
    change_type = db.Column(db.String(20), nullable=False)  # added / removed
    role_id = db.Column(db.String(50), nullable=False)
    role_name = db.Column(db.String(100), nullable=False)
    change_category = db.Column(
        db.String(30), nullable=False
    )  # promotion / demotion / retirement / reactivation / other
    was_staff_before = db.Column(db.Boolean, default=False)
    is_staff_now = db.Column(db.Boolean, default=False)
    modifier_id = db.Column(db.String(50), nullable=True)
    modifier_name = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<RoleChangeLog {self.member_name} {self.change_category} in {self.guild_id}>"


class MemberJoinLeave(db.Model):
    """Tracks member join and leave events for pattern recognition and ML growth prediction."""

    __tablename__ = "member_join_leave"

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    member_id = db.Column(db.String(50), nullable=False, index=True)
    member_name = db.Column(db.String(100), nullable=False)
    is_bot = db.Column(db.Boolean, default=False)
    event_type = db.Column(
        db.String(10), nullable=False, index=True
    )  # 'join' or 'leave'
    leave_reason = db.Column(
        db.String(50), nullable=True
    )  # 'kick', 'ban', 'leave', 'unknown'
    hour_of_day = db.Column(db.Integer, nullable=True)
    day_of_week = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return (
            f"<MemberJoinLeave {self.member_name} {self.event_type} in {self.guild_id}>"
        )


class PingEvent(db.Model):
    """One directed user-to-user ping (reply or direct mention).

    `addressed` stays NULL until the resolver decides whether the pingee
    acknowledged the ping after their return to activity; False means
    unaddressed_after_return. Broadcast pings (@everyone/@here/role) are
    never recorded — those are not 1:1 signals.
    """

    __tablename__ = "ping_events"
    __table_args__ = (
        db.UniqueConstraint(
            "message_id", "pingee_id", name="uq_ping_events_message_pingee"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    pinger_id = db.Column(db.String(50), nullable=False, index=True)
    pinger_name = db.Column(db.String(100), nullable=True)
    pingee_id = db.Column(db.String(50), nullable=False, index=True)
    pingee_name = db.Column(db.String(100), nullable=True)
    channel_id = db.Column(db.String(50), nullable=False)
    channel_name = db.Column(db.String(100), nullable=True)
    message_id = db.Column(db.String(50), nullable=False)
    ping_type = db.Column(
        db.String(20), nullable=False, default="mention"
    )  # reply | mention | reaction_target
    requires_response = db.Column(db.Boolean, nullable=False, default=False)
    addressed = db.Column(db.Boolean, nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)
    return_at = db.Column(db.DateTime, nullable=True)
    # earliest action that addressed the ping (reply-to / mention-back /
    # same-channel post) — enables response-latency statistics
    first_response_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return (
            f"<PingEvent {self.pinger_id} -> {self.pingee_id} "
            f"type={self.ping_type} addressed={self.addressed}>"
        )


class PairScore(db.Model):
    """Batch-computed pairwise statistics. Written only by the recompute job,
    never per-message. NULL affinity_score / unaddressed_rate means
    insufficient_data (sample below the minimum threshold)."""

    __tablename__ = "pair_scores"
    __table_args__ = (
        db.UniqueConstraint(
            "guild_id", "pinger_id", "pingee_id", name="uq_pair_scores_guild_pair"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    pinger_id = db.Column(db.String(50), nullable=False, index=True)
    pinger_name = db.Column(db.String(100), nullable=True)
    pingee_id = db.Column(db.String(50), nullable=False, index=True)
    pingee_name = db.Column(db.String(100), nullable=True)
    affinity_score = db.Column(db.Float, nullable=True)
    unaddressed_rate = db.Column(
        db.Float, nullable=True
    )  # deviation from pinger's own baseline, not an absolute rate
    baseline_unaddressed = db.Column(db.Float, nullable=True)
    sample_size = db.Column(db.Integer, nullable=False, default=0)
    # sudden drop-off detection: interaction in the last 7 days vs the
    # prior 23 days of the scoring window, and when the pair last pinged
    recent_pings = db.Column(db.Integer, nullable=False, default=0)
    prior_pings = db.Column(db.Integer, nullable=False, default=0)
    last_ping_at = db.Column(db.DateTime, nullable=True)
    # interaction depth parameters (see interactions.py)
    initiation_share = db.Column(
        db.Float, nullable=True
    )  # this pinger's fraction of the pair's pings (0.5 = balanced)
    median_response_minutes = db.Column(
        db.Float, nullable=True
    )  # median time for the OTHER side to address this pinger's pings
    max_unaddressed_streak = db.Column(
        db.Integer, nullable=True
    )  # longest run of consecutive unanswered pings by this pinger
    channels = db.Column(db.Integer, nullable=True)  # distinct channels used
    voice_sessions = db.Column(
        db.Integer, nullable=True
    )  # shared same-channel voice sessions in the window
    # conversation structure: bursts of mutually close pings vs drive-bys,
    # and how often this pinger's pings drew a directed ping back
    conversations = db.Column(db.Integer, nullable=False, default=0)
    return_rate = db.Column(db.Float, nullable=True)
    last_computed_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return (
            f"<PairScore {self.pinger_id}->{self.pingee_id} "
            f"affinity={self.affinity_score} unaddressed={self.unaddressed_rate}>"
        )


class MessageRef(db.Model):
    """Content-free per-message pointers (IDs only, never content) used to
    resolve ping_events and attribute interactions. Pruned on cleanup."""

    __tablename__ = "message_refs"

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    channel_id = db.Column(db.String(50), nullable=False, index=True)
    message_id = db.Column(db.String(50), nullable=False, index=True)
    author_id = db.Column(db.String(50), nullable=False, index=True)
    reply_to_message_id = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return (
            f"<MessageRef {self.message_id} by {self.author_id} "
            f"reply_to={self.reply_to_message_id}>"
        )


class UserBehaviorMetric(db.Model):
    """Per-user behavior patterns, batch-computed with the pair scores.

    All metadata-derived: message volume trend (7d vs prior 23d), activity
    rhythm drift (hourly-profile distance), voice hours, week-one absorption
    for new joiners, and @everyone broadcast frequency for staff. Never
    content. Refreshed every 30 min by the same batch as pair_scores.
    """

    __tablename__ = "user_behavior_metrics"
    __table_args__ = (
        db.UniqueConstraint(
            "guild_id", "discord_id", name="uq_user_behavior_guild_member"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    discord_id = db.Column(db.String(50), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=True)
    recent_messages = db.Column(db.Integer, nullable=False, default=0)  # last 7d
    prior_messages = db.Column(db.Integer, nullable=False, default=0)  # prior 23d
    trend = db.Column(db.String(10), nullable=True)  # rising | stable | fading
    rhythm_shift = db.Column(db.Float, nullable=True)  # 0..1 hourly-profile distance
    voice_hours_30d = db.Column(db.Float, nullable=False, default=0.0)
    active_days_30d = db.Column(db.Integer, nullable=False, default=0)
    week1_pings_received = db.Column(db.Integer, nullable=True)  # new joiners only
    absorbed_by = db.Column(db.String(100), nullable=True)
    broadcast_count_30d = db.Column(db.Integer, nullable=True)  # staff only
    first_seen_at = db.Column(db.DateTime, nullable=True)
    computed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return (
            f"<UserBehaviorMetric {self.discord_id} trend={self.trend} "
            f"rhythm={self.rhythm_shift}>"
        )


class BehaviorMetricDaily(db.Model):
    """Per-user per-day behavior metrics, computed from existing metadata
    (message refs, ping events, tasks). All consent-gated: opted-out members
    never get rows. Window metrics (threads, rhythm, funnel) ride in `extra`
    JSON; per-day counters are first-class columns. Written only by the
    daily compute batch — idempotent per (guild_id, user_id, date)."""

    __tablename__ = "behavior_metrics_daily"
    __table_args__ = (
        db.UniqueConstraint(
            "guild_id", "user_id", "date", name="uq_behavior_daily_guild_user_date"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    user_id = db.Column(db.String(50), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    messages = db.Column(db.Integer, nullable=False, default=0)
    pings_sent = db.Column(db.Integer, nullable=False, default=0)
    questions_asked = db.Column(db.Integer, nullable=False, default=0)
    answers_given = db.Column(db.Integer, nullable=False, default=0)
    latency_p50_minutes = db.Column(db.Float, nullable=True)
    latency_p90_minutes = db.Column(db.Float, nullable=True)
    streak_days = db.Column(db.Integer, nullable=False, default=0)
    cadence_cv = db.Column(db.Float, nullable=True)
    channel_diversity = db.Column(db.Float, nullable=True)
    voice_hours = db.Column(db.Float, nullable=False, default=0.0)
    extra = db.Column(db.Text, nullable=True)  # JSON: threads, rhythm, funnel
    computed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return (
            f"<BehaviorMetricDaily {self.user_id} {self.date} "
            f"msgs={self.messages}>"
        )
