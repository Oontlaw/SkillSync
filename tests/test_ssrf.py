"""SSRF hardening: URL validation resolves DNS and rejects private targets;
redirect hops are re-validated; oversized bodies are refused."""
import socket
from unittest import mock

import requests

from work_engine.connector_jira import _safe_get, _validate_jira_url


def test_validate_blocks_non_https_and_private_literals():
    ok, _ = _validate_jira_url("http://jira.example.com/rest")
    assert not ok
    ok, _ = _validate_jira_url("https://127.0.0.1/rest")
    assert not ok
    ok, _ = _validate_jira_url("https://10.0.0.5/rest")
    assert not ok
    ok, _ = _validate_jira_url("https://169.254.169.254/latest/meta-data")
    assert not ok
    ok, _ = _validate_jira_url("https://localhost/rest")
    assert not ok


def test_validate_resolves_dns_and_blocks_private_results():
    """DNS rebinding: public name resolving to a private IP must be blocked."""
    fake = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443)),
    ]
    with mock.patch("work_engine.connector_jira.socket.getaddrinfo", return_value=fake):
        ok, reason = _validate_jira_url("https://rebind.example.com/rest")
    assert not ok and "private" in reason.lower()


def test_validate_allows_public_resolution():
    fake = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ]
    with mock.patch("work_engine.connector_jira.socket.getaddrinfo", return_value=fake):
        ok, reason = _validate_jira_url("https://jira.example.com/rest")
    assert ok and reason == "ok"


def test_safe_get_blocks_redirect_to_private_target():
    """A 302 from an allowed URL to the metadata endpoint must not be followed."""
    resp = requests.Response()
    resp.status_code = 302
    resp.headers["Location"] = "https://169.254.169.254/latest/meta-data"
    resp._content = b""
    with mock.patch(
        "work_engine.connector_jira.requests.get", return_value=resp
    ) as mget, mock.patch(
        "work_engine.connector_jira.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    ):
        out = _safe_get("https://jira.example.com/a")
    assert out is None  # redirect hop rejected, never fetched
    assert mget.call_count == 1  # only the first hop was requested


def test_safe_get_refuses_oversized_body():
    resp = requests.Response()
    resp.status_code = 200
    resp._content = b"x" * (5 * 1024 * 1024 + 1)
    with mock.patch(
        "work_engine.connector_jira.requests.get", return_value=resp
    ), mock.patch(
        "work_engine.connector_jira.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    ):
        out = _safe_get("https://jira.example.com/big")
    assert out is None
