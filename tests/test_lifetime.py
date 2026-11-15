import math
import os
from datetime import date, datetime, timedelta

import pytest

from database import (
    BehaviorMetricDaily,
    BehaviorMetricsRollup,
    GuildMember,
    GuildMetricsContributionDaily,
    GuildMetricsDaily,
    MessageRecord,
    PairLifetime,
    PairScore,
    PairScoresHistory,
    PingEvent,
    ModelRegistry,
    UserBehaviorBaseline,
    UserBehaviorState,
    db,
)
from lifetime.pairs import EDGE_HALF_LIFE_MONTHS, snapshot_pair_month
from lifetime.state import (
    backfill_user_state,
    catch_up_guild_metrics_daily,
    catch_up_rollups,
    catch_up_user_state,
    update_user_state,
)


def _message(guild_id, user_id, created_at, length):
    return MessageRecord(
        guild_id=guild_id,
        discord_id=user_id,
        message_length=length,
        hour_of_day=created_at.hour,
        created_at=created_at,
    )


def test_online_state_matches_batch_welford_and_ewma(app):
    with app.app_context():
        guild_id, user_id = "g1", "u1"
        day1 = date.today() - timedelta(days=4)
        day2 = day1 + timedelta(days=1)
        db.session.add_all(
            [
                BehaviorMetricDaily(
                    guild_id=guild_id, user_id=user_id, date=day1, messages=2,
                    pings_sent=1, voice_hours=1.0,
                ),
                BehaviorMetricDaily(
                    guild_id=guild_id, user_id=user_id, date=day2, messages=4,
                    pings_sent=2, voice_hours=2.0,
                ),
                *[
                    _message(
                        guild_id,
                        user_id,
                        datetime.combine(day1, datetime.min.time())
                        + timedelta(hours=9, minutes=index),
                        10,
                    )
                    for index in range(2)
                ],
                *[
                    _message(
                        guild_id,
                        user_id,
                        datetime.combine(day2, datetime.min.time())
                        + timedelta(hours=17, minutes=index),
                        30,
                    )
                    for index in range(4)
                ],
            ]
        )
        db.session.commit()

        assert update_user_state(day1, half_life_days=2) == 1
        assert update_user_state(day2, half_life_days=2) == 1
        state = UserBehaviorState.query.filter_by(
            guild_id=guild_id, discord_id=user_id
        ).one()
        alpha = 1 - math.exp(-math.log(2) / 2)
        assert state.n_days == 2
        assert state.mean_daily_messages == 3
        assert state.m2_daily_messages == 2
        assert state.lifetime_messages == 6
        assert state.lifetime_pings == 3
        assert state.lifetime_voice_hours == 3
        assert state.hourly_profile[9] == pytest.approx(1 - alpha)
        assert state.hourly_profile[17] == pytest.approx(alpha)
        assert state.ewma_message_length == pytest.approx(10 * (1 - alpha) + 30 * alpha)
        assert state.ewma_off_hours_ratio == pytest.approx(alpha)

        before = (
            state.n_days,
            state.mean_daily_messages,
            state.m2_daily_messages,
            state.hourly_profile[:],
            state.lifetime_messages,
        )
        assert update_user_state(day2, half_life_days=2) == 0
        assert (
            state.n_days,
            state.mean_daily_messages,
            state.m2_daily_messages,
            state.hourly_profile,
            state.lifetime_messages,
        ) == before


def test_lifetime_state_survives_raw_cleanup(app, client):
    with app.app_context():
        old_at = datetime.utcnow() - timedelta(days=200)
        db.session.add(_message("g1", "u1", old_at.replace(hour=10), 12))
        db.session.commit()
        assert backfill_user_state() == 1

    response = client.post(
        "/api/observer/cleanup",
        json={"retention_days": 90},
        headers={"Authorization": "Bearer test-api-key"},
    )
    assert response.status_code == 200
    assert response.get_json()["deleted"] == 1

    with app.app_context():
        assert MessageRecord.query.count() == 0
        state = UserBehaviorState.query.filter_by(
            guild_id="g1", discord_id="u1"
        ).one()
        assert state.lifetime_messages == 1
        assert state.hourly_profile[10] == pytest.approx(1.0)


def test_monthly_pair_snapshot_and_decayed_edge_weight(app):
    with app.app_context():
        month = date(2026, 10, 1)
        now = datetime(2026, 11, 15, 12)
        pings = []
        for index in range(12):
            created = datetime(2026, 10, 5) + timedelta(days=index)
            pinger, pingee = (
                ("A", "B") if index % 2 == 0 else ("B", "A")
            )
            pings.append(
                PingEvent(
                    guild_id="g1",
                    pinger_id=pinger,
                    pingee_id=pingee,
                    channel_id="c1",
                    message_id=f"m{index}",
                    ping_type="mention",
                    addressed=True,
                    first_response_at=created + timedelta(minutes=5),
                    created_at=created,
                )
            )
        db.session.add_all(pings)
        db.session.commit()

        result = snapshot_pair_month(month=month, now=now)
        assert result["pairs"] == 2
        rows = PairScoresHistory.query.filter_by(month=month).all()
        assert len(rows) == 2
        a_to_b = next(row for row in rows if row.pinger_id == "A")
        b_to_a = next(row for row in rows if row.pinger_id == "B")
        assert a_to_b.sample_size == b_to_a.sample_size == 6
        assert a_to_b.affinity_score is not None
        assert a_to_b.return_rate == pytest.approx(1.0)
        assert b_to_a.return_rate == pytest.approx(5 / 6, abs=1e-4)
        assert a_to_b.median_response_minutes == 5
        assert a_to_b.conversations == 12

        edge = PairLifetime.query.filter_by(
            guild_id="g1", pinger_id="A", pingee_id="B"
        ).one()
        assert edge.lifetime_ping_count == 6
        assert edge.decayed_edge_weight == pytest.approx(
            6 * math.exp(-math.log(2) / EDGE_HALF_LIFE_MONTHS)
        )
        assert edge.first_interaction_at == datetime(2026, 10, 5)
        assert edge.last_interaction_at == datetime(2026, 10, 15)
        assert edge.peak_month == month
        snapshot_pair_month(month=month, now=now)
        assert PairLifetime.query.filter_by(
            guild_id="g1", pinger_id="A", pingee_id="B"
        ).one().lifetime_ping_count == 6


def test_optout_erases_member_derived_rows_and_rebuilds_guild_total(app, client):
    with app.app_context():
        day = date.today() - timedelta(days=1)
        db.session.add_all(
            [
                GuildMember(guild_id="g1", member_id="u1", name="u1"),
                GuildMember(guild_id="g1", member_id="u2", name="u2"),
                UserBehaviorState(
                    guild_id="g1", discord_id="u1", n_days=1,
                    mean_daily_messages=3, m2_daily_messages=0,
                    lifetime_messages=3, lifetime_pings=1,
                    lifetime_voice_hours=0, schema_version=1,
                ),
                UserBehaviorBaseline(guild_id="g1", discord_id="u1"),
                BehaviorMetricDaily(
                    guild_id="g1", user_id="u1", date=day, messages=3,
                ),
                BehaviorMetricsRollup(
                    guild_id="g1", user_id="u1", period_type="weekly",
                    period_start=day - timedelta(days=day.weekday()),
                ),
                GuildMetricsContributionDaily(
                    guild_id="g1", user_id="u1", date=day, messages=3, active=True,
                ),
                GuildMetricsContributionDaily(
                    guild_id="g1", user_id="u2", date=day, messages=2, active=True,
                ),
                GuildMetricsDaily(
                    guild_id="g1", date=day, active_users=2, messages=5,
                ),
                PairScore(guild_id="g1", pinger_id="u1", pingee_id="u2"),
                PairScore(guild_id="g1", pinger_id="u2", pingee_id="u1"),
                PairScoresHistory(
                    guild_id="g1", pinger_id="u1", pingee_id="u2",
                    month=day.replace(day=1), sample_size=1,
                ),
                PairScoresHistory(
                    guild_id="g1", pinger_id="u2", pingee_id="u1",
                    month=day.replace(day=1), sample_size=1,
                ),
                PairLifetime(guild_id="g1", pinger_id="u1", pingee_id="u2"),
                PairLifetime(guild_id="g1", pinger_id="u2", pingee_id="u1"),
            ]
        )
        db.session.commit()

    response = client.post(
        "/api/observer/consent",
        json={"guild_id": "g1", "discord_id": "u1", "optin": False, "purge": False},
        headers={"Authorization": "Bearer test-api-key"},
    )
    assert response.status_code == 200

    with app.app_context():
        assert UserBehaviorState.query.filter_by(discord_id="u1").count() == 0
        assert UserBehaviorBaseline.query.filter_by(discord_id="u1").count() == 0
        assert BehaviorMetricDaily.query.filter_by(user_id="u1").count() == 0
        assert BehaviorMetricsRollup.query.filter_by(user_id="u1").count() == 0
        assert GuildMetricsContributionDaily.query.filter_by(user_id="u1").count() == 0
        assert PairScore.query.filter(PairScore.pinger_id == "u1").count() == 0
        assert PairScore.query.filter(PairScore.pingee_id == "u1").count() == 0
        assert PairScoresHistory.query.filter(
            PairScoresHistory.pinger_id == "u1"
        ).count() == 0
        assert PairScoresHistory.query.filter(
            PairScoresHistory.pingee_id == "u1"
        ).count() == 0
        assert PairLifetime.query.filter(
            PairLifetime.pinger_id == "u1"
        ).count() == 0
        assert PairLifetime.query.filter(
            PairLifetime.pingee_id == "u1"
        ).count() == 0
        guild_day = GuildMetricsDaily.query.filter_by(guild_id="g1").one()
        assert guild_day.messages == 2
        assert guild_day.active_users == 1


def test_train_all_registers_versioned_artifact(app, monkeypatch, tmp_path):
    import ml.engine as engine
    from ml import anomaly, burnout, corrector, federated
    from ml import features

    with app.app_context():
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        artifact = models_dir / "anomaly.joblib"
        monkeypatch.setattr(engine, "MODELS_DIR", str(models_dir))
        monkeypatch.setattr(engine, "TRAINING_HISTORY_PATH", str(models_dir / "history.json"))
        monkeypatch.setattr(anomaly, "ANOMALY_MODEL_PATH", str(artifact))

        def train_anomaly(**_kwargs):
            artifact.write_bytes(b"model")
            return {"status": "trained", "users": 7, "anomalies_found": 1}

        monkeypatch.setattr(anomaly, "train", train_anomaly)
        monkeypatch.setattr(engine, "resolve_all_outcomes", lambda: {})
        monkeypatch.setattr(features, "update_user_baselines", lambda **_kwargs: 0)
        monkeypatch.setattr(features, "update_guild_baselines", lambda: 0)
        monkeypatch.setattr(corrector, "train", lambda **_kwargs: {"status": "skipped"})
        monkeypatch.setattr(burnout, "train", lambda **_kwargs: {"status": "weighted_scoring_active"})
        monkeypatch.setattr(federated, "train_federated", lambda **_kwargs: {"status": "skipped"})

        engine.train_all()
        engine.train_all()

        rows = ModelRegistry.query.filter_by(model_name="anomaly").order_by(
            ModelRegistry.version
        ).all()
        assert [row.version for row in rows] == [1, 2]
        assert [row.is_active for row in rows] == [False, True]
        assert rows[1].n_samples == 7
        assert os.path.isfile(rows[1].artifact_path)
        assert rows[1].artifact_path != str(artifact)


def test_catch_up_folds_missed_days_in_order_and_is_idempotent(app):
    with app.app_context():
        days = [date(2026, 10, 1) + timedelta(days=index) for index in range(5)]
        counts = [2, 4, 6, 8, 10]
        db.session.add_all(
            BehaviorMetricDaily(guild_id="g1", user_id="u1", date=day, messages=count)
            for day, count in zip(days, counts)
        )
        db.session.commit()

        first = catch_up_user_state(through_day=days[-1])
        assert first["folded"] == 5
        assert first["skipped_no_source"] == 0
        assert first["dates"] == [day.isoformat() for day in days]
        state = UserBehaviorState.query.filter_by(guild_id="g1", discord_id="u1").one()
        assert state.n_days == 5
        assert state.lifetime_messages == sum(counts)
        assert state.last_updated_date == days[-1]

        second = catch_up_user_state(through_day=days[-1])
        assert second["folded"] == 0
        assert second["dates"] == []

        # a fresh member folded by the batch path lands in the same place
        db.session.add_all(
            BehaviorMetricDaily(guild_id="g1", user_id="u2", date=day, messages=count)
            for day, count in zip(days, counts)
        )
        db.session.commit()
        backfill_user_state()
        batch = UserBehaviorState.query.filter_by(guild_id="g1", discord_id="u2").one()
        assert (
            state.n_days,
            state.mean_daily_messages,
            state.m2_daily_messages,
            state.lifetime_messages,
            state.last_updated_date,
        ) == (
            batch.n_days,
            batch.mean_daily_messages,
            batch.m2_daily_messages,
            batch.lifetime_messages,
            batch.last_updated_date,
        )


def test_catch_up_cap_continues_on_next_call(app):
    with app.app_context():
        days = [date(2026, 9, 1) + timedelta(days=index) for index in range(7)]
        db.session.add_all(
            BehaviorMetricDaily(guild_id="g1", user_id="u1", date=day, messages=1)
            for day in days
        )
        db.session.commit()

        first = catch_up_user_state(through_day=days[-1], max_days=3)
        assert first["folded"] == 3
        assert first["dates"] == [day.isoformat() for day in days[:3]]
        assert UserBehaviorState.query.one().n_days == 3

        second = catch_up_user_state(through_day=days[-1], max_days=3)
        assert second["folded"] == 3
        assert second["dates"] == [day.isoformat() for day in days[3:6]]

        third = catch_up_user_state(through_day=days[-1], max_days=3)
        assert third["folded"] == 1
        assert third["dates"] == [days[6].isoformat()]
        assert UserBehaviorState.query.one().n_days == 7


def test_catch_up_reports_days_with_no_source_data(app):
    with app.app_context():
        day0 = date(2026, 8, 1)
        for offset in (0, 1, 3):
            db.session.add(
                BehaviorMetricDaily(
                    guild_id="g1", user_id="u1", date=day0 + timedelta(days=offset),
                    messages=1,
                )
            )
        db.session.commit()
        result = catch_up_user_state(through_day=day0 + timedelta(days=3))
        assert result["folded"] == 3
        assert result["skipped_no_source"] == 1  # day0 + 2 has no rows


def test_catch_up_guild_metrics_and_rollups(app):
    with app.app_context():
        days = [date(2026, 10, 1) + timedelta(days=index) for index in range(5)]
        db.session.add_all(
            BehaviorMetricDaily(guild_id="g1", user_id="u1", date=day, messages=2)
            for day in days
        )
        db.session.commit()

        guild = catch_up_guild_metrics_daily(through_day=days[-1])
        assert guild["folded"] == 5
        assert GuildMetricsDaily.query.count() == 5
        assert catch_up_guild_metrics_daily(through_day=days[-1])["folded"] == 0

        rollups = catch_up_rollups(through_day=days[-1])
        assert rollups["folded"] == 3  # two weeks + one month
        assert (
            sum(
                row.messages
                for row in BehaviorMetricsRollup.query.filter_by(period_type="weekly")
            )
            == 10
        )
        assert (
            sum(
                row.messages
                for row in BehaviorMetricsRollup.query.filter_by(period_type="monthly")
            )
            == 10
        )


def test_prunes_old_model_versions_but_keeps_registry_rows(app, monkeypatch, tmp_path):
    import ml.engine as engine

    with app.app_context():
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        artifact = models_dir / "anomaly.joblib"
        monkeypatch.setattr(engine, "MODELS_DIR", str(models_dir))
        monkeypatch.setattr(engine, "MODEL_VERSIONS_KEEP", 3)

        def register():
            artifact.write_bytes(b"model")
            return engine._register_model_artifact(
                "anomaly", str(artifact), {"status": "trained", "users": 1},
                train_days=1,
            )

        for _ in range(8):
            register()

        rows = ModelRegistry.query.filter_by(model_name="anomaly").order_by(
            ModelRegistry.version
        ).all()
        assert [row.version for row in rows] == list(range(1, 9))
        assert [row.version for row in rows if row.is_active] == [8]
        for row in rows:
            exists = os.path.isdir(os.path.dirname(row.artifact_path))
            if row.version >= 6:  # active + the 2 newest
                assert exists, row.version
                assert row.pruned_at is None
            else:
                assert not exists, row.version
                assert row.pruned_at is not None


def test_pruning_keeps_best_version_by_primary_metric(app, monkeypatch, tmp_path):
    import ml.engine as engine

    with app.app_context():
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        artifact = models_dir / "forecast.joblib"
        monkeypatch.setattr(engine, "MODELS_DIR", str(models_dir))
        monkeypatch.setattr(engine, "MODEL_VERSIONS_KEEP", 2)

        for score in (0.1, 0.9, 0.2, 0.3, 0.4):
            artifact.write_bytes(b"model")
            engine._register_model_artifact(
                "forecast", str(artifact),
                {"status": "trained", "samples": 5, "r2_score": score},
                train_days=1, guild_id="g1",
            )

        rows = ModelRegistry.query.filter_by(
            model_name="forecast", guild_id="g1"
        ).order_by(ModelRegistry.version).all()
        kept = {
            row.version
            for row in rows
            if os.path.isdir(os.path.dirname(row.artifact_path))
        }
        assert kept == {2, 4, 5}  # best (v2) + newest 2 (v4, v5)
        assert all(row.pruned_at is not None for row in rows if row.version in {1, 3})
