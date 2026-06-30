"""
Tests for ML forecast pipeline:

- predict_next_24h read-only vs logged behavior
- get_accuracy_metrics guild_id filtering and all-time (days=None) support
- Resolve outcomes with target_start/target_end metadata
- Guild page loads without writing PredictionLog
- Observer forecast endpoint DOES log predictions
- Daily over-prediction/under-prediction fails, close prediction passes
- Hourly accuracy output is separate from daily accuracy output
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
    """Seed a resolved forecast PredictionLog for a guild on a past day.

    Distributes both predictions and actuals across 24 hours so each
    hourly PredictionLog row gets its matching hour's counts.
    """
    import uuid

    run_id = run_id or str(uuid.uuid4())[:8]
    pred_time = datetime.utcnow() - timedelta(days=days_ago)
    target_start = pred_time.replace(hour=0, minute=0, second=0, microsecond=0)

    # Distribute predictions across hours
    pred_hourly = [max(1, daily_pred // 24)] * 24
    for i in range(daily_pred % 24):
        pred_hourly[i] += 1

    # Distribute actuals across hours
    actual_hourly = [max(1, actual // 24)] * 24
    for i in range(actual % 24):
        actual_hourly[i] += 1

    for h in range(24):
        meta = {
            "guild_id": guild_id,
            "predicted_hour": h,
            "prediction_run": run_id,
            "daily_total": daily_pred,
            "target_start": target_start.isoformat(),
            "target_end": (target_start + timedelta(hours=24)).isoformat(),
            "resolution_version": 2,
            "actual_granularity": "hourly",
        }
        entry = PredictionLog(
            model_name="forecast",
            prediction_value=pred_hourly[h],
            features_json=json.dumps({"hour": h, "daily_prediction": daily_pred}),
            metadata_json=json.dumps(meta),
            actual_value=actual_hourly[h],
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
        # All-guild metrics should have 2 daily resolved samples
        all_metrics = get_accuracy_metrics(days=30)
        assert all_metrics["daily"]["samples"] == 2, (
            f"Expected 2 daily samples across all guilds, got {all_metrics['daily']['samples']}"
        )

        # Guild-A-only metrics should have 1 daily sample
        guild_a_metrics = get_accuracy_metrics(days=30, guild_id="guild-a")
        assert guild_a_metrics["daily"]["samples"] == 1, (
            f"Expected 1 daily sample for guild-a, got {guild_a_metrics['daily']['samples']}"
        )

        # Guild-B-only metrics should have 1 daily sample
        guild_b_metrics = get_accuracy_metrics(days=30, guild_id="guild-b")
        assert guild_b_metrics["daily"]["samples"] == 1, (
            f"Expected 1 daily sample for guild-b, got {guild_b_metrics['daily']['samples']}"
        )

        # Unknown guild should have 0 daily samples
        unknown_metrics = get_accuracy_metrics(days=30, guild_id="guild-unknown")
        assert unknown_metrics["daily"]["samples"] == 0, (
            f"Expected 0 daily samples for unknown guild, got {unknown_metrics['daily']['samples']}"
        )


def test_get_accuracy_metrics_all_time(app):
    """get_accuracy_metrics with days=None must return all resolved logs."""
    with app.app_context():
        # Seed old and new resolved logs
        _seed_forecast_log(app, "guild-a", days_ago=60, daily_pred=1000, actual=900)
        _seed_forecast_log(app, "guild-a", days_ago=2, daily_pred=500, actual=600)
        db.session.commit()

    with app.app_context():
        # 7-day window: only 1 daily sample (the one from 2 days ago)
        recent = get_accuracy_metrics(days=7)
        assert recent["daily"]["samples"] == 1, (
            f"Expected 1 daily sample in 7-day window, got {recent['daily']['samples']}"
        )

        # All-time (days=None): should have 2 daily samples
        all_time = get_accuracy_metrics(days=None)
        assert all_time["daily"]["samples"] == 2, (
            f"Expected 2 daily samples all-time, got {all_time['daily']['samples']}"
        )

        # Combined: guild-specific all-time
        guild_all = get_accuracy_metrics(days=None, guild_id="guild-a")
        assert guild_all["daily"]["samples"] == 2, (
            f"Expected 2 daily samples for guild-a all-time, got {guild_all['daily']['samples']}"
        )


def test_get_accuracy_metrics_accuracy_value(app):
    """Verify daily and hourly accuracy_pct are computed correctly."""
    with app.app_context():
        # Seed one correct daily prediction: error=100, tolerance=max(900*0.15=135,25)=135
        _seed_forecast_log(app, "guild-a", days_ago=2, daily_pred=1000, actual=900)
        # Seed one wrong daily prediction: error=900, tolerance=max(100*0.15=15,25)=25
        _seed_forecast_log(app, "guild-a", days_ago=3, daily_pred=1000, actual=100)
        db.session.commit()

    with app.app_context():
        # Daily: 1/2 = 50%
        metrics = get_accuracy_metrics(days=None, guild_id="guild-a")
        assert metrics["daily"]["samples"] == 2
        assert metrics["daily"]["accuracy_pct"] == 50.0, (
            f"Expected 50% daily accuracy, got {metrics['daily']['accuracy_pct']}"
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
        # Each hour had exactly 1 message, so each hourly log gets actual_value=1
        assert resolved_for_run[0].actual_value == 1, (
            f"Expected actual_value=1 (1 message in that hour), got {resolved_for_run[0].actual_value}"
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


def test_daily_over_prediction_fails(app):
    """Over-predicting by a large margin should be marked as incorrect."""
    with app.app_context():
        _seed_forecast_log(app, "guild-over", days_ago=2, daily_pred=2000, actual=500)
        db.session.commit()

    with app.app_context():
        metrics = get_accuracy_metrics(days=None, guild_id="guild-over")
        assert metrics["daily"]["samples"] == 1
        # tolerance = max(500*0.15=75,25)=75, error=1500 > 75 -> wrong
        assert metrics["daily"]["accuracy_pct"] == 0.0, (
            f"Over-prediction should be incorrect, got {metrics['daily']['accuracy_pct']}"
        )


def test_daily_under_prediction_fails(app):
    """Under-predicting by a large margin should be marked as incorrect."""
    with app.app_context():
        _seed_forecast_log(app, "guild-under", days_ago=2, daily_pred=100, actual=1000)
        db.session.commit()

    with app.app_context():
        metrics = get_accuracy_metrics(days=None, guild_id="guild-under")
        assert metrics["daily"]["samples"] == 1
        # tolerance = max(1000*0.15=150,25)=150, error=900 > 150 -> wrong
        assert metrics["daily"]["accuracy_pct"] == 0.0, (
            f"Under-prediction should be incorrect, got {metrics['daily']['accuracy_pct']}"
        )


def test_daily_close_prediction_passes(app):
    """A close prediction within tolerance should be marked as correct."""
    with app.app_context():
        _seed_forecast_log(app, "guild-close", days_ago=2, daily_pred=480, actual=500)
        db.session.commit()

    with app.app_context():
        metrics = get_accuracy_metrics(days=None, guild_id="guild-close")
        assert metrics["daily"]["samples"] == 1
        # tolerance = max(500*0.15=75,25)=75, error=20 <= 75 -> correct
        assert metrics["daily"]["accuracy_pct"] == 100.0, (
            f"Close prediction should be correct, got {metrics['daily']['accuracy_pct']}"
        )


def test_hourly_accuracy_separate_from_daily(app):
    """Verify output dict has both hourly and daily keys with separate values."""
    with app.app_context():
        _seed_forecast_log(app, "guild-h", days_ago=2, daily_pred=1000, actual=900)
        db.session.commit()

    with app.app_context():
        metrics = get_accuracy_metrics(days=None, guild_id="guild-h")
        # Both top-level keys present
        assert "daily" in metrics
        assert "hourly" in metrics
        # Daily keys
        assert "samples" in metrics["daily"]
        assert "accuracy_pct" in metrics["daily"]
        assert "mean_absolute_error" in metrics["daily"]
        # Hourly keys
        assert "samples" in metrics["hourly"]
        assert "accuracy_pct" in metrics["hourly"]
        assert "mean_absolute_error" in metrics["hourly"]
        assert "worst_hours" in metrics["hourly"]
        # Daily = 1 run, Hourly = 24 rows
        assert metrics["daily"]["samples"] == 1, (
            f"Expected 1 daily sample, got {metrics['daily']['samples']}"
        )
        assert metrics["hourly"]["samples"] == 24, (
            f"Expected 24 hourly samples, got {metrics['hourly']['samples']}"
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

    # Must contain sample count (newline in template splits "resolved" and "day")
    assert "1 resolved" in html, (
        "Guild page must show resolved day count, got html section: "
        + repr(html[html.find("Daily volume accuracy") :][:200])
    )


def test_target_window_is_next_calendar_day(app):
    """_log_forecast_predictions must set target_start to the next calendar
    day midnight (today_midnight + 1 day), not the current day's midnight."""
    with app.app_context():
        gid = "target-window-test"
        db.session.add(GuildInfo(guild_id=gid, name="Target Window Test"))
        _seed_messages(app, gid, days=35)
        db.session.commit()

        train(gid, days=30)
        db.session.commit()

        # Log predictions
        preds = predict_next_24h(gid, log_prediction=True)
        assert preds is not None

        # Read back the logged predictions
        logs = (
            PredictionLog.query.filter(
                PredictionLog.model_name == "forecast",
            )
            .order_by(PredictionLog.id.desc())
            .limit(24)
            .all()
        )
        assert len(logs) == 24

        for log in logs:
            meta = json.loads(log.metadata_json) if log.metadata_json else {}
            target_start = meta.get("target_start")
            assert target_start is not None, "Missing target_start"
            ts = datetime.fromisoformat(target_start)
            now = datetime.utcnow()
            today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            expected = today_midnight + timedelta(days=1)
            # Allow 1-second tolerance for clock skew
            diff = abs((ts - expected).total_seconds())
            assert diff < 60, (
                f"target_start {ts} should be ~{expected} (next midnight), diff={diff}s"
            )


def test_legacy_logs_excluded_from_v2_metrics(app):
    """Logs without resolution_version=2 must be excluded from
    get_accuracy_metrics and _build_error_profile computations."""
    import uuid

    with app.app_context():
        gid = "legacy-test"
        db.session.add(GuildInfo(guild_id=gid, name="Legacy Test"))
        db.session.commit()

        # Create a legacy-style resolved set of 24 hourly logs WITHOUT
        # resolution_version: 2 — each row stores the daily total as actual_value
        legacy_run = str(uuid.uuid4())[:8]
        legacy_pred_time = datetime.utcnow() - timedelta(days=2)
        legacy_target_start = legacy_pred_time.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        for h in range(24):
            meta = {
                "guild_id": gid,
                "predicted_hour": h,
                "prediction_run": legacy_run,
                "daily_total": 1000,
                "target_start": legacy_target_start.isoformat(),
                "target_end": (legacy_target_start + timedelta(hours=24)).isoformat(),
                # NOTE: NO resolution_version or actual_granularity
            }
            db.session.add(
                PredictionLog(
                    model_name="forecast",
                    prediction_value=42,
                    features_json=json.dumps({"hour": h, "daily_prediction": 1000}),
                    metadata_json=json.dumps(meta),
                    actual_value=500,  # Legacy: daily total stored in every hourly row
                    prediction_time=legacy_pred_time,
                )
            )
        db.session.commit()

        # Create a v2-style resolved set with proper markers
        v2_run = str(uuid.uuid4())[:8]
        v2_pred_time = datetime.utcnow() - timedelta(days=1)
        v2_target_start = v2_pred_time.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        for h in range(24):
            meta = {
                "guild_id": gid,
                "predicted_hour": h,
                "prediction_run": v2_run,
                "daily_total": 1000,
                "target_start": v2_target_start.isoformat(),
                "target_end": (v2_target_start + timedelta(hours=24)).isoformat(),
                "resolution_version": 2,
                "actual_granularity": "hourly",
            }
            # Distribute actuals: ~42 per hour (1000/24)
            actual_per_hour = max(1, 1000 // 24)
            db.session.add(
                PredictionLog(
                    model_name="forecast",
                    prediction_value=42,
                    features_json=json.dumps({"hour": h, "daily_prediction": 1000}),
                    metadata_json=json.dumps(meta),
                    actual_value=actual_per_hour,
                    prediction_time=v2_pred_time,
                )
            )
        db.session.commit()

        # Verify: both legacy and v2 logs exist
        all_logs = PredictionLog.query.filter(
            PredictionLog.model_name == "forecast",
            PredictionLog.actual_value != None,
        ).all()
        assert len(all_logs) == 48, "Expected 48 total logs (24 legacy + 24 v2)"

        # Metrics should only use v2 logs (24 hourly = 1 daily run)
        metrics = get_accuracy_metrics(days=None, guild_id=gid)
        assert metrics["daily"]["samples"] == 1, (
            f"Expected 1 daily sample (v2 only), got {metrics['daily']['samples']}"
        )
        assert metrics["hourly"]["samples"] == 24, (
            f"Expected 24 hourly samples (v2 only), got {metrics['hourly']['samples']}"
        )


def test_resolved_log_has_version_marker(app):
    """After resolve_outcomes, each resolved log must have
    resolution_version=2 and actual_granularity=hourly."""
    import uuid

    run_id = str(uuid.uuid4())[:8]
    with app.app_context():
        gid = "version-marker-test"
        db.session.add(GuildInfo(guild_id=gid, name="Version Marker Test"))
        db.session.commit()

    past_day = datetime.utcnow() - timedelta(days=2)
    target_start = past_day.replace(hour=0, minute=0, second=0, microsecond=0)

    with app.app_context():
        for h in range(24):
            created = target_start + timedelta(hours=h)
            db.session.add(
                MessageRecord(
                    guild_id=gid,
                    discord_id="test-user",
                    channel_name="general",
                    hour_of_day=h,
                    created_at=created,
                    is_public_channel=True,
                )
            )
        db.session.commit()

    with app.app_context():
        for h in range(24):
            meta = {
                "guild_id": gid,
                "predicted_hour": h,
                "prediction_run": run_id,
                "daily_total": 48,
                "target_start": target_start.isoformat(),
                "target_end": (target_start + timedelta(hours=24)).isoformat(),
                "resolution_version": 2,
                "actual_granularity": "hourly",
            }
            db.session.add(
                PredictionLog(
                    model_name="forecast",
                    prediction_value=2,
                    metadata_json=json.dumps(meta),
                    prediction_time=target_start,
                )
            )
        db.session.commit()

    with app.app_context():
        resolved = resolve_outcomes()
        assert resolved > 0

        resolved_logs = PredictionLog.query.filter(
            PredictionLog.model_name == "forecast",
            PredictionLog.actual_value != None,
        ).all()
        for log in resolved_logs:
            meta = json.loads(log.metadata_json) if log.metadata_json else {}
            assert meta.get("resolution_version") == 2, (
                f"Log {log.id} missing resolution_version=2"
            )
            assert meta.get("actual_granularity") == "hourly", (
                f"Log {log.id} missing actual_granularity=hourly"
            )
