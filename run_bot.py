#!/usr/bin/env python3
"""
SkillSync Discord Bot — bot entry point.

Usage:
    python run_bot.py

Starts the Discord bot using the token from DISCORD_TOKEN in .env.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Import the bot instance and its cog
import bot_commands
from bot import DISCORD_TOKEN, bot, prefix_cache
from bot_core.scanner import scan_guild as shared_scan_guild


async def main():
    bot.prefix_cache = prefix_cache
    bot.scan_guild = shared_scan_guild
    await bot.add_cog(bot_commands.Moderation(bot))
    while True:
        try:
            await bot.start(DISCORD_TOKEN)
        except KeyboardInterrupt:
            await bot.close()
            return
        except Exception as e:
            print(f'[SkillSync] Bot disconnected: {e}. Reconnecting in 10s...')
            await asyncio.sleep(10)


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
