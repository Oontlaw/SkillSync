import discord
from bot_core.config import HEARTBEAT_GUILD_ID
from bot_core.state import set_heartbeat_channel
from bot_core.logging import log


async def setup_heartbeat(bot):
    """Find or create the heartbeat channel in HEARTBEAT_GUILD_ID."""
    if not HEARTBEAT_GUILD_ID:
        log('Heartbeat disabled: HEARTBEAT_GUILD_ID not set')
        return
    guild = bot.get_guild(int(HEARTBEAT_GUILD_ID))
    if not guild:
        log(f'Heartbeat guild {HEARTBEAT_GUILD_ID} not found')
        return
    channel = discord.utils.get(guild.text_channels, name='heartbeat')
    if not channel:
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            channel = await guild.create_text_channel('heartbeat', overwrites=overwrites, topic='Bot status & uptime updates')
            log(f'Created #heartbeat channel in {guild.name}')
        except Exception as e:
            log(f'Could not create heartbeat channel: {e}')
            return
    set_heartbeat_channel(channel.id)
    await channel.send('🟢 **Heartbeat started** — Bot online, monitoring active.')
    log(f'Heartbeat channel set to #{channel.name} in {guild.name}')
