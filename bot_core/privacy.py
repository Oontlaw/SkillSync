from bot_core.config import MOD_BOT_NAMES


def is_channel_public(channel, guild):
    """Check if a channel is accessible to @everyone (public).
    Private channels (mod-only, staff-only) don't get content stored."""
    try:
        overwrites = channel.overwrites_for(guild.default_role)
        return overwrites.read_messages is not False
    except Exception as e:
        print(f'[SkillSync] is_channel_public error: {e}')
        return False


def is_mod_bot(member):
    """Check if a member is a known moderation bot."""
    if not member or not member.bot:
        return False
    return any(name in member.name.lower() for name in MOD_BOT_NAMES)
