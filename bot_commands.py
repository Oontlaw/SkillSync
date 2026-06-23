import discord
from discord.ext import commands
import requests
import os
import typing
import asyncio
from datetime import datetime, timedelta, timezone

SKILLSYNC_API = os.getenv('SKILLSYNC_API', 'http://localhost:5000/api')
API_KEY = os.getenv('API_KEY')
LOG_FILE = os.path.join(os.environ.get('TEMP', 'C:\\Temp'), 'skillsync_bot.log')
def log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}\n')
    except Exception:
        pass

async def api_post(endpoint, payload):
    try:
        await asyncio.to_thread(
            requests.post, f'{SKILLSYNC_API}{endpoint}', json=payload,
            headers={'Authorization': f'Bearer {API_KEY}'}, timeout=5
        )
        log(f'API OK {endpoint}')
    except Exception as e:
        log(f'API error in bot_commands on {endpoint}: {e}')


class Moderation(commands.Cog):
    """Moderation commands for the staff bot. All actions also log to observer API."""

    def __init__(self, bot):
        self.bot = bot

    # ── Permission helper ──

    async def cog_command_error(self, ctx, error):
        """Catch-all error handler for unhandled command errors."""
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f'Missing: `{error.param.name}` → try `{ctx.prefix}{ctx.command.name} {ctx.command.signature}`')
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f'Invalid argument: {error}')
        elif isinstance(error, commands.CommandNotFound):
            pass
        else:
            await ctx.send(f'Error: {error}')

    def can_moderate(self, ctx, target):
        """Check if the invoker has permission to act on target (role hierarchy)."""
        if ctx.author == target:
            return False, "You cannot target yourself."
        if ctx.author.top_role <= target.top_role and ctx.guild.owner_id != ctx.author.id:
            return False, "Your role is not high enough to moderate this user."
        return True, None

    # ── prefix ──

    @commands.command(name='prefix', help='Show or set prefixes. Usage: prefix, prefix add !, prefix remove !, prefix reset')
    @commands.has_permissions(administrator=True)
    async def set_prefix(self, ctx, action: typing.Optional[str] = None, *, value: typing.Optional[str] = None):
        prefixes = list(self.bot.prefix_cache.get(str(ctx.guild.id), ['!ss ']))
        if '!ss ' not in prefixes:
            prefixes = ['!ss '] + prefixes

        # Show current prefixes
        if action is None:
            display = '`, `'.join(prefixes)
            await ctx.send(f'Active prefixes: `{display}`')
            return

        # Add a prefix
        if action == 'add' and value:
            if len(value) > 10:
                await ctx.send('Prefix too long. Maximum 10 characters.')
                return
            if value in prefixes:
                await ctx.send(f'Prefix `{value}` already exists.')
                return
            prefixes.append(value)
            self.bot.prefix_cache[str(ctx.guild.id)] = prefixes
            await asyncio.to_thread(
                requests.patch, f'{SKILLSYNC_API}/observer/guilds/{ctx.guild.id}/prefix',
                json={'prefixes': prefixes}, headers={'Authorization': f'Bearer {API_KEY}'}, timeout=5
            )
            await ctx.send(f'Added prefix `{value}`. Active: `{"`, `".join(prefixes)}`')
            return

        # Remove a prefix (can't remove default)
        if action == 'remove' and value:
            if value == '!ss ':
                await ctx.send('Cannot remove the default `!ss ` prefix.')
                return
            if value not in prefixes:
                await ctx.send(f'Prefix `{value}` not found.')
                return
            prefixes.remove(value)
            self.bot.prefix_cache[str(ctx.guild.id)] = prefixes
            await asyncio.to_thread(
                requests.patch, f'{SKILLSYNC_API}/observer/guilds/{ctx.guild.id}/prefix',
                json={'prefixes': prefixes}, headers={'Authorization': f'Bearer {API_KEY}'}, timeout=5
            )
            await ctx.send(f'Removed prefix `{value}`. Active: `{"`, `".join(prefixes)}`')
            return

        # Reset to just default
        if action == 'reset':
            prefixes = ['!ss ']
            self.bot.prefix_cache[str(ctx.guild.id)] = prefixes
            await asyncio.to_thread(
                requests.patch, f'{SKILLSYNC_API}/observer/guilds/{ctx.guild.id}/prefix',
                json={'prefixes': prefixes}, headers={'Authorization': f'Bearer {API_KEY}'}, timeout=5
            )
            await ctx.send('Reset to default prefix: `!ss `')
            return

        # Single prefix shorthand: `prefix !` adds `!` as additional
        if not value:
            if len(action) > 10:
                await ctx.send('Prefix too long. Maximum 10 characters.')
                return
            if action in prefixes:
                await ctx.send(f'Prefix `{action}` already exists.')
                return
            prefixes.append(action)
            self.bot.prefix_cache[str(ctx.guild.id)] = prefixes
            await asyncio.to_thread(
                requests.patch, f'{SKILLSYNC_API}/observer/guilds/{ctx.guild.id}/prefix',
                json={'prefixes': prefixes}, headers={'Authorization': f'Bearer {API_KEY}'}, timeout=5
            )
            await ctx.send(f'Added prefix `{action}`. Active: `{"`, `".join(prefixes)}`')
            return

    @set_prefix.error
    async def prefix_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send('Only server admins can change the prefix.')

    # ── scan ──

    @commands.command(name='scan', help='Re-scan guild roles, members, and permissions.')
    @commands.has_permissions(administrator=True)
    async def rescan(self, ctx):
        await ctx.send('Scanning server roles, members, and permissions...')
        await self.bot.scan_guild(ctx.guild)
        await ctx.send('Scan complete.')

    # ── ban ──

    @commands.command(name='ban', help='Ban a user. Usage: ban @user [reason]')
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx, target: discord.Member, *, reason='No reason given'):
        ok, err = self.can_moderate(ctx, target)
        if not ok:
            await ctx.send(err)
            return
        try:
            await target.ban(reason=f'{ctx.author.name}: {reason}')
            await ctx.send(f'Banned {target.name}. Reason: {reason}')
        except discord.Forbidden:
            await ctx.send('I don\'t have permission to ban that user.')
            return
        except discord.HTTPException as e:
            await ctx.send(f'Failed to ban: {e}')
            return
        await api_post('/observer/action', {
            'discord_id': str(ctx.author.id),
            'staff_name': ctx.author.name,
            'action_type': 'ban_issued',
            'target': target.name,
            'target_id': str(target.id),
            'guild': ctx.guild.name,
            'guild_id': str(ctx.guild.id),
            'reason': reason,
            'flagged': False,
            'flag_reason': None,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })

    @ban.error
    async def ban_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send('You need Ban Members permission to use this.')
        elif isinstance(error, commands.BadArgument):
            await ctx.send('Could not find that user. Use @mention or UserID.')

    # ── kick ──

    @commands.command(name='kick', help='Kick a user. Usage: kick @user [reason]')
    @commands.has_permissions(kick_members=True)
    async def kick(self, ctx, target: discord.Member, *, reason='No reason given'):
        ok, err = self.can_moderate(ctx, target)
        if not ok:
            await ctx.send(err)
            return
        try:
            await target.kick(reason=f'{ctx.author.name}: {reason}')
            await ctx.send(f'Kicked {target.name}. Reason: {reason}')
        except discord.Forbidden:
            await ctx.send('I don\'t have permission to kick that user.')
            return
        except discord.HTTPException as e:
            await ctx.send(f'Failed to kick: {e}')
            return
        await api_post('/observer/action', {
            'discord_id': str(ctx.author.id),
            'staff_name': ctx.author.name,
            'action_type': 'kick_issued',
            'target': target.name,
            'target_id': str(target.id),
            'guild': ctx.guild.name,
            'guild_id': str(ctx.guild.id),
            'reason': reason,
            'flagged': False,
            'flag_reason': None,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })

    @kick.error
    async def kick_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send('You need Kick Members permission to use this.')

    # ── timeout ──

    @commands.command(name='timeout', help='Timeout a user. Usage: timeout @user <minutes> [reason]')
    @commands.has_permissions(moderate_members=True)
    async def timeout(self, ctx, target: discord.Member, minutes: int, *, reason='No reason given'):
        ok, err = self.can_moderate(ctx, target)
        if not ok:
            await ctx.send(err)
            return
        if minutes < 1 or minutes > 40320:
            await ctx.send('Timeout must be between 1 minute and 28 days (40320 min).')
            return
        until = discord.utils.utcnow() + timedelta(minutes=minutes)
        try:
            await target.timeout(until, reason=f'{ctx.author.name}: {reason}')
            await ctx.send(f'Timed out {target.name} for {minutes} min. Reason: {reason}')
        except discord.Forbidden:
            await ctx.send('I don\'t have permission to timeout that user.')
            return
        except discord.HTTPException as e:
            await ctx.send(f'Failed to timeout: {e}')
            return
        await api_post('/observer/action', {
            'discord_id': str(ctx.author.id),
            'staff_name': ctx.author.name,
            'action_type': 'timeout_issued',
            'target': target.name,
            'target_id': str(target.id),
            'guild': ctx.guild.name,
            'guild_id': str(ctx.guild.id),
            'duration_minutes': minutes,
            'reason': reason,
            'flagged': False,
            'flag_reason': None,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })

    @timeout.error
    async def timeout_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send('You need Moderate Members permission to use this.')

    # ── warn ──

    @commands.command(name='warn', help='Warn a user. Usage: warn @user [reason]')
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx, target: discord.Member, *, reason='No reason given'):
        ok, err = self.can_moderate(ctx, target)
        if not ok:
            await ctx.send(err)
            return
        await api_post('/observer/action', {
            'discord_id': str(ctx.author.id),
            'staff_name': ctx.author.name,
            'action_type': 'warn_issued',
            'target': target.name,
            'target_id': str(target.id),
            'guild': ctx.guild.name,
            'guild_id': str(ctx.guild.id),
            'reason': reason,
            'flagged': False,
            'flag_reason': None,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
        await ctx.send(f'Warned {target.name}. Reason: {reason}')

    # ── purge ──

    @commands.command(name='purge', aliases=['clear'], help='Delete recent messages. Usage: purge <count>')
    @commands.has_permissions(manage_messages=True)
    async def purge(self, ctx, count: int):
        if count < 1 or count > 100:
            await ctx.send('You can purge between 1 and 100 messages.')
            return
        try:
            deleted = await ctx.channel.purge(limit=count + 1)
            await ctx.send(f'Deleted {len(deleted) - 1} messages.', delete_after=3)
        except discord.Forbidden:
            await ctx.send('I don\'t have permission to purge messages.')
        except discord.HTTPException as e:
            await ctx.send(f'Failed to purge: {e}')

    @purge.error
    async def purge_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send('You need Manage Messages permission to use this.')


async def setup(bot):
    await bot.add_cog(Moderation(bot))
