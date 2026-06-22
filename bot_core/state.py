from datetime import datetime, timezone
from bot_core.api_client import api_post
from bot_core.logging import log
from bot_core.config import (
    MAX_BUFFER_SIZE, MESSAGE_BUFFER_LIMIT, PRESENCE_BUFFER_LIMIT,
    JOIN_BUFFER_LIMIT, MEMBER_PRESENCE_BUFFER_LIMIT,
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

# ── Behavioral message buffer ──
message_buffer = []

# ── Presence change buffer ──
presence_buffer = []

# ── Member join buffer ──
join_buffer = []

# ── Member presence buffer ──
member_presence_buffer = []

# ── Mention tracking buffer ──
pending_mentions = {}
mention_buffer = []

# ── ML retrain counter ──
ml_retrain_counter = 0

def set_ml_retrain_counter(val):
    global ml_retrain_counter
    ml_retrain_counter = val

def inc_ml_retrain_counter():
    global ml_retrain_counter
    ml_retrain_counter += 1
    return ml_retrain_counter


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
