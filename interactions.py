"""Pairwise interaction statistics — computed statistics, not learned models.

resolve_pending_pings(): resolves ping_events.addressed per the
addressed-window logic — after the pingee's return to activity, the ping
counts as addressed if the pingee (1) replies to the ping's message,
(2) mentions the pinger back within W minutes of returning, or (3) posts in
the same channel within their next K messages after returning. Otherwise,
once W minutes of activity pass without any condition, the ping is marked
False — i.e. unaddressed_after_return. General activity elsewhere never
resolves a ping.

recompute_pair_scores(): batch-rebuilds pair_scores from ping_events.
Since 2026-09-27 (user directive) affinity and unaddressed rates cover EVERY
directed 1:1 ping — reply or mention, question or not — so the graph reflects
how people actually chat, not just explicit asks. requires_response is still
recorded per ping for future re-tightening. affinity_score is NPMI
(normalized pointwise mutual information) over daily activity buckets — never
raw interaction counts. unaddressed_rate is the pair's unaddressed rate minus
the pinger's own baseline unaddressed rate across all pingees (a deviation,
not an absolute rate; no intent is ever claimed). Each pair also gets
sudden-drop telemetry: pings in the last 7 days vs the prior 23 days and the
last-ping timestamp, so the admin view can flag pairs that used to interact
heavily and went quiet.

No scikit-learn here by design (spec non-goal) — pure arithmetic.
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func

from database import MessageRef, PairScore, PingEvent, db

# ── Tunables ──
ADDRESS_WINDOW_MINUTES = 30  # W: ping resolves once pingee has been active this long
ADDRESS_MAX_MESSAGES = 5  # K: same-channel post within pingee's next K messages counts
AFFINITY_WINDOW_DAYS = 30  # trailing window for the PMI day-buckets
MIN_PAIR_SAMPLE = 10  # N: minimum pings before any score is surfaced
STALE_PING_DAYS = 14  # pings older than this with no pingee return resolve as unaddressed
RECENT_WINDOW_DAYS = 7  # drift: "recent" slice of the scoring window
FADING_PRIOR_MIN = 5  # drift: pair counted "fading" if it had at least this many pings
FADING_RECENT_MAX = 1  # drift: ...in the prior slice but at most this many recently


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
            # 1) direct reply to the ping's message after returning
            for ref in refs:
                if ref.created_at >= return_at and ref.reply_to_message_id == ping.message_id:
                    addressed = True
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
                addressed = back is not None
            # 3) post in the ping's channel within their next K messages
            if not addressed:
                next_msgs = [r for r in refs if r.created_at >= return_at][
                    :ADDRESS_MAX_MESSAGES
                ]
                addressed = any(r.channel_id == ping.channel_id for r in next_msgs)

            ping.addressed = addressed
            ping.resolved_at = now
            resolved += 1

    db.session.commit()
    return resolved


# ── Batch scorer ──

def recompute_pair_scores(now=None):
    """Rebuild pair_scores from the trailing AFFINITY_WINDOW_DAYS of
    qualifying pings (requires_response only, per the spec filter).

    Pairs below MIN_PAIR_SAMPLE get sample_size only, with NULL scores —
    insufficient_data, never a computed number.
    """
    now = now or datetime.utcnow()
    window_start = now - timedelta(days=AFFINITY_WINDOW_DAYS)
    total_days = AFFINITY_WINDOW_DAYS

    pings = PingEvent.query.filter(
        PingEvent.created_at >= window_start,
    ).all()

    # distinct active days per author: refs (all messaging) plus the days a
    # user sent qualifying pings (a ping is activity by the pinger)
    active_days = defaultdict(set)
    day_rows = (
        db.session.query(MessageRef.author_id, func.date(MessageRef.created_at))
        .filter(MessageRef.created_at >= window_start)
        .distinct()
        .all()
    )
    for author_id, day in day_rows:
        active_days[author_id].add(str(day))
    for ping in pings:
        active_days[ping.pinger_id].add(ping.created_at.date().isoformat())

    # canonical (unordered) pair grouping for affinity
    pair_pings = defaultdict(list)
    for ping in pings:
        key = (
            ping.guild_id,
            min(ping.pinger_id, ping.pingee_id),
            max(ping.pinger_id, ping.pingee_id),
        )
        pair_pings[key].append(ping)

    # pinger baseline: unaddressed rate across ALL pingees (resolved only)
    baseline_stats = defaultdict(lambda: [0, 0])  # [resolved, unaddressed]
    for ping in pings:
        if ping.addressed is not None:
            baseline_stats[ping.pinger_id][0] += 1
            if not ping.addressed:
                baseline_stats[ping.pinger_id][1] += 1

    rows = []
    for (guild_id, a_id, b_id), plist in pair_pings.items():
        sample = len(plist)
        latest = max(plist, key=lambda p: p.created_at)

        # sudden-drop telemetry: last 7 days vs the prior 23 days of the window
        seven_days_ago = now - timedelta(days=RECENT_WINDOW_DAYS)
        recent = sum(1 for p in plist if p.created_at >= seven_days_ago)
        prior = sample - recent
        last_ping_at = max(p.created_at for p in plist)

        affinity = None
        if sample >= MIN_PAIR_SAMPLE:
            days_ab = {p.created_at.date().isoformat() for p in plist}
            p_ab = len(days_ab) / total_days
            p_a = len(active_days.get(a_id, set())) / total_days
            p_b = len(active_days.get(b_id, set())) / total_days
            affinity = npmi(p_ab, p_a, p_b)
            if affinity is not None:
                affinity = round(affinity, 4)

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
                    pair_rate = sum(
                        1 for p in resolved_pair if not p.addressed
                    ) / len(resolved_pair)
                stats = baseline_stats.get(pinger_id)
                if stats and stats[0] >= MIN_PAIR_SAMPLE:
                    baseline = stats[1] / stats[0]
                unaddressed = corrected_unaddressed_rate(pair_rate, baseline)

            if pinger_id == latest.pinger_id:
                pinger_name, pingee_name = latest.pinger_name, latest.pingee_name
            else:
                pinger_name, pingee_name = latest.pingee_name, latest.pinger_name
            rows.append(
                PairScore(
                    guild_id=guild_id,
                    pinger_id=pinger_id,
                    pinger_name=pinger_name,
                    pingee_id=pingee_id,
                    pingee_name=pingee_name,
                    affinity_score=affinity,
                    unaddressed_rate=unaddressed,
                    baseline_unaddressed=baseline,
                    sample_size=sample,
                    recent_pings=recent,
                    prior_pings=prior,
                    last_ping_at=last_ping_at,
                    last_computed_at=now,
                )
            )

    # batch rebuild: pair_scores are window-scoped, so stale rows are dropped
    db.session.query(PairScore).delete()
    if rows:
        db.session.add_all(rows)
    db.session.commit()
    return {
        "pairs": len(rows),
        "scored": sum(1 for r in rows if r.affinity_score is not None),
        "window_days": AFFINITY_WINDOW_DAYS,
        "min_sample": MIN_PAIR_SAMPLE,
    }
