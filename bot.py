import os, sys
import discord
from discord.ext import commands
from dotenv import load_dotenv
load_dotenv()

from bot_core.config import DISCORD_TOKEN, intents
from bot_core.state import prefix_cache
from bot_core.scanner import scan_guild as shared_scan_guild
from bot_core.events_ready import handle_ready, handle_guild_join
from bot_core.events_messages import handle_message
from bot_core.events_moderation import handle_member_ban, handle_member_unban, handle_member_update, handle_member_remove
from bot_core.events_presence import handle_presence_update, handle_member_join, handle_voice_state_update
import bot_commands


def get_prefix(bot_, message):
    """Dynamic prefix per guild + bot mention. Falls back to ['!ss ']."""
    prefixes = ['!ss ']
    if message.guild:
        prefixes = prefix_cache.get(str(message.guild.id), ['!ss '])
    if '!ss ' not in prefixes:
        prefixes = ['!ss '] + prefixes
    return commands.when_mentioned_or(*prefixes)(bot_, message)


bot = commands.Bot(command_prefix=get_prefix, intents=intents)


# ── Event registrations (thin wrappers) ──

@bot.event
async def on_ready():
    await handle_ready(bot)


@bot.event
async def on_guild_join(guild):
    await handle_guild_join(bot, guild)


@bot.event
async def on_message(message):
    await handle_message(bot, message)


@bot.event
async def on_member_ban(guild, user):
    await handle_member_ban(guild, user)


@bot.event
async def on_member_unban(guild, user):
    await handle_member_unban(guild, user)


@bot.event
async def on_member_update(before, after):
    await handle_member_update(before, after)


@bot.event
async def on_member_remove(member):
    await handle_member_remove(member)


@bot.event
async def on_presence_update(before, after):
    await handle_presence_update(before, after)


@bot.event
async def on_member_join(member):
    await handle_member_join(member)


@bot.event
async def on_voice_state_update(member, before, after):
    await handle_voice_state_update(member, before, after)


# ── Main ──

if __name__ == '__main__':
    bot.prefix_cache = prefix_cache
    bot.scan_guild = shared_scan_guild
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(bot.add_cog(bot_commands.Moderation(bot)))
        loop.run_until_complete(bot.start(DISCORD_TOKEN))
    except KeyboardInterrupt:
        loop.run_until_complete(bot.close())
    finally:
        loop.close()
