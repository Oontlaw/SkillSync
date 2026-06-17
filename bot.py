import discord
from discord.ext import commands
import requests
import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
SKILLSYNC_API = os.getenv('SKILLSYNC_API', 'http://localhost:5000/api')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True

bot = commands.Bot(command_prefix='!', intents=intents)

# --- Helper: notify SkillSync API of a community event ---
def record_community_event(discord_id, event_type, detail, score_impact):
    try:
        requests.post(f'{SKILLSYNC_API}/community/event', json={
            'discord_id': str(discord_id),
            'event_type': event_type,
            'detail': detail,
            'score_impact': score_impact
        })
    except Exception as e:
        print(f'[SkillSync Bot] API error: {e}')


# --- Events ---

@bot.event
async def on_ready():
    print(f'✅ SkillSync Bot is online as {bot.user}')


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content.lower()

    # Detect helpfulness (simple keyword pattern — improves with AI over time)
    helpful_keywords = ['solved', 'fixed', 'here is', 'try this', 'solution', 'i found']
    if any(kw in content for kw in helpful_keywords) and len(content) > 50:
        record_community_event(
            discord_id=message.author.id,
            event_type='community_helpful',
            detail=f'Helpful message in #{message.channel.name}',
            score_impact=5
        )

    await bot.process_commands(message)


@bot.event
async def on_member_ban(guild, user):
    record_community_event(
        discord_id=user.id,
        event_type='community_rule_break',
        detail=f'Banned from {guild.name}',
        score_impact=-8
    )
    print(f'[SkillSync] Ban recorded for {user.name}')


@bot.event
async def on_member_remove(member):
    # Could indicate kick — log as mild anomaly
    record_community_event(
        discord_id=member.id,
        event_type='community_removed',
        detail=f'Left/removed from {member.guild.name}',
        score_impact=0
    )


# --- Commands ---

@bot.command(name='score')
async def check_score(ctx, member: discord.Member = None):
    """Check a member's SkillSync score."""
    target = member or ctx.author
    try:
        res = requests.get(f'{SKILLSYNC_API}/workers')
        workers = res.json()
        match = next((w for w in workers if w['discord_id'] == str(target.id)), None)
        if match:
            await ctx.send(f"📊 **{target.display_name}**'s SkillSync Score: **{match['score']} pts**")
        else:
            await ctx.send(f"⚠️ {target.display_name} is not registered in SkillSync.")
    except Exception as e:
        await ctx.send(f"❌ Could not fetch score: {e}")


@bot.command(name='leaderboard')
async def leaderboard(ctx):
    """Show top 5 workers."""
    try:
        res = requests.get(f'{SKILLSYNC_API}/leaderboard')
        top = res.json()[:5]
        msg = "🏆 **SkillSync Leaderboard**\n"
        for i, w in enumerate(top, 1):
            msg += f"{i}. {w['name']} — **{w['score']} pts**\n"
        await ctx.send(msg)
    except Exception as e:
        await ctx.send(f"❌ Could not fetch leaderboard: {e}")


@bot.command(name='kudos')
@commands.has_permissions(manage_messages=True)
async def give_kudos(ctx, member: discord.Member, *, reason: str):
    """Staff command: give bonus points to a member."""
    record_community_event(
        discord_id=member.id,
        event_type='extra_contribution',
        detail=f'Kudos from {ctx.author.name}: {reason}',
        score_impact=20
    )
    await ctx.send(f"⭐ Kudos given to **{member.display_name}** for: {reason}")


if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
