from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

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
