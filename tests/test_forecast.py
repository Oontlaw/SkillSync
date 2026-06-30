"""
Tests for ML forecast pipeline:

- predict_next_24h read-only vs logged behavior
- get_accuracy_metrics guild_id filtering and all-time (days=None) support
- Resolve outcomes with target_start/target_end metadata
- Guild page loads without writing PredictionLog
- Observer forecast endpoint DOES log predictions
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
import pytest

from database import GuildInfo, MessageRecord, PredictionLog, db
from ml.forecast import (
    _hourly_profile_from_30d,
    get_accuracy_metrics,
    predict_next_24h,
    resolve_outcomes,
    train,
)

# ── Fixtures ──


def _seed_messages(app, guild_id, days=35, msgs_per_day=100):
    """Seed MessageRecord rows for a guild over N days.
    Must be called inside an app.app_context().
    """
    now = datetime.utcnow()
    for d in range(days):
        day = now - timedelta(days=d)
        for h in range(24):
            created = day.replace(hour=h, minute=0, second=0, microsecond=0)
            for _ in range(max(1, msgs_per_day // 24)):
                db.session.add(
                    MessageRecord(
                        guild_id=guild_id,
                        discord_id="test-user",
                        channel_name="general",
                        hour_of_day=h,
                        day_of_week=created.weekday(),
                        created_at=created,
                        is_public_channel=True,
                    )
                )
    db.session.commit()


def _seed_forecast_log(app, guild_id, days_ago, daily_pred, actual, run_id=None):
    """Seed a resolved forecast PredictionLog for a guild on a past day."""
    import uuid

    run_id = run_id or str(uuid.uuid4())[:8]
    pred_time = datetime.utcnow() - timedelta(days=days_ago)
    target_start = pred_time.replace(hour=0, minute=0, second=0, microsecond=0)
    hourly = [max(1, daily_pred // 24)] * 24
    # Distribute remainder
    for i in range(daily_pred % 24):
        hourly[i] += 1

    for h in range(24):
        meta = {
            "guild_id": guild_id,
            "predicted_hour": h,
            "prediction_run": run_id,
            "daily_total": daily_pred,
            "target_start": target_start.isoformat(),
            "target_end": (target_start + timedelta(hours=24)).isoformat(),
        }
        entry = PredictionLog(
            model_name="forecast",
            prediction_value=hourly[h],
            metadata_json=json.dumps(meta),
            actual_value=actual,
            prediction_time=pred_time,
        )
        db.session.add(entry)
    db.session.commit()
    return run_id, target_start


# ── Tests ──


def test_predict_next_24h_readonly_does_not_log(app):
    """predict_next_24h with log_prediction=False must not create PredictionLog rows."""
    with app.app_context():
        db.session.add(GuildInfo(guild_id="forecast-test-guild", name="Test Guild"))
        _seed_messages(app, "forecast-test-guild", days=35)
        db.session.commit()

        # Train a model first
        result = train("forecast-test-guild", days=30)
        assert result["status"] == "trained", f"Training failed: {result}"

        count_before = PredictionLog.query.count()
        db.session.commit()

        preds = predict_next_24h("forecast-test-guild", log_prediction=False)

        assert preds is not None, "predict_next_24h returned None"
        assert len(preds) == 24, "Expected 24 hourly predictions"
        assert all(p >= 0 for p in preds), "All predictions must be non-negative"

        count_after = PredictionLog.query.count()
        assert count_after == count_before, (
            f"Read-only prediction created {count_after - count_before} "
            f"PredictionLog rows"
        )


def test_predict_next_24h_logged_creates_logs(app):
    """predict_next_24h with log_prediction=True must create 24 PredictionLog rows."""
    with app.app_context():
        GuildInfo.query.filter_by(guild_id="forecast-test-guild-2").delete()
        db.session.flush()
        db.session.add(GuildInfo(guild_id="forecast-test-guild-2", name="Test Guild 2"))
        _seed_messages(app, "forecast-test-guild-2", days=35)
        db.session.commit()

        result = train("forecast-test-guild-2", days=30)
        assert result["status"] == "trained", f"Training failed: {result}"

        count_before = PredictionLog.query.count()
        db.session.commit()

        preds = predict_next_24h("forecast-test-guild-2", log_prediction=True)

        assert preds is not None
        assert len(preds) == 24

        count_after = PredictionLog.query.count()
        assert count_after == count_before + 24, (
            f"Expected 24 new PredictionLog rows, got {count_after - count_before}"
        )

        # Verify metadata contains target_start/target_end
        new_logs = PredictionLog.query.order_by(PredictionLog.id.desc()).limit(24).all()
        for log in new_logs:
            meta = json.loads(log.metadata_json) if log.metadata_json else {}
            assert "target_start" in meta, "Missing target_start in metadata"
            assert "target_end" in meta, "Missing target_end in metadata"


def test_get_accuracy_metrics_guild_filtering(app):
    """get_accuracy_metrics must filter to a specific guild_id when given."""
    with app.app_context():
        # Seed resolved logs for two different guilds
        _seed_forecast_log(app, "guild-a", days_ago=2, daily_pred=1000, actual=900)
        _seed_forecast_log(app, "guild-b", days_ago=2, daily_pred=500, actual=600)
        db.session.commit()

    with app.app_context():
        # All-guild metrics should have 2 resolved samples
        all_metrics = get_accuracy_metrics(days=30)
        assert all_metrics["samples"] == 2, (
            f"Expected 2 samples across all guilds, got {all_metrics['samples']}"
        )

        # Guild-A-only metrics should have 1 resolved sample
        guild_a_metrics = get_accuracy_metrics(days=30, guild_id="guild-a")
        assert guild_a_metrics["samples"] == 1, (
            f"Expected 1 sample for guild-a, got {guild_a_metrics['samples']}"
        )

        # Guild-B-only metrics should have 1 resolved sample
        guild_b_metrics = get_accuracy_metrics(days=30, guild_id="guild-b")
        assert guild_b_metrics["samples"] == 1, (
            f"Expected 1 sample for guild-b, got {guild_b_metrics['samples']}"
        )

        # Unknown guild should have 0 samples
        unknown_metrics = get_accuracy_metrics(days=30, guild_id="guild-unknown")
        assert unknown_metrics["samples"] == 0, (
            f"Expected 0 samples for unknown guild, got {unknown_metrics['samples']}"
        )


def test_get_accuracy_metrics_all_time(app):
    """get_accuracy_metrics with days=None must return all resolved logs."""
    with app.app_context():
        # Seed old and new resolved logs
        _seed_forecast_log(app, "guild-a", days_ago=60, daily_pred=1000, actual=900)
        _seed_forecast_log(app, "guild-a", days_ago=2, daily_pred=500, actual=600)
        db.session.commit()

    with app.app_context():
        # 7-day window: only 1 sample (the one from 2 days ago)
        recent = get_accuracy_metrics(days=7)
        assert recent["samples"] == 1, (
            f"Expected 1 sample in 7-day window, got {recent['samples']}"
        )

        # All-time (days=None): should have 2 samples
        all_time = get_accuracy_metrics(days=None)
        assert all_time["samples"] == 2, (
            f"Expected 2 samples all-time, got {all_time['samples']}"
        )

        # Combined: guild-specific all-time
        guild_all = get_accuracy_metrics(days=None, guild_id="guild-a")
        assert guild_all["samples"] == 2, (
            f"Expected 2 samples for guild-a all-time, got {guild_all['samples']}"
        )


def test_get_accuracy_metrics_accuracy_value(app):
    """Verify accuracy_pct is computed correctly."""
    with app.app_context():
        # Seed one correct prediction (within 25% threshold)
        _seed_forecast_log(app, "guild-a", days_ago=2, daily_pred=1000, actual=900)
        # Seed one wrong prediction (outside threshold)
        _seed_forecast_log(app, "guild-a", days_ago=3, daily_pred=1000, actual=100)
        db.session.commit()

    with app.app_context():
        # actual=900 vs pred=1000: error=100, threshold=max(900*0.25=225,50)=225 → correct
        # actual=100 vs pred=1000: error=900, threshold=max(100*0.25=25,50)=50 → wrong
        metrics = get_accuracy_metrics(days=None, guild_id="guild-a")
        assert metrics["samples"] == 2
        assert metrics["accuracy_pct"] == 50.0, (
            f"Expected 50% accuracy, got {metrics['accuracy_pct']}"
        )


def test_resolve_outcomes_with_target_window(app):
    """resolve_outcomes must correctly handle target_start/target_end metadata."""
    import uuid

    run_id = str(uuid.uuid4())[:8]
    with app.app_context():
        db.session.add(GuildInfo(guild_id="resolve-test", name="Resolve Test"))
        db.session.commit()

    # Seed messages for a past day
    past_day = datetime.utcnow() - timedelta(days=2)
    target_start = past_day.replace(hour=0, minute=0, second=0, microsecond=0)

    with app.app_context():
        for h in range(24):
            created = target_start + timedelta(hours=h)
            db.session.add(
                MessageRecord(
                    guild_id="resolve-test",
                    discord_id="test-user",
                    channel_name="general",
                    hour_of_day=h,
                    created_at=created,
                    is_public_channel=True,
                )
            )
        db.session.commit()

    # Create unresolved PredictionLog with target_start/target_end
    with app.app_context():
        for h in range(24):
            meta = {
                "guild_id": "resolve-test",
                "predicted_hour": h,
                "prediction_run": run_id,
                "daily_total": 48,
                "target_start": target_start.isoformat(),
                "target_end": (target_start + timedelta(hours=24)).isoformat(),
            }
            entry = PredictionLog(
                model_name="forecast",
                prediction_value=2,
                metadata_json=json.dumps(meta),
                prediction_time=target_start,
            )
            db.session.add(entry)
        db.session.commit()

    with app.app_context():
        resolved = resolve_outcomes()
        assert resolved > 0, "Expected some predictions to be resolved"

        # Verify actual_value was set for logs matching our run_id
        all_logs = PredictionLog.query.filter(
            PredictionLog.model_name == "forecast",
            PredictionLog.actual_value != None,
        ).all()
        resolved_for_run = [
            l
            for l in all_logs
            if json.loads(l.metadata_json or "{}").get("prediction_run") == run_id
        ]
        assert len(resolved_for_run) == 24, (
            f"Expected 24 resolved logs for our run, got {len(resolved_for_run)}"
        )
        assert resolved_for_run[0].actual_value == 24, (
            f"Expected actual_value=24 (24 messages), got {resolved_for_run[0].actual_value}"
        )


def test_hourly_profile_from_30d(app):
    """_hourly_profile_from_30d must return 24 floats summing to 1.0."""
    with app.app_context():
        _seed_messages(app, "profile-test", days=30)
        db.session.commit()

    with app.app_context():
        profile = _hourly_profile_from_30d("profile-test")

    assert profile is not None
    assert len(profile) == 24
    assert abs(sum(profile) - 1.0) < 0.001, (
        f"Hourly profile should sum to ~1.0, got {sum(profile)}"
    )
    assert all(p >= 0 for p in profile), "All profile values must be non-negative"


def test_guild_page_uses_readonly_forecast(app, client):
    """The /guild/<id> route must use log_prediction=False (no new PredictionLogs)."""
    with app.app_context():
        gid = "guild-page-test"
        db.session.add(GuildInfo(guild_id=gid, name="Guild Page Test"))
        _seed_messages(app, gid, days=35)
        db.session.commit()

        train(gid, days=30)
        count_before = PredictionLog.query.count()
        db.session.commit()

    # Log in and hit the guild page
    with client.session_transaction() as sess:
        sess["user"] = {"id": "test-user", "name": "Test User"}
        sess["accessible_guilds"] = [{"id": gid, "name": "Guild Page Test"}]
        sess["_csrf_token"] = "csrf-test"

    resp = client.get(f"/guild/{gid}")
    assert resp.status_code == 200, f"Guild page returned {resp.status_code}"

    with app.app_context():
        count_after = PredictionLog.query.count()
        assert count_after == count_before, (
            f"Guild page created {count_after - count_before} PredictionLog rows"
        )


def test_observer_forecast_endpoint_logs(app, client):
    """The observer /api/observer/ml/forecast/<id> endpoint must log predictions."""
    with app.app_context():
        gid = "observer-forecast-test"
        db.session.add(GuildInfo(guild_id=gid, name="Observer Forecast Test"))
        _seed_messages(app, gid, days=35)
        db.session.commit()

        train(gid, days=30)
        count_before = PredictionLog.query.count()
        db.session.commit()

    resp = client.post(
        f"/api/observer/ml/forecast/{gid}",
        headers={"Authorization": "Bearer test-api-key"},
    )
    assert resp.status_code == 200, f"Observer forecast returned {resp.status_code}"

    with app.app_context():
        count_after = PredictionLog.query.count()
        assert count_after == count_before + 24, (
            f"Expected 24 new PredictionLog rows from observer endpoint, "
            f"got {count_after - count_before}"
        )


def test_accuracy_label_uses_daily_volume(app, client):
    """The guild page must display daily volume accuracy (not hourly)."""
    with app.app_context():
        gid = "label-test"
        db.session.add(GuildInfo(guild_id=gid, name="Label Test"))
        _seed_messages(app, gid, days=35)
        db.session.commit()

        train(gid, days=30)
        # Seed a resolved forecast for this guild
        _seed_forecast_log(app, gid, days_ago=2, daily_pred=1000, actual=950)
        db.session.commit()

    with client.session_transaction() as sess:
        sess["user"] = {"id": "test-user", "name": "Test User"}
        sess["accessible_guilds"] = [{"id": gid, "name": "Label Test"}]
        sess["_csrf_token"] = "csrf-test"

    resp = client.get(f"/guild/{gid}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Must contain the new label
    assert "Daily volume accuracy" in html, (
        "Guild page must show 'Daily volume accuracy' instead of just 'Accuracy'"
    )

    # Must contain sample count
    assert "resolved day" in html, "Guild page must show resolved day count"
    assert "resolved day" in html, "Guild page must show resolved day count"
