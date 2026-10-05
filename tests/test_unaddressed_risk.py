"""Unaddressed-ping risk model — leakage-safe feature construction and
honest training on synthetic data with a known signal."""
from datetime import datetime, timedelta

import pytest

from database import PingEvent, db
from ml import unaddressed

G = "rg"


def _seed_structured_pings(n_per=40):
    """Pinger 'BAD' goes unaddressed ~40% of the time (late-night pings),
    pinger 'OK' almost never. The signal is learnable."""
    now = datetime.utcnow()
    rows = []
    mid = 0
    for pinger, partner, un_rate in (("BAD", "V", 0.4), ("OK", "W", 0.05)):
        for i in range(n_per):
            ts = now - timedelta(days=20, hours=i * 2)
            midnightish = ts.hour >= 23 or ts.hour <= 4
            unaddressed_row = midnightish if pinger == "BAD" else False
            rows.append(PingEvent(
                guild_id=G, pinger_id=pinger, pingee_id=partner,
                channel_id="c", channel_name="c", message_id=f"m{mid}",
                ping_type="mention" if mid % 2 else "reply",
                requires_response=bool(mid % 3 == 0),
                addressed=not unaddressed_row,
                resolved_at=now, created_at=ts,
            ))
            mid += 1
    db.session.add_all(rows)
    db.session.commit()


def test_feature_builder_is_chronological_and_leakage_safe(app):
    """The feature row for ping i must only use pings BEFORE i: the last
    ping's pinger_prior_count must be n-1, not n."""
    with app.app_context():
        _seed_structured_pings(n_per=30)
        X, y, n_pos = unaddressed._build_training_data(days=60)
        assert len(y) == 60
        assert n_pos >= unaddressed.MIN_POSITIVES
        # cumulative counters: max pinger_prior_count < total per pinger
        pinger_col = 1
        assert X[:, pinger_col].max() < 30
        # first ping of the window has zero history
        assert X[0, pinger_col] == 0 and X[0, 0] == 0.0 and X[0, 3] == 0.0


def test_train_persists_model_and_honest_metrics(app, monkeypatch, tmp_path):
    with app.app_context():
        _seed_structured_pings(n_per=40)
        model_path = str(tmp_path / "unaddressed_risk.joblib")
        monkeypatch.setattr(unaddressed, "RISK_MODEL_PATH", model_path)
        result = unaddressed.train(days=60)
        assert result["status"] == "trained"
        assert result["positives"] >= unaddressed.MIN_POSITIVES
        ap = result["cv_average_precision"]
        assert ap is not None and 0.0 <= ap <= 1.0

        stats = unaddressed.get_stats()
        assert stats["trained"] is True and stats["version"] == 1
        assert stats["n_samples"] == result["samples"]


def test_train_skips_on_insufficient_data(app, monkeypatch, tmp_path):
    with app.app_context():
        monkeypatch.setattr(
            unaddressed, "RISK_MODEL_PATH", str(tmp_path / "x.joblib")
        )
        now = datetime.utcnow()
        db.session.add(PingEvent(
            guild_id=G, pinger_id="a", pingee_id="b", channel_id="c",
            channel_name="c", message_id="solo", ping_type="mention",
            addressed=False, resolved_at=now, created_at=now,
        ))
        db.session.commit()
        result = unaddressed.train(days=30)
        assert result["status"] == "skipped"
