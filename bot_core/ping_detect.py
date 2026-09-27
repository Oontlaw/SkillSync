"""Pure ping-detection helpers for pairwise interaction profiling.

Content is inspected transiently here — nothing in this module stores or
returns message content, only derived flags and IDs.
"""
import re

# Imperative/request patterns that imply a response is wanted. Intentionally
# small — a coarse heuristic is fine (spec: don't over-engineer).
_IMPERATIVE_RE = re.compile(
    r'\b(can|could|would|will|please|pls|plz|need|send|check|review|fix|'
    r'update|deploy|share|confirm|let me know|asap|eod)\b',
    re.IGNORECASE,
)


def requires_response(content):
    """True if the message looks like it wants an answer: a question mark or
    an imperative/request pattern. No content -> not requiring response."""
    if not content:
        return False
    if '?' in content:
        return True
    return bool(_IMPERATIVE_RE.search(content))


def extract_pings(message):
    """Return ping dicts for the directed 1:1 pings in a discord.py message.

    - Broadcast pings (@everyone / @here) drop the whole message — not 1:1.
    - Role mentions never produce pings.
    - A reply ping points at the referenced message's author; direct user
      mentions produce mention pings. Bot pingees and self-pings are skipped.
    """
    if message.author.bot or message.mention_everyone:
        return []

    author = message.author
    base = {
        'guild_id': str(message.guild.id),
        'pinger_id': str(author.id),
        'pinger_name': author.name,
        'channel_id': str(message.channel.id),
        'channel_name': getattr(message.channel, 'name', 'unknown'),
        'message_id': str(message.id),
        'requires_response': requires_response(message.content),
    }

    # Reply-to attribution (duck-typed so this module stays import-light)
    reply_author = None
    reference = getattr(message, 'reference', None)
    if reference is not None and reference.message_id:
        resolved = getattr(reference, 'resolved', None)
        r_author = getattr(resolved, 'author', None)
        if r_author is not None and not r_author.bot and r_author.id != author.id:
            reply_author = r_author

    pings = []
    seen = set()
    if reply_author is not None:
        seen.add(reply_author.id)
        pings.append(
            dict(
                base,
                pingee_id=str(reply_author.id),
                pingee_name=reply_author.name,
                ping_type='reply',
            )
        )
    for mentioned in getattr(message, 'mentions', None) or []:
        if mentioned.bot or mentioned.id == author.id or mentioned.id in seen:
            continue
        seen.add(mentioned.id)
        pings.append(
            dict(
                base,
                pingee_id=str(mentioned.id),
                pingee_name=mentioned.name,
                ping_type='mention',
            )
        )
    return pings
