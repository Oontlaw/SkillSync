"""Daily behavior-metric rollup — counters, latency percentiles, streaks,
diversity, rhythm/funnel JSON, consent gating, idempotency. Pure arithmetic
over seeded metadata; no content, no ML."""
import json
from datetime import datetime, timedelta

from database import BehaviorMetricDaily, MessageRef, PingEvent, db
from behavior_metrics import (
    _cosine_similarity,
    _percentile,
    _shannon_entropy,
    compute_behavior_metrics,
)

G = "g1"


def _ref(author, ts, ch="c1", mid=""):
    return MessageRef(
        guild_id=G, channel_id=ch, message_id=mid or f"{author}-{ts.isoformat()}",
        author_id=author, created_at=ts,
    )


def _ping(pinger, pingee, ts, req=False, addressed=None, first_resp=None, mid=""):
    return PingEvent(
        guild_id=G, pinger_id=pinger, pingee_id=pingee, channel_id="c1",
        channel_name="c1", message_id=mid or f"p-{pinger}-{pingee}-{ts.isoformat()}",
        ping_type="mention", requires_response=req, addressed=addressed,
        first_response_at=first_resp, created_at=ts,
    )


# ── pure helpers ──


def test_shannon_entropy_bounds():
    assert _shannon_entropy([]) is None
    assert _shannon_entropy([5]) is None
    assert _shannon_entropy([5, 5]) == 1.0  # evenly spread
    assert 0 < _shannon_entropy([9, 1]) < 1.0


def test_percentile_interpolation():
    assert _percentile([], 50) is None
    assert _percentile([10.0], 50) == 10.0
    assert _percentile([10.0, 20.0, 30.0, 40.0], 50) == 25.0
    assert _percentile([0.0, 100.0], 90) == 90.0


def test_cosine_similarity():
    assert _cosine_similarity([0] * 4, [1, 0, 0, 0]) is None
    assert _cosine_similarity([1, 0, 2, 0], [1, 0, 2, 0]) == 1.0
    assert _cosine_similarity([1, 0, 0, 0], [0, 0, 0, 1]) == 0.0


# ── daily rollup ──


def test_daily_counters_and_latency(app):
    with app.app_context():
        now = datetime.utcnow()
        day = (now - timedelta(days=1)).date()
        base = datetime.combine(day, datetime.min.time()) + timedelta(hours=12)
        # 3 messages on the day (one in another channel) + 1 the day before
        db.session.add(_ref("U", base, "c1", mid="m1"))
        db.session.add(_ref("U", base + timedelta(minutes=5), "c2", mid="m2"))
        db.session.add(_ref("U", base + timedelta(minutes=9), "c1", mid="m3"))
        db.session.add(_ref("U", base - timedelta(days=1), "c1", mid="m0"))
        # 2 outgoing pings on the day, one demands a response
        db.session.add(_ping("U", "V", base + timedelta(minutes=1), req=True, mid="pa"))
        db.session.add(_ping("U", "W", base + timedelta(minutes=2), mid="pb"))
        # 1 received ping addressed 10 minutes later
        db.session.add(_ping("X", "U", base, addressed=True,
                             first_resp=base + timedelta(minutes=10), mid="pc"))
        db.session.commit()

        result = compute_behavior_metrics(day=day)
        assert result["users"] >= 1
        row = BehaviorMetricDaily.query.filter_by(guild_id=G, user_id="U", date=day).first()
        assert row.messages == 3
        assert row.pings_sent == 2
        assert row.questions_asked == 1
        assert row.answers_given == 1
        assert row.latency_p50_minutes == 10.0
        assert row.streak_days >= 1
        extra = json.loads(row.extra)
        assert "rhythm_168" in extra and len(extra["rhythm_168"]) == 168
        assert extra["funnel"]["first_message_at"] is not None


def test_streak_counts_consecutive_days(app):
    with app.app_context():
        now = datetime.utcnow()
        day = (now - timedelta(days=1)).date()
        # messages yesterday and the two days before → streak 3
        for back in (0, 1, 2):
            ts = datetime.combine(day, datetime.min.time()) + timedelta(
                hours=10, minutes=back
            ) - timedelta(days=back)
            db.session.add(_ref("S", ts, mid=f"s{back}"))
        db.session.commit()
        compute_behavior_metrics(day=day)
        row = BehaviorMetricDaily.query.filter_by(guild_id=G, user_id="S").first()
        assert row.streak_days == 3


def test_channel_diversity_even_spread_is_one(app):
    with app.app_context():
        now = datetime.utcnow()
        day = (now - timedelta(days=1)).date()
        base = datetime.combine(day, datetime.min.time()) + timedelta(hours=9)
        db.session.add(_ref("D", base, "c1", mid="d1"))
        db.session.add(_ref("D", base + timedelta(minutes=1), "c2", mid="d2"))
        db.session.commit()
        compute_behavior_metrics(day=day)
        row = BehaviorMetricDaily.query.filter_by(guild_id=G, user_id="D").first()
        assert row.channel_diversity == 1.0


def test_behavior_metrics_consent_gate(app):
    with app.app_context():
        from database import GuildMember

        now = datetime.utcnow()
        day = (now - timedelta(days=1)).date()
        db.session.add(GuildMember(guild_id=G, member_id="OUT", name="Out", consent_optin=False))
        db.session.add(_ref("OUT", datetime.combine(day, datetime.min.time()) + timedelta(hours=8), mid="o1"))
        db.session.add(_ref("IN", datetime.combine(day, datetime.min.time()) + timedelta(hours=8), mid="i1"))
        db.session.commit()
        compute_behavior_metrics(day=day)
        assert BehaviorMetricDaily.query.filter_by(user_id="OUT").count() == 0
        assert BehaviorMetricDaily.query.filter_by(user_id="IN").count() == 1


def test_behavior_metrics_idempotent(app):
    with app.app_context():
        now = datetime.utcnow()
        day = (now - timedelta(days=1)).date()
        db.session.add(_ref("R", datetime.combine(day, datetime.min.time()) + timedelta(hours=8), mid="r1"))
        db.session.commit()
        first = compute_behavior_metrics(day=day)
        second = compute_behavior_metrics(day=day)
        assert first["users"] == second["users"]
        assert BehaviorMetricDaily.query.filter_by(date=day).count() == first["users"]


def test_behavior_metrics_endpoint(app, client):
    with app.app_context():
        now = datetime.utcnow()
        day = (now - timedelta(days=1)).date()
        db.session.add(_ref("E", datetime.combine(day, datetime.min.time()) + timedelta(hours=8), mid="e1"))
        db.session.commit()
    auth = {"Authorization": "Bearer test-api-key"}
    resp = client.post("/api/observer/behavior-metrics/compute", json={}, headers=auth)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["users"] >= 1 and body["date"] == day.isoformat()
    # explicit date parameter
    resp = client.post(
        "/api/observer/behavior-metrics/compute",
        json={"date": "not-a-date"}, headers=auth,
    )
    assert resp.status_code == 400
