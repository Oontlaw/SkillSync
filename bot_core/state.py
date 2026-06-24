from datetime import datetime, timezone
from bot_core.api_client import api_post
from bot_core.logging import log
from bot_core.config import (
    MAX_BUFFER_SIZE, MESSAGE_BUFFER_LIMIT, PRESENCE_BUFFER_LIMIT,
    JOIN_BUFFER_LIMIT, JOIN_LEAVE_BUFFER_LIMIT, MEMBER_PRESENCE_BUFFER_LIMIT,
    MENTION_BUFFER_LIMIT, VOICE_BUFFER_LIMIT,
)

# ── Prefix cache: { guild_id: [prefixes] } ──
prefix_cache = {}

# ── Trusted guilds: { guild_id: True/False } ──
content_trust = {}

# ── Voice session tracking: { user_id: session_data } ──
voice_sessions = {}
voice_buffer = []

# ── Heartbeat state ──
heartbeat_channel_id = None
bot_start_time = None

def set_heartbeat_channel(channel_id):
    global heartbeat_channel_id
    heartbeat_channel_id = channel_id

def set_bot_start_time(t):
    global bot_start_time
    bot_start_time = t

# ── Active @everyone/@here pings: { guild_id: {...} } ──
active_pings = {}

# ── In-memory pending ban/timeout watches ──
pending_bans = {}
pending_timeouts = {}

# ── AutoMod alert channels: { guild_id: { channel_id: [rule_name, ...] } } ──
automod_alert_channels = {}

def set_automod_alert_channels(channels):
    global automod_alert_channels
    automod_alert_channels = channels

# ── Staff activity proximity ──
last_staff_activity = {}

# ── Live online member set per guild: { guild_id: set(member_id) } ──
# Tracked via presence events; seeded from scan.
# Flushed to API as a simple count to update GuildInfo.online_count.
online_members = {}  # dict[str, set[int]]

def track_online(guild_id: str, member_id: int):
    online_members.setdefault(guild_id, set()).add(member_id)

def track_offline(guild_id: str, member_id: int):
    online_members.get(guild_id, set()).discard(member_id)

def seed_online_set(guild_id: str, member_ids: list):
    online_members[guild_id] = set(member_ids)

# ── Behavioral message buffer ──
message_buffer = []

# ── Presence change buffer ──
presence_buffer = []

# ── Member join buffer ──
join_buffer = []

# ── Member join/leave buffer ──
join_leave_buffer = []

# ── Member presence buffer ──
member_presence_buffer = []

# ── Mention tracking buffer ──
pending_mentions = {}
mention_buffer = []

# ── ML retrain counter (weekly schedule) ──
ml_retrain_counter = 0
forecast_counter = 0

def set_ml_retrain_counter(val):
    global ml_retrain_counter
    ml_retrain_counter = val

def inc_ml_retrain_counter():
    global ml_retrain_counter
    ml_retrain_counter += 1
    return ml_retrain_counter

def inc_forecast_counter():
    global forecast_counter
    forecast_counter += 1
    return forecast_counter

def reset_forecast_counter():
    global forecast_counter
    forecast_counter = 0

# ── Retrain-on-correction flag ──
_correction_retrain_needed = False
_correction_retrain_count = 0

def request_retrain():
    """Signal that a correction-feedback retrain is needed (called after admin correction)."""
    global _correction_retrain_needed
    _correction_retrain_needed = True

def consume_retrain_request():
    """Check and clear the retrain flag. Returns True if retrain was requested."""
    global _correction_retrain_needed, _correction_retrain_count
    if _correction_retrain_needed:
        _correction_retrain_needed = False
        _correction_retrain_count += 1
        return True
    return False


async def flush_message_buffer():
    """Send buffered messages to the API for behavioral analysis."""
    global message_buffer
    if not message_buffer:
        return
    batch = message_buffer[:]
    message_buffer = []
    await api_post('/observer/messages', batch)
    log(f'FLUSHED {len(batch)} messages to behavioral log')


async def flush_presence_buffer():
    """Send buffered presence updates to the API."""
    global presence_buffer
    if not presence_buffer:
        return
    batch = presence_buffer[:]
    presence_buffer = []
    await api_post('/observer/activity', {'batch': True, 'updates': batch})
    log(f'FLUSHED {len(batch)} presence updates')


async def flush_voice_buffer():
    """Send buffered voice sessions to the API."""
    global voice_buffer
    if not voice_buffer:
        return
    batch = voice_buffer[:]
    voice_buffer = []
    await api_post('/observer/voice-activity', {'batch': True, 'sessions': batch})
    log(f'FLUSHED {len(batch)} voice sessions')


async def flush_mention_buffer():
    """Send buffered mention records to the API."""
    global mention_buffer
    if not mention_buffer:
        return
    batch = mention_buffer[:]
    mention_buffer = []
    await api_post('/observer/mentions', batch)
    log(f'FLUSHED {len(batch)} mentions')


async def flush_join_buffer():
    """Send buffered member join records to the API."""
    global join_buffer
    if not join_buffer:
        return
    batch = join_buffer[:]
    join_buffer = []
    await api_post('/observer/activity', {'batch': True, 'joins': batch})


async def flush_member_presence_buffer():
    """Send buffered presence updates to update GuildMember online status and activity."""
    global member_presence_buffer
    if not member_presence_buffer:
        return
    batch = member_presence_buffer[:]
    member_presence_buffer = []
    await api_post('/observer/presence', {'updates': batch})
    log(f'FLUSHED {len(batch)} member presence updates')


async def flush_online_count():
    """Send live online member counts to API."""
    if not online_members:
        return
    payload = {}
    for guild_id, members in list(online_members.items()):
        payload[guild_id] = len(members)
    await api_post('/observer/online-count', payload)
    log(f'FLUSHED online counts for {len(payload)} guilds: {payload}')


async def flush_join_leave_buffer():
    """Send buffered member join/leave records to the API."""
    global join_leave_buffer
    if not join_leave_buffer:
        return
    batch = join_leave_buffer[:]
    join_leave_buffer = []
    await api_post('/observer/join-leave', {'batch': True, 'events': batch})
    log(f'FLUSHED {len(batch)} join/leave events')
