"""Durable monthly pair snapshots and exponentially decayed edge weights."""

import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy import func

from database import (
    BehaviorMetricDaily,
    MessageRef,
    PairLifetime,
    PairScoresHistory,
    PingEvent,
    db,
)
from interactions import _baseline_stats, _group_pairs, _pair_rows, opted_out_members

EDGE_HALF_LIFE_MONTHS = 6.0


def _month_start(value):
    if isinstance(value, datetime):
        value = value.date()
    return value.replace(day=1)


def _month_window(month, now=None):
    month = _month_start(month)
    if now and (now.year, now.month) == (month.year, month.month):
        end = now
    else:
        next_month = month.replace(
            year=month.year + (month.month == 12),
            month=1 if month.month == 12 else month.month + 1,
        )
        end = datetime.combine(next_month, time.min)
    start = datetime.combine(month, time.min)
    return month, start, end


def _active_days(guild_id, start, end, pings, opted_out):
    active = defaultdict(set)
    daily_query = BehaviorMetricDaily.query.filter(
        BehaviorMetricDaily.guild_id == guild_id,
        BehaviorMetricDaily.date >= start.date(),
    )
    if end.time() == time.min:
        daily_query = daily_query.filter(BehaviorMetricDaily.date < end.date())
    else:
        daily_query = daily_query.filter(BehaviorMetricDaily.date <= end.date())
    for row in daily_query.all():
        if (guild_id, row.user_id) not in opted_out and (
            row.messages or row.pings_sent
        ):
            active[row.user_id].add(row.date.isoformat())
    for (author_id, day) in db.session.query(
        MessageRef.author_id, db.func.date(MessageRef.created_at)
    ).filter(
        MessageRef.guild_id == guild_id,
        MessageRef.created_at >= start,
        MessageRef.created_at < end,
    ).distinct().all():
        if (guild_id, author_id) in opted_out:
            continue
        active[author_id].add(str(day))
    for ping in pings:
        if (guild_id, ping.pinger_id) not in opted_out:
            active[ping.pinger_id].add(ping.created_at.date().isoformat())
    return active


def _month_has_raw_pings(start, end):
    return (
        db.session.query(PingEvent.id)
        .filter(PingEvent.created_at >= start, PingEvent.created_at < end)
        .first()
        is not None
    )


def _finalize_month(month):
    rows = PairScoresHistory.query.filter_by(month=month).all()
    for row in rows:
        row.finalized = True
    return len(rows)


def _snapshot_month(month, now=None, replace_existing=True, finalize=False):
    month, start, end = _month_window(month, now)
    existing = PairScoresHistory.query.filter_by(month=month).all()
    if replace_existing and any(row.finalized for row in existing):
        return {"month": month.isoformat(), "pairs": 0, "skipped": "finalized"}
    pings = PingEvent.query.filter(
        PingEvent.created_at >= start, PingEvent.created_at < end
    ).all()
    if replace_existing and existing and not pings:
        # raw pings were purged; refuse so a direct call cannot delete history
        return {"month": month.isoformat(), "pairs": 0, "skipped": "no_raw_pings"}
    guild_ids = {ping.guild_id for ping in pings}
    opted_out = opted_out_members(guild_ids)
    pings = [
        ping
        for ping in pings
        if (ping.guild_id, ping.pinger_id) not in opted_out
        and (ping.guild_id, ping.pingee_id) not in opted_out
    ]
    elapsed_days = (end.date() - month).days
    month_days = max(1, elapsed_days + (end.time() != time.min))
    as_of = end - timedelta(microseconds=1) if end.time() == time.min else end
    rows = []
    raw_windows = defaultdict(list)

    for guild_id in {ping.guild_id for ping in pings}:
        guild_pings = [ping for ping in pings if ping.guild_id == guild_id]
        active_days = _active_days(
            guild_id, start, end, guild_pings, opted_out
        )
        baseline = _baseline_stats(guild_pings)
        for (pair_guild, a_id, b_id), pair_pings in _group_pairs(guild_pings).items():
            pair_rows = _pair_rows(
                pair_pings,
                a_id,
                b_id,
                pair_guild,
                now=as_of,
                window_start=start,
                active_days=active_days,
                total_days=month_days,
                baseline_stats=baseline,
                voice_guild_ids=[guild_id],
                include_voice=False,
            )
            for row in pair_rows:
                directional = [
                    ping for ping in pair_pings if ping.pinger_id == row["pinger_id"]
                ]
                if not directional:
                    continue
                rows.append(
                    {
                        "guild_id": guild_id,
                        "pinger_id": row["pinger_id"],
                        "pingee_id": row["pingee_id"],
                        "month": month,
                        "affinity_score": row["affinity_score"],
                        "unaddressed_rate": row["unaddressed_rate"],
                        "sample_size": len(directional),
                        "return_rate": row["return_rate"],
                        "median_response_minutes": row["median_response_minutes"],
                        "conversations": row["conversations"],
                    }
                )
                raw_windows[
                    (guild_id, row["pinger_id"], row["pingee_id"])
                ].extend(directional)

    if replace_existing:
        existing_by_key = {
            (row.guild_id, row.pinger_id, row.pingee_id): row for row in existing
        }
        present = set()
        for values in rows:
            key = (values["guild_id"], values["pinger_id"], values["pingee_id"])
            present.add(key)
            row = existing_by_key.get(key)
            if row is None:
                row = PairScoresHistory(**values)
                db.session.add(row)
            else:
                for name, value in values.items():
                    if name != "month":
                        setattr(row, name, value)
            row.finalized = finalize
        for key, row in existing_by_key.items():
            if key not in present:
                db.session.delete(row)

    db.session.flush()
    current_month = _month_start(now.date()) if now else month
    _rebuild_pair_lifetime(current_month, raw_windows)
    db.session.commit()
    return {"month": month.isoformat(), "pairs": len(rows)}


def snapshot_pair_month(month=None, now=None):
    """Upsert a month's snapshot and recompute lifetime weights."""
    now = now or datetime.utcnow()
    return _snapshot_month(month or now.date(), now=now)


def backfill_pair_history(now=None):
    """Backfill months still represented by raw pings; finalize closed ones."""
    now = now or datetime.utcnow()
    months = set()
    for (value,) in db.session.query(func.date(PingEvent.created_at)).distinct():
        event_date = date.fromisoformat(value) if isinstance(value, str) else value
        months.add(_month_start(event_date))
    months.add(_month_start(now.date()))
    updated = 0
    for month in sorted(months):
        if (month.year, month.month) == (now.year, now.month):
            updated += _snapshot_month(month, now=now)["pairs"]
            continue
        rows = PairScoresHistory.query.filter_by(month=month).all()
        if rows and all(row.finalized for row in rows):
            continue
        start, end = _month_window(month, now)[1:]
        if _month_has_raw_pings(start, end):
            # one last snapshot of the just-closed month, then freeze it
            updated += _snapshot_month(month, now=now, finalize=True)["pairs"]
        elif rows:
            _finalize_month(month)
    db.session.commit()
    return updated


def _rebuild_pair_lifetime(current_month, raw_windows):
    history = PairScoresHistory.query.all()
    grouped = defaultdict(list)
    for row in history:
        grouped[(row.guild_id, row.pinger_id, row.pingee_id)].append(row)
    existing_rows = {
        (row.guild_id, row.pinger_id, row.pingee_id): row
        for row in PairLifetime.query.all()
    }
    for key, snapshots in grouped.items():
        lifetime = existing_rows.pop(key, None)
        if lifetime is None:
            lifetime = PairLifetime(
                guild_id=key[0], pinger_id=key[1], pingee_id=key[2]
            )
            db.session.add(lifetime)

        edge_weight = 0.0
        peak = None
        peak_month = None
        for snapshot in snapshots:
            age_months = (
                (current_month.year - snapshot.month.year) * 12
                + current_month.month
                - snapshot.month.month
            )
            if age_months >= 0:
                edge_weight += snapshot.sample_size * math.exp(
                    -math.log(2.0) * age_months / EDGE_HALF_LIFE_MONTHS
                )
            if snapshot.affinity_score is not None and (
                peak is None or snapshot.affinity_score > peak
            ):
                peak = snapshot.affinity_score
                peak_month = snapshot.month

        lifetime.decayed_edge_weight = edge_weight
        lifetime.lifetime_ping_count = sum(row.sample_size for row in snapshots)
        lifetime.peak_affinity = peak
        lifetime.peak_month = peak_month
        raw = raw_windows.get(key, [])
        if raw:
            first = min(ping.created_at for ping in raw)
            last = max(ping.created_at for ping in raw)
            if lifetime.first_interaction_at is None or first < lifetime.first_interaction_at:
                lifetime.first_interaction_at = first
            if lifetime.last_interaction_at is None or last > lifetime.last_interaction_at:
                lifetime.last_interaction_at = last
    for stale in existing_rows.values():
        db.session.delete(stale)
