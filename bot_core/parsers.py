import re


def extract_warn_from_embed(embed):
    """
    Parse warn embeds from common mod bots (MEE6, Dyno, Carl-bot).
    Returns parsed data dict or None if not a warn embed.
    """
    if not embed.description and not embed.fields:
        return None

    text = (embed.description or '') + ' '.join(f.value for f in embed.fields)
    text_lower = text.lower()

    warn_keywords = ['warned', 'warning', 'infraction', 'strike', 'muted', 'kicked']
    if not any(kw in text_lower for kw in warn_keywords):
        return None

    mod_name = None
    target_name = None
    reason = None

    for field in embed.fields:
        name_lower = field.name.lower()
        if 'moderator' in name_lower or 'staff' in name_lower or 'by' in name_lower:
            mod_name = field.value.strip()
        if 'user' in name_lower or 'member' in name_lower or 'target' in name_lower:
            target_name = field.value.strip()
        if 'reason' in name_lower:
            reason = field.value.strip()

    if embed.footer and embed.footer.text:
        footer = embed.footer.text
        if 'moderator' in footer.lower() or 'by' in footer.lower():
            mod_name = footer

    return {
        'mod_name': mod_name,
        'target_name': target_name,
        'reason': reason or 'No reason provided',
        'raw': text[:300]
    }


def extract_automod_alert(embed):
    """
    Parse Discord's native AutoMod alert embed from alert channels.
    Returns dict with trigger data or None.
    """
    if not embed.description and not embed.fields:
        return None

    title = (embed.title or '') + (embed.description or '')
    title_lower = title.lower()

    if not any(kw in title_lower for kw in ['flagged', 'blocked', 'automod', 'auto mod', 'warning']):
        return None

    rule_name = None
    user_name = None
    user_id = None
    channel_name = None
    channel_id = None
    content_snippet = None
    action_taken = None

    if embed.author and embed.author.name:
        if not rule_name:
            rule_name = embed.author.name

    for field in embed.fields:
        name_lower = field.name.lower()
        value = field.value.strip()
        if 'rule' in name_lower:
            rule_name = value
        elif 'user' in name_lower or 'member' in name_lower:
            user_name = value
        elif 'channel' in name_lower:
            channel_name = value
        elif 'content' in name_lower or 'message' in name_lower or 'flagged' in name_lower:
            content_snippet = value[:300]
        elif 'action' in name_lower:
            action_taken = value

    if not user_name and title and '@' in title:
        m = re.search(r'@(\S+)', title)
        if m:
            user_name = m.group(1)

    if not channel_name and title and '#' in title:
        m = re.search(r'#(\S+)', title)
        if m:
            channel_name = m.group(1)

    if not content_snippet and embed.description and not embed.fields:
        content_snippet = embed.description[:300]

    if not rule_name and embed.author and embed.author.name:
        rule_name = embed.author.name

    if not rule_name:
        return None

    return {
        'rule_name': rule_name,
        'rule_id': None,
        'user_name': user_name or 'Unknown',
        'user_id': user_id,
        'channel_name': channel_name,
        'channel_id': channel_id,
        'content_snippet': content_snippet,
        'action_taken': action_taken or 'flagged',
    }
