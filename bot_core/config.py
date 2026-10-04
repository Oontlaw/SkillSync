import os

import discord

load_dotenv = __import__("dotenv").load_dotenv
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# ponytail: use 127.0.0.1 instead of localhost — Windows DNS tries IPv6 first,
# adding ~2s latency to every request (IPv6 timeout → IPv4 fallback).
SKILLSYNC_API = os.getenv("SKILLSYNC_API", "http://127.0.0.1:5000/api")
API_KEY = os.getenv("API_KEY", "")

MESSAGE_RETENTION_DAYS = int(os.getenv("MESSAGE_RETENTION_DAYS", "180"))
PING_RETENTION_DAYS = int(os.getenv("PING_RETENTION_DAYS", "90"))
VOICE_RETENTION_DAYS = int(os.getenv("VOICE_RETENTION_DAYS", "180"))
JOIN_EVENT_RETENTION_DAYS = int(os.getenv("JOIN_EVENT_RETENTION_DAYS", "180"))
HEARTBEAT_GUILD_ID = os.getenv("HEARTBEAT_GUILD_ID", "")

BAN_WATCH_HOURS = 48
PING_WATCH_MINUTES = 20
HEARTBEAT_INTERVAL_MINUTES = 5
MAX_BUFFER_SIZE = 10000

MESSAGE_BUFFER_LIMIT = 30
PRESENCE_BUFFER_LIMIT = 50
JOIN_BUFFER_LIMIT = 30
JOIN_LEAVE_BUFFER_LIMIT = 30
MEMBER_PRESENCE_BUFFER_LIMIT = 50
MENTION_BUFFER_LIMIT = 30
VOICE_BUFFER_LIMIT = 30
PING_BUFFER_LIMIT = 30
REF_BUFFER_LIMIT = 50

MOD_BOT_NAMES = ["mee6", "dyno", "carl-bot", "wick", "arcane", "combot", "gaius"]

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True
intents.guilds = True
intents.presences = True
intents.voice_states = True
