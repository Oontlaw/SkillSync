from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Worker(db.Model):
    __tablename__ = 'workers'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    discord_id = db.Column(db.String(50), unique=True, nullable=True, index=True)
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
    worker_id = db.Column(db.Integer, db.ForeignKey('workers.id'), nullable=False, index=True)
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
    worker_id = db.Column(db.Integer, db.ForeignKey('workers.id'), nullable=False, index=True)
    change = db.Column(db.Float, nullable=False)       # positive or negative
    reason = db.Column(db.String(300), nullable=False)
    source = db.Column(db.String(50), default='system')  # system / admin / discord
    admin_correction = db.Column(db.Boolean, default=False)
    guild_id = db.Column(db.String(50), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f'<ScoreLog worker={self.worker_id} change={self.change}>'


class CommunityEvent(db.Model):
    __tablename__ = 'community_events'

    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(50), nullable=False, index=True)
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


class MessageRecord(db.Model):
    __tablename__ = 'message_records'

    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(50), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    channel_name = db.Column(db.String(100), nullable=True)
    is_public_channel = db.Column(db.Boolean, default=True)
    message_length = db.Column(db.Integer, default=0)
    message_content = db.Column(db.Text, nullable=True)      # Only stored for public channels
    hour_of_day = db.Column(db.Integer, nullable=True)       # 0-23
    day_of_week = db.Column(db.Integer, nullable=True)       # 0=Mon, 6=Sun
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f'<MessageRecord {self.discord_id} | len={self.message_length}>'


class GuildInfo(db.Model):
    """Stores scanned guild/server information."""
    __tablename__ = 'guild_info'

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
    __tablename__ = 'guild_roles'

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
        return any([
            self.is_admin,
            self.can_ban,
            self.can_kick,
            self.can_manage_guild,
            self.can_manage_roles,
        ])


class GuildMember(db.Model):
    """Stores member information per guild with staff flags and presence tracking."""
    __tablename__ = 'guild_members'

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
    status = db.Column(db.String(20), default='offline')
    activity_name = db.Column(db.String(100), nullable=True)
    activity_type = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class GuildChannel(db.Model):
    """Stores channel information per guild."""
    __tablename__ = 'guild_channels'

    id = db.Column(db.Integer, primary_key=True)
    guild_id = db.Column(db.String(50), nullable=False, index=True)
    channel_id = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    topic = db.Column(db.Text, nullable=True)
    channel_type = db.Column(db.String(20), nullable=False, default='text')  # text, voice, announcement, forum
    category = db.Column(db.String(100), nullable=True)
    position = db.Column(db.Integer, default=0)
    is_public = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class MentionRecord(db.Model):
    __tablename__ = 'mention_records'

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
        return f'<MentionRecord {self.mentioner_id} -> {self.mentioned_id} reply={self.reply_time_seconds}>'


class BehavioralAnomaly(db.Model):
    __tablename__ = 'behavioral_anomalies'

    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(50), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=True)
    guild_id = db.Column(db.String(50), nullable=True)
    anomaly_type = db.Column(db.String(50), nullable=False)
    severity = db.Column(db.Float, default=0.0)
    details = db.Column(db.Text, nullable=True)
    detected_at = db.Column(db.DateTime, default=datetime.utcnow)
    cleared_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f'<BehavioralAnomaly {self.anomaly_type} | {self.discord_id} | sev={self.severity}>'


class AutoModRule(db.Model):
    __tablename__ = 'automod_rules'

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
        return f'<AutoModRule {self.name} | {self.trigger_type} -> {self.action_type}>'


class VoiceActivity(db.Model):
    __tablename__ = 'voice_activity'

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
        return f'<VoiceActivity {self.discord_id} | {self.duration_seconds}s in {self.channel_name}>'


class PingJoinEvent(db.Model):
    """Records when a moderator pings @everyone and new members join within 20 min."""
    __tablename__ = 'ping_join_events'

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
        return f'<PingJoinEvent {self.moderator_name} | +{self.new_members} in {self.guild_name}>'


class BurnoutRisk(db.Model):
    __tablename__ = 'burnout_risks'

    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(db.Integer, db.ForeignKey('workers.id'), nullable=False, index=True)
    discord_id = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=True)
    score = db.Column(db.Float, default=0.0, index=True)
    anomaly_freq = db.Column(db.Float, default=0.0)
    volume_volatility = db.Column(db.Float, default=0.0)
    reversal_rate = db.Column(db.Float, default=0.0)
    voice_creep = db.Column(db.Float, default=0.0)
    signals = db.Column(db.Text, nullable=True)
    detected_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<BurnoutRisk {self.name} | score={self.score}>'


class AutoModTrigger(db.Model):
    __tablename__ = 'automod_triggers'

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
        return f'<AutoModTrigger {self.rule_name} -> {self.user_name} in #{self.channel_name}>'
