"""Test dashboard v2 endpoints and template rendering"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from flask import session


def test_dashboard_v2_requires_auth(client):
    """Test that v2 dashboard redirects to login when not authenticated."""
    response = client.get("/v2/")
    assert response.status_code == 302
    assert "/auth/login" in response.location


def test_dashboard_v2_index_template(client, auth_headers):
    """Test that v2 dashboard index renders successfully."""
    with client.session_transaction() as sess:
        sess["user"] = {"id": "123456789", "name": "TestUser", "avatar": "abc"}
        sess["accessible_guilds"] = [{"id": "123", "name": "TestGuild"}]

    response = client.get("/v2/", headers=auth_headers)
    assert response.status_code == 200
    assert b"SkillSync" in response.data


def test_dashboard_v2_live_endpoint(client, auth_headers):
    """Test v2 live endpoint returns correct data."""
    with client.session_transaction() as sess:
        sess["user"] = {"id": "123456789", "name": "TestUser", "avatar": "abc"}
        sess["accessible_guilds"] = [
            {"id": "123", "name": "Guild1"},
            {"id": "456", "name": "Guild2"},
        ]

    response = client.get("/v2/_live", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert "total_members_tracked" in data
    assert "guild_online_map" in data


def test_dashboard_v2_only_accessible_guilds(client, auth_headers, monkeypatch):
    """Test that v2 dashboard only exposes accessible guilds."""
    with client.session_transaction() as sess:
        sess["user"] = {"id": "123456789", "name": "TestUser", "avatar": "abc"}
        sess["accessible_guilds"] = [{"id": "123", "name": "AllowedGuild"}]

    # Mock get_accessible_guild_ids to return specific guilds
    with patch("routes.security.accessible_guild_ids") as mock_ids:
        mock_ids.return_value = ["123"]

        response = client.get("/v2/")
        assert response.status_code == 200
        # Verify dashboard only shows allowed guild
        assert b"AllowedGuild" in response.data


def test_dashboard_v2_js_validation(client, auth_headers):
    """Test that JavaScript CSRF protection is present in v2 dashboard."""
    with client.session_transaction() as sess:
        sess["user"] = {"id": "123456789", "name": "TestUser", "avatar": "abc"}
        sess["accessible_guilds"] = [{"id": "123", "name": "TestGuild"}]

    response = client.get("/v2/")
    assert response.status_code == 200
    assert b"X-CSRF-Token" in response.data
    assert b"csrfToken" in response.data


def test_dashboard_v2_csrf_protection(client):
    """Test CSRF protection for v2 dashboard POST endpoints."""
    with client.session_transaction() as sess:
        sess["user"] = {"id": "123456789", "name": "TestUser", "avatar": "abc"}
        sess["accessible_guilds"] = [{"id": "123", "name": "TestGuild"}]

    # Test anomaly feedback endpoint without CSRF token
    response = client.post(
        "/v2/ml/anomaly-feedback", json={"anomaly_id": "1", "feedback": "confirmed"}
    )
    assert response.status_code == 403


def test_dashboard_v2_ml_endpoints_exist(client, auth_headers):
    """Test that all v2 ML endpoints exist and are accessible."""
    with client.session_transaction() as sess:
        sess["user"] = {"id": "123456789", "name": "TestUser", "avatar": "abc"}
        sess["accessible_guilds"] = [{"id": "123", "name": "TestGuild"}]

    endpoints = [
        ("/v2/ml/anomaly-feedback", "POST"),
        ("/v2/ml/burnout-feedback", "POST"),
        ("/v2/ml/retrain", "POST"),
        ("/v2/ml/federated-train", "POST"),
    ]

    for endpoint, method in endpoints:
        if method == "POST":
            response = client.post(endpoint, headers=auth_headers)
        else:
            response = client.get(endpoint, headers=auth_headers)
        assert response.status_code == 200, (
            f"Endpoint {endpoint} failed: {response.data}"
        )


def test_dashboard_v2_media_queries_without_style(client):
    """Test v2 dashboard responsive design."""
    with client.session_transaction() as sess:
        sess["user"] = {"id": "123456789", "name": "TestUser", "avatar": "abc"}
        sess["accessible_guilds"] = [{"id": "123", "name": "TestGuild"}]

    response = client.get("/v2/")
    assert response.status_code == 200
    # Check for critical CSS structure
    assert b'class="topbar"' in response.data
    assert b".wrap" in response.data
    assert b".card" in response.data


def test_dashboard_v2_refresh_request_structure(client, auth_headers):
    """Test that live refresh request maintains proper structure."""
    with client.session_transaction() as sess:
        sess["user"] = {"id": "123456789", "name": "TestUser", "avatar": "abc"}
        sess["accessible_guilds"] = [{"id": "123", "name": "TestGuild"}]

    response = client.get("/v2/_live", headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data.get("total_members_tracked"), (int, type(None)))
    assert isinstance(data.get("guild_online_map"), dict)


def test_dashboard_v2_anomaly_access_control(client, auth_headers):
    """Test that v2 dashboard prevents unauthorized anomaly access."""
    with client.session_transaction() as sess:
        sess["user"] = {"id": "123456789", "name": "TestUser", "avatar": "abc"}
        sess["accessible_guilds"] = []

    response = client.get("/v2/", headers=auth_headers)
    assert response.status_code == 302


def test_dashboard_v2_frontend_routing(client):
    """Test v2 dashboard frontend routing structure."""
    response = client.get("/v2/", follow_redirects=True)
    assert response.status_code in [200, 302]


def test_dashboard_v2_drift_banner_present(client, auth_headers):
    """Test that v2 dashboard shows drift banner when drift detected."""
    with client.session_transaction() as sess:
        sess["user"] = {"id": "123456789", "name": "TestUser", "avatar": "abc"}
        sess["accessible_guilds"] = [{"id": "123", "name": "TestGuild"}]

    # Mock ml_status with drift detection
    with patch("routes.dashboard.ml_engine.get_model_status") as mock_status:
        mock_status.return_value = {
            "health": {"drift_detected": True, "drift_reasons": ["reason1"]}
        }

        response = client.get("/v2/", headers=auth_headers)
        assert response.status_code == 200
        assert b"drift" in response.data.lower()


def test_dashboard_v2_data_structure(client, auth_headers):
    """Test that v2 dashboard has expected data structure."""
    with client.session_transaction() as sess:
        sess["user"] = {"id": "123456789", "name": "TestUser", "avatar": "abc"}
        sess["accessible_guilds"] = [{"id": "123", "name": "TestGuild"}]

    response = client.get("/v2/", headers=auth_headers)
    assert response.status_code == 200

    # Check for critical dashboard components
    assert b"Dashboard" in response.data
    assert b"Tracked" in response.data
    assert b"Guilds" in response.data
