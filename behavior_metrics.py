"""Daily per-user behavior metrics — computed statistics over metadata the
system already captures. No message content, no ML, no scikit-learn.

compute_behavior_metrics(date): builds one BehaviorMetricDaily row per
(guild, user) active in the trailing window, for the given day. Consent is
enforced via interactions.opted_out_members() — opted-out members produce
zero rows. The batch is idempotent: rows for the target date are deleted
and rebuilt (same lifecycle as pair_scores).

Metrics:
  per-day counters  — messages, pings_sent, questions_asked (pings flagged
                      requires_response), answers_given (received pings the
                      user addressed)
  latency_p50/p90   — minutes from received-ping to first response
  streak_days       — consecutive active days ending on the target date
  cadence_cv        — coefficient of variation of daily message counts
                      (trailing 14 days)
  channel_diversity — normalized Shannon entropy of channel distribution
                      (trailing 7 days)
  voice_hours       — voice seconds on the target day
  extra JSON        — thread engagement (30d), rhythm 7x24 histogram (28d)
                      with cosine similarity vs the prior baseline, and the
                      onboarding funnel (first message / ping / task)
"""
import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func

from database import (
    BehaviorMetricDaily,
    MessageRef,
    PingEvent,
    Task,
    VoiceActivity,
    Worker,
    WorkerIdentity,
    db,
)
from interactions import opted_out_members

RHYTHM_WINDOW_DAYS = 28
THREAD_WINDOW_DAYS = 30
CADENCE_WINDOW_DAYS = 14
DIVERSITY_WINDOW_DAYS = 7


def _shannon_entropy(counts):
    """Normalized Shannon entropy of a count distribution, 0..1 (1 = evenly
    spread). None when there is nothing to distribute."""
    total = sum(counts)
    if total <= 0 or len(counts) <= 1:
        return None
    entropy = -sum(
        (c / total) * math.log2(c / total) for c in counts if c > 0
    )
    return round(entropy / math.log2(len(counts)), 4)


def _cosine_similarity(h1, h2):
    """Cosine similarity between two flat histograms. None when either is
    all zeros."""
    n1 = math.sqrt(sum(x * x for x in h1))
    n2 = math.sqrt(sum(x * x for x in h2))
    if n1 == 0 or n2 == 0:
        return None
    return round(sum(a * b for a, b in zip(h1, h2)) / (n1 * n2), 4)


def _percentile(sorted_values, pct):
    """Linear-interpolated percentile of a sorted list. None when empty."""
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return round(sorted_values[lo], 1)
    return round(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo), 1)


def compute_behavior_metrics(day=None, now=None):
    """Compute the daily behavior-metric rollup for `day` (default: yesterday).

    Returns {"users": <rows written>, "date": iso}. Consent-gated: members
    who opted out of profiling never appear.
    """
    now = now or datetime.utcnow()
    target_date = day or (now - timedelta(days=1)).date()
    day_start = datetime(target_date.year, target_date.month, target_date.day)
    day_end = day_start + timedelta(days=1)
    window_start = now - timedelta(days=RHYTHM_WINDOW_DAYS)

    # ── message refs: everything message-derived, grouped per (guild, author)
    ref_rows = (
        db.session.query(
            MessageRef.guild_id,
            MessageRef.author_id,
            MessageRef.channel_id,
            MessageRef.message_id,
            MessageRef.reply_to_message_id,
            MessageRef.created_at,
        )
        .filter(MessageRef.created_at >= window_start)
        .all()
    )
    opted_out = opted_out_members()
    refs_by_user = defaultdict(list)
    author_of_ref = {}  # (guild, message_id) -> author, for reply attribution
    for guild_id, author_id, channel_id, message_id, reply_to, created_at in ref_rows:
        if (guild_id, author_id) in opted_out:
            continue
        refs_by_user[(guild_id, author_id)].append((channel_id, created_at))
        author_of_ref[(guild_id, message_id)] = author_id

    # ── ping events
    ping_rows = (
        db.session.query(
            PingEvent.guild_id,
            PingEvent.pinger_id,
            PingEvent.pingee_id,
            PingEvent.requires_response,
            PingEvent.addressed,
            PingEvent.created_at,
            PingEvent.first_response_at,
        )
        .filter(PingEvent.created_at >= window_start)
        .all()
    )
    pings_by_user = defaultdict(list)   # pinger: all outgoing pings
    received_by_user = defaultdict(list)  # pingee: incoming resolved pings
    for (guild_id, pinger, pingee, req_resp, addressed, created, first_resp) in ping_rows:
        if (guild_id, pinger) in opted_out or (guild_id, pingee) in opted_out:
            continue
        pings_by_user[(guild_id, pinger)].append((req_resp, created))
        if addressed is not None:
            received_by_user[(guild_id, pingee)].append((addressed, created, first_resp))

    # ── voice seconds per (guild, user) on the target day
    voice_rows = {
        (g, u): secs
        for g, u, secs in db.session.query(
            VoiceActivity.guild_id,
            VoiceActivity.discord_id,
            func.coalesce(func.sum(VoiceActivity.duration_seconds), 0.0),
        )
        .filter(
            VoiceActivity.created_at >= day_start,
            VoiceActivity.created_at < day_end,
        )
        .group_by(VoiceActivity.guild_id, VoiceActivity.discord_id)
        .all()
    }

    # ── onboarding: first-ever activity timestamps per user
    first_message = {
        (g, a): t
        for g, a, t in db.session.query(
            MessageRef.guild_id, MessageRef.author_id, func.min(MessageRef.created_at)
        ).group_by(MessageRef.guild_id, MessageRef.author_id).all()
    }
    first_ping = {
        (g, a): t
        for g, a, t in db.session.query(
            PingEvent.guild_id, PingEvent.pinger_id, func.min(PingEvent.created_at)
        ).group_by(PingEvent.guild_id, PingEvent.pinger_id).all()
    }
    first_task = dict(
        db.session.query(
            WorkerIdentity.discord_id, func.min(Task.completed_at)
        )
        .join(Worker, Task.worker_id == Worker.id)
        .filter(Worker.discord_id == WorkerIdentity.discord_id, Task.completed_at.isnot(None))
        .group_by(WorkerIdentity.discord_id)
        .all()
    )

    # ── thread engagement over the window: who starts threads (their
    # messages get replied to) vs who replies in others' threads
    replies_to = defaultdict(set)   # author -> set of authors who replied to them
    replied_by = defaultdict(set)   # author -> set of authors they replied to
    for guild_id, author_id, _ch, _mid, reply_to, _created in ref_rows:
        if not reply_to:
            continue
        target = author_of_ref.get((guild_id, reply_to))
        if not target or target == author_id:
            continue
        replied_by[(guild_id, author_id)].add(target)
        replies_to[(guild_id, target)].add(author_id)

    row_keys = (
        set(refs_by_user) | set(pings_by_user) | set(received_by_user)
    )
    rows = []
    for key in row_keys:
        guild_id, user_id = key
        refs = refs_by_user.get(key, [])

        # per-day counters
        day_refs = [c for (_ch, c) in refs if day_start <= c < day_end]
        out_pings = [
            (req, c) for (req, c) in pings_by_user.get(key, []) if day_start <= c < day_end
        ]
        in_pings = [
            (addr, c, fr)
            for (addr, c, fr) in received_by_user.get(key, [])
            if day_start <= c < day_end
        ]

        # latency percentiles on addressed pings received that day
        latencies = sorted(
            (fr - c).total_seconds() / 60.0
            for (addr, c, fr) in in_pings
            if addr and fr
        )

        # streak: consecutive active days ending on the target date
        active_days = {c.date() for (_ch, c) in refs}
        streak = 0
        cursor = target_date
        while cursor in active_days:
            streak += 1
            cursor -= timedelta(days=1)

        # cadence: coefficient of variation over the trailing 14 days
        cadence_counts = [
            sum(1 for (_ch, c) in refs if (now - timedelta(days=i)).date() == c.date())
            for i in range(CADENCE_WINDOW_DAYS)
        ]
        mean = sum(cadence_counts) / CADENCE_WINDOW_DAYS
        if mean > 0:
            variance = sum((c - mean) ** 2 for c in cadence_counts) / CADENCE_WINDOW_DAYS
            cadence_cv = round(math.sqrt(variance) / mean, 4)
        else:
            cadence_cv = None

        # channel diversity over the trailing 7 days
        chan_counts = defaultdict(int)
        for ch, c in refs:
            if c >= now - timedelta(days=DIVERSITY_WINDOW_DAYS):
                chan_counts[ch or "unknown"] += 1
        diversity = _shannon_entropy(list(chan_counts.values()))

        # rhythm 7x24 (dow-major) + cosine similarity vs own prior baseline
        rhythm = [0.0] * (7 * 24)
        recent_hist = [0.0] * (7 * 24)
        prior_hist = [0.0] * (7 * 24)
        for _ch, c in refs:
            slot = c.weekday() * 24 + c.hour
            rhythm[slot] += 1
            if c >= now - timedelta(days=7):
                recent_hist[slot] += 1
            else:
                prior_hist[slot] += 1

        # onboarding funnel (first-ever timestamps)
        fm = first_message.get(key)
        fp = first_ping.get(key)
        ft = first_task.get(user_id)
        days_to_task = (
            round((ft - fm).total_seconds() / 86400.0, 2)
            if fm and ft and ft >= fm
            else None
        )

        extra = json.dumps(
            {
                "threads_started": len(replies_to.get(key, set())),
                "replied_to_distinct_peers": sorted(replies_to.get(key, set())),
                "replies_to_distinct_peers": sorted(replied_by.get(key, set())),
                "rhythm_168": [int(x) for x in rhythm],
                "rhythm_similarity_recent_vs_prior": _cosine_similarity(
                    recent_hist, prior_hist
                ),
                "funnel": {
                    "first_message_at": fm.isoformat() if fm else None,
                    "first_ping_sent_at": fp.isoformat() if fp else None,
                    "first_task_completed_at": ft.isoformat() if ft else None,
                    "days_to_first_task": days_to_task,
                },
            }
        )

        rows.append(
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "date": target_date,
                "messages": len(day_refs),
                "pings_sent": len(out_pings),
                "questions_asked": sum(1 for req, _c in out_pings if req),
                "answers_given": sum(1 for addr, _c, _fr in in_pings if addr),
                "latency_p50_minutes": _percentile(latencies, 50),
                "latency_p90_minutes": _percentile(latencies, 90),
                "streak_days": streak,
                "cadence_cv": cadence_cv,
                "channel_diversity": diversity,
                "voice_hours": round(voice_rows.get(key, 0.0) / 3600.0, 3),
                "extra": extra,
                "computed_at": now,
            }
        )

    # idempotent rebuild for the target date
    db.session.query(BehaviorMetricDaily).filter(
        BehaviorMetricDaily.date == target_date
    ).delete()
    if rows:
        db.session.add_all([BehaviorMetricDaily(**r) for r in rows])
    db.session.commit()
    return {"users": len(rows), "date": target_date.isoformat()}
