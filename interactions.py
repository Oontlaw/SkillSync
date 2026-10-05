"""Pairwise interaction statistics.

resolve_pending_pings() resolves ping_events.addressed: after the pingee
returns to activity, the ping counts as addressed if the pingee replies to
the ping's message, mentions the pinger back within W minutes of returning,
or posts in the ping's channel within their next K messages. Otherwise it
becomes unaddressed_after_return. General activity elsewhere never resolves
a ping.

recompute_pair_scores() batch-rebuilds pair_scores over the trailing window
of directed 1:1 pings (reply or mention, question or not; requires_response
is still recorded per ping). affinity_score is NPMI over daily activity
buckets. unaddressed_rate is the pair's rate minus the pinger's own baseline
rate across all pingees, so it reads as a deviation rather than an absolute
rate. recent_pings/prior_pings/last_ping_at power the sudden drop-off view.

cross_guild_pair_rows() merges pings across guilds at read time with the
same math; nothing is persisted.

recompute_user_metrics() batch-rebuilds user_behavior_metrics: message
trend (7d vs prior 23d), hourly-rhythm drift, voice hours, active days,
week-1 ping absorption for new joiners, broadcast counts for staff.
Runs on the same delete-all + reinsert cycle as pair_scores.

voice_only_pairs() returns pairs that share voice sessions but have no
scored text interaction.
"""
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func

from database import (
    GuildMember,
    MessageRef,
    PairScore,
    PingEvent,
    PingJoinEvent,
    UserBehaviorMetric,
    VoiceActivity,
    db,
)

# ── Tunables ──
ADDRESS_WINDOW_MINUTES = 30  # W: ping resolves once pingee has been active this long
ADDRESS_MAX_MESSAGES = 5  # K: same-channel post within pingee's next K messages counts
AFFINITY_WINDOW_DAYS = 30  # trailing window for the PMI day-buckets
MIN_PAIR_SAMPLE = 10  # N: minimum pings before any score is surfaced
STALE_PING_DAYS = 14  # pings older than this with no pingee return resolve as unaddressed
RECENT_WINDOW_DAYS = 7  # drift: "recent" slice of the scoring window
FADING_PRIOR_MIN = 5  # drift: pair counted "fading" if it had at least this many pings
FADING_RECENT_MAX = 1  # drift: ...in the prior slice but at most this many recently
CONVERSATION_GAP_MINUTES = 30  # gap that starts a new conversation
RETURN_WINDOW_HOURS = 24  # ping-back window for return_rate
CROSS_GUILD = "__all__"  # sentinel guild_id for read-time cross-server rows


# ── Profiling consent (opt-out model) ──

def opted_out_members(guild_ids=None):
    """(guild_id, member_id) pairs with consent_optin=False. Missing rows or
    NULL count as opted in."""
    q = db.session.query(GuildMember.guild_id, GuildMember.member_id).filter(
        GuildMember.consent_optin.is_(False)
    )
    if guild_ids:
        q = q.filter(GuildMember.guild_id.in_(guild_ids))
    return {(g, m) for g, m in q.all()}


# ── Pure scoring helpers (unit-tested directly) ──

def npmi(p_ab, p_a, p_b):
    """Normalized PMI, clamped to [-1, 1]. None when any probability is zero.

    The clamp is a guard, not decoration: pair co-occurrence days derive from
    pings in both directions, so p_ab can marginally exceed one side's
    activity probability when the other user pinged an otherwise-inactive
    day. Spec requires a bounded range; enforce it.
    """
    if p_ab <= 0 or p_a <= 0 or p_b <= 0:
        return None
    pmi = math.log(p_ab / (p_a * p_b))
    return max(-1.0, min(1.0, pmi / -math.log(p_ab)))


def corrected_unaddressed_rate(pair_rate, baseline):
    """Pair unaddressed rate deviated from the pinger's baseline. None when
    either side lacks a computable rate (insufficient data)."""
    if pair_rate is None or baseline is None:
        return None
    return round(pair_rate - baseline, 4)


# ── Shared computation blocks (used by per-guild recompute AND cross-guild
#    read-time aggregation — one math path, two keyings) ──

def _active_days(window_start, pings, guild_ids=None, opted_out=None):
    """Distinct active days per author: the message-ref stream (all messaging)
    plus the days a user sent pings (a ping is activity by the pinger).
    Opted-out authors contribute nothing."""
    active = defaultdict(set)
    q = db.session.query(
        MessageRef.guild_id, MessageRef.author_id, func.date(MessageRef.created_at)
    ).filter(MessageRef.created_at >= window_start)
    if guild_ids:
        q = q.filter(MessageRef.guild_id.in_(guild_ids))
    skip = opted_out or set()
    for guild_id, author_id, day in q.distinct().all():
        if (guild_id, author_id) in skip:
            continue
        active[author_id].add(str(day))
    for ping in pings:
        if (ping.guild_id, ping.pinger_id) in skip:
            continue
        active[ping.pinger_id].add(ping.created_at.date().isoformat())
    return active


def _baseline_stats(pings):
    """Per-pinger [resolved, unaddressed] across ALL pingees — the baseline
    every pair rate is deviated against."""
    stats = defaultdict(lambda: [0, 0])
    for ping in pings:
        if ping.addressed is not None:
            stats[ping.pinger_id][0] += 1
            if not ping.addressed:
                stats[ping.pinger_id][1] += 1
    return stats


def _shared_voice_sessions(guild_ids, user_a, user_b, window_start):
    """Count same-channel voice sessions between two users with overlapping
    time ranges inside the scoring window — the voice dimension the text-ping
    graph can't see. Cross-guild mode merges sessions from every guild."""
    q = VoiceActivity.query.filter(
        VoiceActivity.joined_at.isnot(None),
        VoiceActivity.left_at.isnot(None),
        VoiceActivity.created_at >= window_start,
        VoiceActivity.discord_id.in_([user_a, user_b]),
    )
    if guild_ids:
        q = q.filter(VoiceActivity.guild_id.in_(guild_ids))
    rows = q.all()
    a_sessions = [
        (r.channel_name, r.joined_at, r.left_at)
        for r in rows
        if r.discord_id == user_a
    ]
    b_sessions = [
        (r.channel_name, r.joined_at, r.left_at)
        for r in rows
        if r.discord_id == user_b
    ]
    count = 0
    for ch_a, ja, la in a_sessions:
        for ch_b, jb, lb in b_sessions:
            if ch_a and ch_a == ch_b and ja < lb and jb < la:
                count += 1
    return count


def _unaddressed_streak(ordered, pinger_id):
    """Longest run of consecutive unanswered pings sent by pinger_id.
    A ping from the other side, or one that got addressed, resets the run;
    still-unresolved pings neither extend nor reset it (conservative)."""
    streak = run = 0
    for p in ordered:
        if p.pinger_id == pinger_id:
            if p.addressed is False:
                run += 1
                streak = max(streak, run)
            elif p.addressed is True:
                run = 0
        else:
            run = 0
    return streak


def _name_maps(guild_ids, member_ids):
    """GuildMember is the source of truth for display names — ping snapshots
    go stale the moment someone renames (the bot now pushes renames live via
    /observer/member-name). Returns a per-(guild, member) map plus a
    cross-guild member map for '__all__' rows."""
    if not guild_ids or not member_ids:
        return {}, {}
    rows = GuildMember.query.filter(
        GuildMember.guild_id.in_(guild_ids),
        GuildMember.member_id.in_(member_ids),
    ).all()
    per_guild, global_names = {}, {}
    for r in rows:
        nm = r.display_name or r.name
        if not nm:
            continue
        per_guild[(r.guild_id, r.member_id)] = nm
        global_names.setdefault(r.member_id, nm)
    return per_guild, global_names


def _pair_rows(
    plist,
    a_id,
    b_id,
    guild_id,
    *,
    now,
    window_start,
    active_days,
    total_days,
    baseline_stats,
    voice_guild_ids=None,
    name_map=None,
    global_names=None,
):
    """Build the two directional row dicts for ONE unordered pair.

    Returns dicts (not ORM objects) so both the per-guild recompute and the
    cross-guild read-time path share identical math.
    """
    sample = len(plist)
    latest = max(plist, key=lambda p: p.created_at)

    seven_days_ago = now - timedelta(days=RECENT_WINDOW_DAYS)
    recent = sum(1 for p in plist if p.created_at >= seven_days_ago)
    prior = sample - recent
    last_ping_at = max(p.created_at for p in plist)

    channel_count = len({p.channel_id for p in plist})
    voice = _shared_voice_sessions(voice_guild_ids, a_id, b_id, window_start)
    ordered = sorted(plist, key=lambda p: p.created_at)

    # conversation count: gap > 30 min between consecutive pings starts a
    # new one, so two long talks != 83 drive-bys at the same sample size
    conversations = 1 if ordered else 0
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.created_at - prev.created_at > timedelta(minutes=CONVERSATION_GAP_MINUTES):
            conversations += 1

    # fraction of this pinger's pings that drew a ping back within 24h
    # (directed response; unaddressed_rate also accepts same-channel posts)
    def _return_rate(side_pinger):
        mine = [p.created_at for p in plist if p.pinger_id == side_pinger]
        if not mine:
            return None
        back_times = [p.created_at for p in plist if p.pinger_id != side_pinger]
        horizon = timedelta(hours=RETURN_WINDOW_HOURS)
        returned = sum(1 for t in mine if any(t < o <= t + horizon for o in back_times))
        return round(returned / len(mine), 4)

    affinity = None
    if sample >= MIN_PAIR_SAMPLE:
        days_ab = {p.created_at.date().isoformat() for p in plist}
        p_ab = len(days_ab) / total_days
        p_a = len(active_days.get(a_id, set())) / total_days
        p_b = len(active_days.get(b_id, set())) / total_days
        affinity = npmi(p_ab, p_a, p_b)
        if affinity is not None:
            affinity = round(affinity, 4)

    rows = []
    for pinger_id, pingee_id in ((a_id, b_id), (b_id, a_id)):
        pair_rate = None
        baseline = None
        unaddressed = None
        if sample >= MIN_PAIR_SAMPLE:
            resolved_pair = [
                p
                for p in plist
                if p.pinger_id == pinger_id and p.addressed is not None
            ]
            if resolved_pair:
                pair_rate = sum(1 for p in resolved_pair if not p.addressed) / len(
                    resolved_pair
                )
            stats = baseline_stats.get(pinger_id)
            if stats and stats[0] >= MIN_PAIR_SAMPLE:
                baseline = stats[1] / stats[0]
            unaddressed = corrected_unaddressed_rate(pair_rate, baseline)

        def _name(member_id, snapshot):
            """Fresh GuildMember name; ping snapshot only as last resort."""
            if name_map:
                nm = name_map.get((guild_id, member_id))
                if nm:
                    return nm
                if global_names:
                    g = global_names.get(member_id)
                    if g:
                        return g
            return snapshot

        if pinger_id == latest.pinger_id:
            pinger_name = _name(pinger_id, latest.pinger_name)
            pingee_name = _name(pingee_id, latest.pingee_name)
        else:
            pinger_name = _name(pinger_id, latest.pingee_name)
            pingee_name = _name(pingee_id, latest.pinger_name)

        mine = [p for p in plist if p.pinger_id == pinger_id]
        init_share = round(len(mine) / sample, 3) if sample else None
        delays = [
            (p.first_response_at - p.created_at).total_seconds() / 60.0
            for p in mine
            if p.addressed and p.first_response_at
        ]
        resp_med = round(statistics.median(delays), 1) if delays else None
        streak = _unaddressed_streak(ordered, pinger_id)

        rows.append(
            {
                "guild_id": guild_id,
                "pinger_id": pinger_id,
                "pinger_name": pinger_name,
                "pingee_id": pingee_id,
                "pingee_name": pingee_name,
                "affinity_score": affinity,
                "unaddressed_rate": unaddressed,
                "baseline_unaddressed": baseline,
                "sample_size": sample,
                "recent_pings": recent,
                "prior_pings": prior,
                "last_ping_at": last_ping_at,
                "initiation_share": init_share,
                "median_response_minutes": resp_med,
                "max_unaddressed_streak": streak,
                "channels": channel_count,
                "voice_sessions": voice,
                "conversations": conversations,
                "return_rate": _return_rate(pinger_id),
                "last_computed_at": now,
            }
        )
    return rows


def _group_pairs(pings, cross_guild=False):
    """Group pings by unordered pair — with or without the guild dimension."""
    groups = defaultdict(list)
    for ping in pings:
        key = (min(ping.pinger_id, ping.pingee_id), max(ping.pinger_id, ping.pingee_id))
        if not cross_guild:
            key = (ping.guild_id,) + key
        groups[key].append(ping)
    return groups


# ── Resolver ──

def resolve_pending_pings(now=None):
    """Resolve ping_events.addressed for pings whose window has closed.

    Returns the number of pings finalized this run.
    """
    now = now or datetime.utcnow()
    stale_cutoff = now - timedelta(days=STALE_PING_DAYS)
    pending = PingEvent.query.filter(PingEvent.addressed.is_(None)).all()
    if not pending:
        return 0

    by_target = defaultdict(list)
    for ping in pending:
        by_target[(ping.guild_id, ping.pingee_id)].append(ping)

    resolved = 0
    for (guild_id, pingee_id), pings in by_target.items():
        oldest = min(p.created_at for p in pings)
        refs = (
            MessageRef.query.filter(
                MessageRef.guild_id == guild_id,
                MessageRef.author_id == pingee_id,
                MessageRef.created_at >= oldest,
            )
            .order_by(MessageRef.created_at.asc())
            .all()
        )
        for ping in pings:
            return_at = next(
                (r.created_at for r in refs if r.created_at > ping.created_at),
                None,
            )
            if return_at is None:
                # pingee has not returned to activity yet; only finalize pings
                # that have sat unresolved past the stale horizon
                if ping.created_at < stale_cutoff:
                    ping.addressed = False
                    ping.resolved_at = now
                    resolved += 1
                continue
            ping.return_at = return_at
            deadline = return_at + timedelta(minutes=ADDRESS_WINDOW_MINUTES)
            if now < deadline:
                continue  # still inside the window — leave unresolved

            addressed = False
            first_response_at = None
            # 1) direct reply to the ping's message after returning
            for ref in refs:
                if ref.created_at >= return_at and ref.reply_to_message_id == ping.message_id:
                    addressed = True
                    first_response_at = ref.created_at
                    break
            # 2) mention-back within W minutes of returning
            if not addressed:
                back = PingEvent.query.filter(
                    PingEvent.guild_id == guild_id,
                    PingEvent.pinger_id == pingee_id,
                    PingEvent.pingee_id == ping.pinger_id,
                    PingEvent.created_at >= return_at,
                    PingEvent.created_at <= deadline,
                ).first()
                if back is not None:
                    addressed = True
                    first_response_at = back.created_at
            # 3) post in the ping's channel within their next K messages
            if not addressed:
                next_msgs = [r for r in refs if r.created_at >= return_at][
                    :ADDRESS_MAX_MESSAGES
                ]
                for r in next_msgs:
                    if r.channel_id == ping.channel_id:
                        addressed = True
                        first_response_at = r.created_at
                        break

            ping.addressed = addressed
            ping.first_response_at = first_response_at
            ping.resolved_at = now
            resolved += 1

    db.session.commit()
    return resolved


# ── Batch scorer ──

def recompute_pair_scores(now=None):
    """Rebuild pair_scores from the trailing AFFINITY_WINDOW_DAYS of pings.

    Pairs below MIN_PAIR_SAMPLE get sample_size only, with NULL scores.
    Opted-out members never appear (their pings are dropped at ingest and
    filtered here).
    """
    now = now or datetime.utcnow()
    window_start = now - timedelta(days=AFFINITY_WINDOW_DAYS)
    total_days = AFFINITY_WINDOW_DAYS

    pings = PingEvent.query.filter(PingEvent.created_at >= window_start).all()
    opted_out = opted_out_members()
    pings = [
        p
        for p in pings
        if (p.guild_id, p.pinger_id) not in opted_out
        and (p.guild_id, p.pingee_id) not in opted_out
    ]
    active_days = _active_days(window_start, pings, opted_out=opted_out)
    baseline_stats = _baseline_stats(pings)
    name_map, global_names = _name_maps(
        {p.guild_id for p in pings},
        {p.pinger_id for p in pings} | {p.pingee_id for p in pings},
    )

    rows = []
    for (guild_id, a_id, b_id), plist in _group_pairs(pings).items():
        rows.extend(
            _pair_rows(
                plist,
                a_id,
                b_id,
                guild_id,
                now=now,
                window_start=window_start,
                active_days=active_days,
                total_days=total_days,
                baseline_stats=baseline_stats,
                voice_guild_ids=[guild_id],
                name_map=name_map,
                global_names=global_names,
            )
        )

    # batch rebuild: pair_scores are window-scoped, so stale rows are dropped
    db.session.query(PairScore).delete()
    if rows:
        db.session.add_all([PairScore(**r) for r in rows])
    db.session.commit()
    return {
        "pairs": len(rows) // 2,
        "scored": sum(1 for r in rows if r["affinity_score"] is not None) // 2,
        "window_days": AFFINITY_WINDOW_DAYS,
        "min_sample": MIN_PAIR_SAMPLE,
    }


def cross_guild_pair_rows(guild_ids, now=None):
    """Read-time cross-server aggregation: the same math as the per-guild
    recompute but with pings merged across guilds, so a person's
    relationships follow them across servers. Returns plain dicts — nothing
    is persisted."""
    now = now or datetime.utcnow()
    window_start = now - timedelta(days=AFFINITY_WINDOW_DAYS)
    total_days = AFFINITY_WINDOW_DAYS

    pings = PingEvent.query.filter(
        PingEvent.created_at >= window_start,
        PingEvent.guild_id.in_(guild_ids),
    ).all()
    opted_out = opted_out_members(guild_ids)
    pings = [
        p
        for p in pings
        if (p.guild_id, p.pinger_id) not in opted_out
        and (p.guild_id, p.pingee_id) not in opted_out
    ]
    active_days = _active_days(window_start, pings, guild_ids=guild_ids, opted_out=opted_out)
    baseline_stats = _baseline_stats(pings)
    name_map, global_names = _name_maps(
        set(guild_ids),
        {p.pinger_id for p in pings} | {p.pingee_id for p in pings},
    )

    rows = []
    for (a_id, b_id), plist in _group_pairs(pings, cross_guild=True).items():
        rows.extend(
            _pair_rows(
                plist,
                a_id,
                b_id,
                CROSS_GUILD,
                now=now,
                window_start=window_start,
                active_days=active_days,
                total_days=total_days,
                baseline_stats=baseline_stats,
                voice_guild_ids=guild_ids,
                name_map=name_map,
                global_names=global_names,
            )
        )
    return rows


# ── Per-user behavior metrics ──

def _message_trend(recent, prior):
    """7d vs prior-23d volume trend. None below 3 total messages."""
    if recent + prior < 3:
        return None
    if prior >= FADING_PRIOR_MIN and recent == 0:
        return "fading"
    if recent > prior * 1.5:
        return "rising"
    return "stable"


def _rhythm_shift(recent_times, prior_times):
    """Cosine distance between 24-bin hourly histograms of the recent and
    prior message streams. None below 5 recent messages or empty prior."""
    if len(recent_times) < 5:
        return None

    def hist(times):
        h = [0.0] * 24
        for t in times:
            h[t.hour] += 1.0
        return h

    h1, h2 = hist(recent_times), hist(prior_times)
    n1 = math.sqrt(sum(x * x for x in h1))
    n2 = math.sqrt(sum(x * x for x in h2))
    if n1 == 0 or n2 == 0:
        return None
    dot = sum(a * b for a, b in zip(h1, h2))
    return round(1.0 - dot / (n1 * n2), 4)


def recompute_user_metrics(now=None):
    """Batch-rebuild user_behavior_metrics for the trailing 30-day window.

    Rows cover everyone with message refs in the window, new joiners (week-1
    absorption) and broadcast moderators, even those with no recent messages.
    """
    now = now or datetime.utcnow()
    window_start = now - timedelta(days=AFFINITY_WINDOW_DAYS)
    recent_start = now - timedelta(days=RECENT_WINDOW_DAYS)
    week = timedelta(days=7)

    msgs = defaultdict(list)  # (guild_id, author_id) -> [created_at]
    q = db.session.query(MessageRef.guild_id, MessageRef.author_id, MessageRef.created_at).filter(
        MessageRef.created_at >= window_start
    )
    for guild_id, author_id, created_at in q.all():
        msgs[(guild_id, author_id)].append(created_at)

    voice_hours = {
        (g, m): secs / 3600.0
        for g, m, secs in db.session.query(
            VoiceActivity.guild_id,
            VoiceActivity.discord_id,
            func.coalesce(func.sum(VoiceActivity.duration_seconds), 0.0),
        )
        .filter(VoiceActivity.created_at >= window_start)
        .group_by(VoiceActivity.guild_id, VoiceActivity.discord_id)
        .all()
    }

    broadcasts = {
        (g, m): n
        for g, m, n in db.session.query(
            PingJoinEvent.guild_id,
            PingJoinEvent.moderator_id,
            func.count(),
        )
        .filter(PingJoinEvent.created_at >= window_start)
        .group_by(PingJoinEvent.guild_id, PingJoinEvent.moderator_id)
        .all()
    }

    # first-ever ref per (guild, member), outside the window
    first_seen = {
        (g, m): t
        for g, m, t in db.session.query(
            MessageRef.guild_id,
            MessageRef.author_id,
            func.min(MessageRef.created_at),
        ).group_by(MessageRef.guild_id, MessageRef.author_id).all()
    }

    # week-1 absorption: pings received by joiners in their first 7 days
    joiners = GuildMember.query.filter(
        GuildMember.joined_at.isnot(None),
        GuildMember.joined_at >= window_start,
        GuildMember.is_bot.is_(False),
    ).all()
    absorption = {}
    for jm in joiners:
        pings = PingEvent.query.filter(
            PingEvent.guild_id == jm.guild_id,
            PingEvent.pingee_id == jm.member_id,
            PingEvent.created_at >= jm.joined_at,
            PingEvent.created_at <= jm.joined_at + week,
        ).all()
        names = [p.pinger_name for p in pings if p.pinger_name]
        absorption[(jm.guild_id, jm.member_id)] = (
            len(pings),
            Counter(names).most_common(1)[0][0] if names else None,
        )

    row_keys = set(msgs) | set(absorption) | set(broadcasts)
    opted_out = opted_out_members()
    row_keys = {k for k in row_keys if k not in opted_out}
    name_map, global_names = _name_maps(
        {g for g, _ in row_keys}, {m for _, m in row_keys}
    )
    member_rows = {}
    if row_keys:
        for gm in GuildMember.query.filter(
            GuildMember.guild_id.in_({g for g, _ in row_keys}),
            GuildMember.member_id.in_({m for _, m in row_keys}),
        ).all():
            member_rows[(gm.guild_id, gm.member_id)] = gm

    rows = []
    fading = 0
    new_members = 0
    for guild_id, member_id in row_keys:
        times = msgs.get((guild_id, member_id), [])
        recent = [t for t in times if t >= recent_start]
        prior = [t for t in times if t < recent_start]
        trend = _message_trend(len(recent), len(prior))
        if trend == "fading":
            fading += 1
        gm = member_rows.get((guild_id, member_id))
        if gm and gm.joined_at and gm.joined_at >= window_start:
            new_members += 1
        week1_pings, absorbed_by = absorption.get((guild_id, member_id), (None, None))
        name = (
            name_map.get((guild_id, member_id))
            or global_names.get(member_id)
            or (gm.display_name or gm.name if gm else None)
        )
        rows.append(
            {
                "guild_id": guild_id,
                "discord_id": member_id,
                "name": name,
                "recent_messages": len(recent),
                "prior_messages": len(prior),
                "trend": trend,
                "rhythm_shift": _rhythm_shift(recent, prior),
                "voice_hours_30d": round(voice_hours.get((guild_id, member_id), 0.0), 2),
                "active_days_30d": len({t.date() for t in times}),
                "week1_pings_received": week1_pings,
                "absorbed_by": absorbed_by,
                "broadcast_count_30d": broadcasts.get((guild_id, member_id)),
                "first_seen_at": first_seen.get((guild_id, member_id)) or (gm.joined_at if gm else None),
                "computed_at": now,
            }
        )

    db.session.query(UserBehaviorMetric).delete()
    if rows:
        db.session.add_all([UserBehaviorMetric(**r) for r in rows])
    db.session.commit()
    return {"users": len(rows), "fading": fading, "new_members": new_members}


# ── Voice-only bonds ──

def voice_only_pairs(guild_ids, window_start):
    """Pairs sharing voice sessions in the window with no pair_scores row.
    Returns plain dicts."""
    q = VoiceActivity.query.filter(
        VoiceActivity.joined_at.isnot(None),
        VoiceActivity.left_at.isnot(None),
        VoiceActivity.created_at >= window_start,
    )
    if guild_ids:
        q = q.filter(VoiceActivity.guild_id.in_(guild_ids))
    opted_out = opted_out_members(guild_ids)
    sessions = [
        s for s in q.all() if (s.guild_id, s.discord_id) not in opted_out
    ]

    by_channel = defaultdict(list)
    for s in sessions:
        by_channel[(s.guild_id, s.channel_name)].append(s)

    scored_q = PairScore.query.with_entities(
        PairScore.guild_id, PairScore.pinger_id, PairScore.pingee_id
    )
    if guild_ids:
        scored_q = scored_q.filter(PairScore.guild_id.in_(guild_ids))
    text_pairs = {
        (r.guild_id, frozenset((r.pinger_id, r.pingee_id))) for r in scored_q.all()
    }

    merged = {}  # (guild_id, frozenset(pair)) -> {"a": .., "b": .., "sessions": n, "channels": [..]}
    for (guild_id, _channel), chan_sessions in by_channel.items():
        chan_sessions.sort(key=lambda s: s.joined_at)
        for i, s1 in enumerate(chan_sessions):
            for s2 in chan_sessions[i + 1:]:
                if s2.joined_at >= s1.left_at:
                    break  # sorted by start — no later session can overlap s1
                if s1.discord_id == s2.discord_id:
                    continue
                if s1.joined_at >= s2.left_at:
                    continue
                pair = frozenset((s1.discord_id, s2.discord_id))
                if (guild_id, pair) in text_pairs:
                    continue  # already have text interaction
                entry = merged.setdefault(
                    (guild_id, pair),
                    {"a": s1.discord_id, "b": s2.discord_id, "sessions": 0, "channels": []},
                )
                entry["sessions"] += 1
                if s1.channel_name and s1.channel_name not in entry["channels"]:
                    entry["channels"].append(s1.channel_name)

    return [
        {
            "guild_id": guild_id,
            "a_id": entry["a"],
            "b_id": entry["b"],
            "shared_sessions": entry["sessions"],
            "channel_names": entry["channels"],
        }
        for (guild_id, _pair), entry in merged.items()
    ]
