"""Pairwise interaction & responsiveness profiling — pure helpers, resolver
state machine, and batch scorer. All DB work inside a single app_context
per test (SQLite scratch DB from conftest)."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from bot_core.ping_detect import extract_pings, requires_response
from interactions import (
    MIN_PAIR_SAMPLE,
    corrected_unaddressed_rate,
    npmi,
    recompute_pair_scores,
    resolve_pending_pings,
)


# ── requires_response heuristic ──


def test_requires_response_question_mark():
    assert requires_response("can you send the report?") is True


def test_requires_response_imperative():
    assert requires_response("please check this") is True
    assert requires_response("send it over when you can") is True


def test_requires_response_plain_chat():
    assert requires_response("lol nice one") is False


def test_requires_response_empty_or_missing():
    assert requires_response("") is False
    assert requires_response(None) is False


# ── ping extraction ──


def _message(**kw):
    author = SimpleNamespace(id=111, name="alice", bot=False)
    defaults = dict(
        id=888,
        author=author,
        guild=SimpleNamespace(id=555),
        channel=SimpleNamespace(id=777, name="general"),
        content="hello",
        mention_everyone=False,
        mentions=[],
        reference=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_extract_pings_broadcast_dropped():
    m = _message(
        mention_everyone=True,
        mentions=[SimpleNamespace(id=222, name="bob", bot=False)],
    )
    assert extract_pings(m) == []


def test_extract_pings_reply_to_author():
    ref = SimpleNamespace(
        message_id=999,
        resolved=SimpleNamespace(author=SimpleNamespace(id=222, name="bob", bot=False)),
    )
    pings = extract_pings(_message(reference=ref))
    assert len(pings) == 1
    assert pings[0]["ping_type"] == "reply"
    assert pings[0]["pingee_id"] == "222"
    assert pings[0]["message_id"] == "888"


def test_extract_pings_mentions_skip_bots_and_self():
    m = _message(
        mentions=[
            SimpleNamespace(id=222, name="bob", bot=False),
            SimpleNamespace(id=333, name="somebot", bot=True),
            SimpleNamespace(id=111, name="alice", bot=False),
        ]
    )
    assert [p["pingee_id"] for p in extract_pings(m)] == ["222"]


def test_extract_pings_requires_response_flag_transients():
    m = _message(
        content="can you look at this?",
        mentions=[SimpleNamespace(id=222, name="bob", bot=False)],
    )
    ping = extract_pings(m)[0]
    assert ping["requires_response"] is True
    # no content field ever leaves this module
    assert "content" not in ping


# ── scoring math ──


def test_npmi_independent_is_zero():
    assert abs(npmi(0.2, 0.5, 0.4)) < 1e-9


def test_npmi_bounded_and_directional():
    assert 0 < npmi(0.3, 0.5, 0.4) <= 1.0  # co-occurs above chance
    assert -1.0 <= npmi(0.05, 0.5, 0.4) < 0  # below chance
    assert npmi(0.9, 0.1, 0.1) <= 1.0  # clamp holds


def test_npmi_none_when_zero_input():
    assert npmi(0.0, 0.5, 0.5) is None
    assert npmi(0.2, 0.0, 0.4) is None


def test_corrected_unaddressed_rate_is_deviation():
    assert corrected_unaddressed_rate(0.5, 0.2) == pytest.approx(0.3)
    assert corrected_unaddressed_rate(None, 0.2) is None
    assert corrected_unaddressed_rate(0.5, None) is None


# ── resolver (addressed-window logic) ──

G = "test-guild"


def _ping(a, b, ts, addressed=None, mid="", ch="chan-1", rr=True):
    from database import PingEvent

    return PingEvent(
        guild_id=G,
        pinger_id=a,
        pingee_id=b,
        channel_id=ch,
        channel_name=ch,
        message_id=mid or f"{a}-{b}-{ts.isoformat()}",
        ping_type="mention",
        requires_response=rr,
        addressed=addressed,
        resolved_at=datetime.utcnow() if addressed is not None else None,
        created_at=ts,
    )


def _ref(author, ts, ch="chan-1", reply_to=None, mid=""):
    from database import MessageRef

    return MessageRef(
        guild_id=G,
        channel_id=ch,
        message_id=mid or f"{author}-{ts.isoformat()}",
        author_id=author,
        reply_to_message_id=reply_to,
        created_at=ts,
    )


def test_resolver_same_channel_post_within_k_counts_as_addressed(app):
    with app.app_context():
        from database import PingEvent, db

        now = datetime.utcnow()
        t0 = now - timedelta(minutes=40)
        db.session.add(_ping("A", "B", t0, mid="m1", ch="chan-2"))
        db.session.add(_ref("B", t0 + timedelta(minutes=5)))  # return to activity
        for i in range(3):  # posts back in the ping's channel within K messages
            db.session.add(
                _ref("B", t0 + timedelta(minutes=6 + i), ch="chan-2", mid=f"r{i}")
            )
        db.session.commit()
        resolve_pending_pings()
        row = PingEvent.query.filter_by(guild_id=G, message_id="m1").first()
        assert row.addressed is True and row.resolved_at is not None
        assert row.return_at is not None
        db.session.remove()


def test_resolver_activity_elsewhere_is_unaddressed_after_return(app):
    with app.app_context():
        from database import PingEvent, db

        now = datetime.utcnow()
        t0 = now - timedelta(minutes=60)
        db.session.add(_ping("A", "B", t0, mid="m2", ch="chan-2"))
        db.session.add(
            _ref("B", t0 + timedelta(minutes=5), ch="totally-other")
        )  # returned, but never posts in the ping's channel
        db.session.commit()
        resolve_pending_pings()
        row = PingEvent.query.filter_by(guild_id=G, message_id="m2").first()
        assert row.addressed is False
        db.session.remove()


def test_resolver_direct_reply_counts_even_in_other_channel(app):
    with app.app_context():
        from database import PingEvent, db

        now = datetime.utcnow()
        t0 = now - timedelta(minutes=40)
        db.session.add(_ping("A", "B", t0, mid="pingmsg", ch="chan-2"))
        db.session.add(_ref("B", t0 + timedelta(minutes=5), ch="lounge"))
        db.session.add(
            _ref(
                "B",
                t0 + timedelta(minutes=6),
                ch="lounge",
                reply_to="pingmsg",
                mid="thereply",
            )
        )
        db.session.commit()
        resolve_pending_pings()
        row = PingEvent.query.filter_by(guild_id=G, message_id="pingmsg").first()
        assert row.addressed is True
        db.session.remove()


def test_resolver_leaves_window_open(app):
    with app.app_context():
        from database import PingEvent, db

        now = datetime.utcnow()
        t0 = now - timedelta(minutes=10)
        db.session.add(_ping("A", "B", t0, mid="m3"))
        db.session.add(_ref("B", t0 + timedelta(minutes=5)))  # returned 5 min ago
        db.session.commit()
        resolve_pending_pings()
        row = PingEvent.query.filter_by(guild_id=G, message_id="m3").first()
        assert row.addressed is None  # W=30min window not elapsed yet
        db.session.remove()


# ── batch scorer ──


def test_recompute_counts_all_pings_not_just_asks(app):
    """Widened rule (2026-09-27): every 1:1 ping counts toward affinity,
    not only response-demanding ones."""
    with app.app_context():
        from database import MessageRef, PairScore, db

        now = datetime.utcnow()
        for i in range(MIN_PAIR_SAMPLE):
            day = now - timedelta(days=15 - i)
            db.session.add(
                _ping("A", "B", day, addressed=(i >= 3), rr=False, mid=f"w{i}")
            )
            db.session.add(
                MessageRef(guild_id=G, channel_id="chan-1", message_id=f"wr{i}", author_id="A", created_at=day)
            )
            db.session.add(
                MessageRef(guild_id=G, channel_id="chan-1", message_id=f"wb{i}", author_id="B", created_at=day)
            )
        db.session.commit()
        recompute_pair_scores()
        ab = PairScore.query.filter_by(guild_id=G, pinger_id="A", pingee_id="B").first()
        assert ab.sample_size == MIN_PAIR_SAMPLE
        assert ab.affinity_score is not None and -1 <= ab.affinity_score <= 1
        db.session.remove()


def test_fading_pair_detection(app):
    """Sudden drop-off: prior-period pings with near-silence recently."""
    with app.app_context():
        from database import PairScore, db
        from interactions import FADING_PRIOR_MIN, FADING_RECENT_MAX

        now = datetime.utcnow()
        for i in range(6):  # 6 pings in the prior slice (8-18 days ago)
            db.session.add(
                _ping("A", "B", now - timedelta(days=8 + i * 2), addressed=True, mid=f"f{i}")
            )
        db.session.add(_ping("A", "B", now - timedelta(days=2), addressed=True, mid="fnew"))
        # control pair: still active (pings within the last 6 days)
        for i in range(12):
            db.session.add(
                _ping("C", "D", now - timedelta(days=i % 6), addressed=True, mid=f"a{i}")
            )
        db.session.commit()
        recompute_pair_scores()

        ab = PairScore.query.filter_by(guild_id=G, pinger_id="A", pingee_id="B").first()
        assert ab.recent_pings == 1 and ab.prior_pings == 6
        assert ab.prior_pings >= FADING_PRIOR_MIN and ab.recent_pings <= FADING_RECENT_MAX

        cd = PairScore.query.filter_by(guild_id=G, pinger_id="C", pingee_id="D").first()
        assert not (cd.prior_pings >= FADING_PRIOR_MIN and cd.recent_pings <= FADING_RECENT_MAX)
        db.session.remove()


def test_recompute_insufficient_data_stays_null(app):
    with app.app_context():
        from database import PairScore, PingEvent, db

        now = datetime.utcnow()
        for i in range(MIN_PAIR_SAMPLE - 1):
            db.session.add(
                _ping("C", "D", now - timedelta(days=1), addressed=(i >= 2), mid=f"cd{i}")
            )
        db.session.commit()
        recompute_pair_scores()
        rows = PairScore.query.filter_by(guild_id=G).all()
        assert len(rows) == 2  # both directions
        for row in rows:
            assert row.affinity_score is None
            assert row.unaddressed_rate is None
            assert row.sample_size == MIN_PAIR_SAMPLE - 1
        db.session.remove()


def test_recompute_scores_and_baseline_deviation(app):
    with app.app_context():
        from database import PairScore, db

        now = datetime.utcnow()
        # A -> B: 10 resolved, 3 unaddressed  |  A -> E: 10 resolved, 1 unaddressed
        for i in range(10):
            db.session.add(
                _ping("A", "B", now - timedelta(days=15 - i), addressed=(i >= 3), mid=f"ab{i}")
            )
            db.session.add(
                _ping("A", "E", now - timedelta(days=15 - i), addressed=(i >= 1), mid=f"ae{i}")
            )
            db.session.add(_ref("A", now - timedelta(days=i)))
            db.session.add(_ref("B", now - timedelta(days=i)))
            db.session.add(_ref("E", now - timedelta(days=i)))
        db.session.commit()
        recompute_pair_scores()

        ab = PairScore.query.filter_by(guild_id=G, pinger_id="A", pingee_id="B").first()
        ae = PairScore.query.filter_by(guild_id=G, pinger_id="A", pingee_id="E").first()
        ba = PairScore.query.filter_by(guild_id=G, pinger_id="B", pingee_id="A").first()

        assert ab.sample_size == 10 and ae.sample_size == 10  # per unordered pair
        assert ab.affinity_score is not None and -1 <= ab.affinity_score <= 1
        assert ab.affinity_score == pytest.approx(ba.affinity_score)  # symmetric
        # baseline(A) = (3 + 1) / 20 = 0.2 ; pair(A->B) = 3/10 = 0.3 -> +0.1
        assert ab.baseline_unaddressed == pytest.approx(0.2)
        assert ab.unaddressed_rate == pytest.approx(0.1, abs=1e-6)
        # pair(A->E) = 1/10 = 0.1 -> -0.1
        assert ae.unaddressed_rate == pytest.approx(-0.1, abs=1e-6)
        # B never pinged A: directional unaddressed rate is insufficient
        assert ba.unaddressed_rate is None
        db.session.remove()
