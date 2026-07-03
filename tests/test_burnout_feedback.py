from datetime import datetime, timedelta

from database import (
    BehavioralAnomaly,
    BurnoutRisk,
    GuildInfo,
    GuildMember,
    UserBehaviorBaseline,
    Worker,
    db,
)
from ml import burnout
from tests.conftest import login_discord


def add_worker(guild_id="1", discord_id="100", name="Burnout User"):
    guild = GuildInfo(guild_id=guild_id, name=f"Guild {guild_id}")
    member = GuildMember(
        guild_id=guild_id,
        member_id=discord_id,
        name=name,
        is_staff=True,
    )
    worker = Worker(
        name=name,
        email=f"{discord_id}@example.com",
        discord_id=discord_id,
    )
    db.session.add_all([guild, member, worker])
    db.session.commit()
    return worker


def test_dismissed_anomalies_do_not_feed_burnout(app):
    with app.app_context():
        worker = add_worker()
        db.session.add_all(
            [
                BehavioralAnomaly(
                    discord_id=worker.discord_id,
                    anomaly_type="ml_anomaly",
                    severity=90,
                    feedback="dismissed",
                )
                for _ in range(5)
            ]
        )
        db.session.commit()

        signals, triggered = burnout._compute_signal_scores(worker.discord_id, worker.id)

        assert signals["anomaly_freq"] == 0
        assert "frequent_anomalies" not in triggered


def test_dismissed_burnout_is_suppressed_during_cooldown(app):
    with app.app_context():
        worker = add_worker()
        db.session.add(
            UserBehaviorBaseline(
                discord_id=worker.discord_id,
                mean_daily_msgs_90d=10,
            )
        )
        db.session.add_all(
            [
                BehavioralAnomaly(
                    discord_id=worker.discord_id,
                    anomaly_type="ml_anomaly",
                    severity=90,
                    feedback="confirmed",
                )
                for _ in range(5)
            ]
        )
        db.session.add(
            BurnoutRisk(
                worker_id=worker.id,
                discord_id=worker.discord_id,
                name=worker.name,
                score=45,
                signals='["frequent_anomalies", "volume_volatility"]',
                feedback="dismissed",
                feedback_at=datetime.utcnow(),
            )
        )
        db.session.commit()

        result = burnout.score_worker(worker.discord_id)

        assert result["burnout_score"] >= 40
        assert result["is_flagged"] is False
        assert result["suppressed"] is True


def test_dashboard_hides_dismissed_burnout(app, client):
    with app.app_context():
        worker = add_worker(name="Hidden Burnout")
        db.session.add(
            BurnoutRisk(
                worker_id=worker.id,
                discord_id=worker.discord_id,
                name=worker.name,
                score=80,
                feedback="dismissed",
                feedback_at=datetime.utcnow(),
            )
        )
        db.session.commit()

    login_discord(client, ["1"])
    response = client.get("/")

    assert response.status_code == 200
    assert b"Hidden Burnout" not in response.data


def test_dashboard_hides_stale_confirmed_burnout(app, client):
    with app.app_context():
        worker = add_worker(name="Stale Confirmed")
        db.session.add(
            BurnoutRisk(
                worker_id=worker.id,
                discord_id=worker.discord_id,
                name=worker.name,
                score=80,
                feedback="confirmed",
                feedback_at=datetime.utcnow()
                - timedelta(days=burnout.BURNOUT_CONFIRM_RETAIN_DAYS + 1),
            )
        )
        db.session.commit()

    login_discord(client, ["1"])
    response = client.get("/")

    assert response.status_code == 200
    assert b"Stale Confirmed" not in response.data


def test_stale_confirmed_burnout_reopens_on_fresh_flag(app):
    with app.app_context():
        worker = add_worker()
        db.session.add(
            UserBehaviorBaseline(
                discord_id=worker.discord_id,
                mean_daily_msgs_90d=10,
            )
        )
        db.session.add_all(
            [
                BehavioralAnomaly(
                    discord_id=worker.discord_id,
                    anomaly_type="ml_anomaly",
                    severity=90,
                    feedback="confirmed",
                )
                for _ in range(5)
            ]
        )
        risk = BurnoutRisk(
            worker_id=worker.id,
            discord_id=worker.discord_id,
            name=worker.name,
            score=45,
            signals='["frequent_anomalies", "volume_volatility"]',
            feedback="confirmed",
            feedback_at=datetime.utcnow()
            - timedelta(days=burnout.BURNOUT_CONFIRM_RETAIN_DAYS + 1),
        )
        db.session.add(risk)
        db.session.commit()

        result = burnout.score_worker(worker.discord_id)

        assert result["is_flagged"] is True
        assert risk.feedback is None
        assert risk.feedback_at is None
