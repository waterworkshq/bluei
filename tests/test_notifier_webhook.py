"""Tests for the generic HTTP POST webhook notifier."""

import json
from unittest.mock import MagicMock, patch

import pytest
import urllib.error

from bluei.engine.notifiers import NotificationPayload
from bluei.engine.notifiers.webhook import WebhookNotifier


def _make_payload(**overrides):
    defaults = {
        "title": "merge failed",
        "body": "3 consecutive failures",
        "severity": "error",
        "escalation_type": "consecutive_merge_failures",
        "repo_name": "my-project",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "raw_findings": [
            {"type": "consecutive_merge_failures", "detail": "3 failures"}
        ],
    }
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def _mock_response(status=200, body=b"ok"):
    mock = MagicMock()
    mock.status = status
    mock.read.return_value = body
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def test_webhook_success_200():
    notifier = WebhookNotifier({"url": "https://example.com/hook"})
    with patch(
        "bluei.engine.notifiers.webhook.urllib.request.urlopen",
        return_value=_mock_response(),
    ):
        result = notifier.send(_make_payload())
    assert result.success
    assert result.status_code == 200


def test_webhook_payload_format():
    notifier = WebhookNotifier({"url": "https://example.com/hook"})
    payload = _make_payload()
    raw = notifier.format_payload(payload)
    data = json.loads(raw)
    assert data["source"] == "bluei"
    assert "version" in data
    assert data["timestamp"] == payload.timestamp
    assert data["repo"] == "my-project"
    assert data["severity"] == "error"
    assert data["escalation_type"] == "consecutive_merge_failures"
    assert data["title"] == "merge failed"
    assert data["body"] == "3 consecutive failures"
    assert len(data["findings"]) == 1


def test_webhook_custom_headers():
    notifier = WebhookNotifier(
        {
            "url": "https://example.com/hook",
            "headers": {"X-Custom": "value123"},
        }
    )
    captured_req = {}

    def capture_urlopen(req, **kwargs):
        captured_req["req"] = req
        return _mock_response()

    with patch(
        "bluei.engine.notifiers.webhook.urllib.request.urlopen",
        side_effect=capture_urlopen,
    ):
        notifier.send(_make_payload())
    assert captured_req["req"].get_header("X-custom") == "value123"


def test_webhook_env_var_substitution(monkeypatch):
    monkeypatch.setenv("HOOK_URL", "https://example.com/webhook")
    monkeypatch.setenv("AUTH_KEY", "secret123")
    notifier = WebhookNotifier(
        {
            "url": "${HOOK_URL}",
            "headers": {"Authorization": "Bearer ${AUTH_KEY}"},
        }
    )
    captured_req = {}

    def capture_urlopen(req, **kwargs):
        captured_req["req"] = req
        return _mock_response()

    with patch(
        "bluei.engine.notifiers.webhook.urllib.request.urlopen",
        side_effect=capture_urlopen,
    ):
        result = notifier.send(_make_payload())
    assert result.success
    assert captured_req["req"].full_url == "https://example.com/webhook"
    assert captured_req["req"].get_header("Authorization") == "Bearer secret123"


def test_webhook_missing_env_var():
    notifier = WebhookNotifier({"url": "${NONEXISTENT_WEBHOOK_URL_XYZ}"})
    result = notifier.send(_make_payload())
    assert not result.success
    assert "missing env var" in result.error


def test_webhook_no_url_configured():
    notifier = WebhookNotifier({})
    result = notifier.send(_make_payload())
    assert not result.success
    assert "no url configured" in result.error


def test_webhook_400_no_retry():
    notifier = WebhookNotifier({"url": "https://example.com/hook"})
    error = urllib.error.HTTPError("url", 400, "Bad Request", {}, None)
    error.read = lambda: b"bad payload"
    call_count = {"n": 0}

    def counting_urlopen(req, **kwargs):
        call_count["n"] += 1
        raise error

    with patch(
        "bluei.engine.notifiers.webhook.urllib.request.urlopen",
        side_effect=counting_urlopen,
    ):
        result = notifier.send(_make_payload())
    assert not result.success
    assert result.status_code == 400
    assert call_count["n"] == 1


def test_webhook_403_no_retry():
    notifier = WebhookNotifier({"url": "https://example.com/hook"})
    error = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
    error.read = lambda: b"forbidden"
    call_count = {"n": 0}

    def counting_urlopen(req, **kwargs):
        call_count["n"] += 1
        raise error

    with patch(
        "bluei.engine.notifiers.webhook.urllib.request.urlopen",
        side_effect=counting_urlopen,
    ):
        result = notifier.send(_make_payload())
    assert not result.success
    assert result.status_code == 403
    assert call_count["n"] == 1


def test_webhook_500_retries():
    notifier = WebhookNotifier({"url": "https://example.com/hook"})
    error = urllib.error.HTTPError("url", 500, "Server Error", {}, None)
    error.read = lambda: b"internal error"
    call_count = {"n": 0}

    def counting_urlopen(req, **kwargs):
        call_count["n"] += 1
        raise error

    with patch(
        "bluei.engine.notifiers.webhook.urllib.request.urlopen",
        side_effect=counting_urlopen,
    ):
        with patch("bluei.engine.notifiers.webhook.time.sleep"):
            result = notifier.send(_make_payload())
    assert not result.success
    assert result.status_code == 500
    assert call_count["n"] == 3


def test_webhook_network_error_retries():
    notifier = WebhookNotifier({"url": "https://example.com/hook"})
    call_count = {"n": 0}

    def counting_urlopen(req, **kwargs):
        call_count["n"] += 1
        raise urllib.error.URLError("connection refused")

    with patch(
        "bluei.engine.notifiers.webhook.urllib.request.urlopen",
        side_effect=counting_urlopen,
    ):
        with patch("bluei.engine.notifiers.webhook.time.sleep"):
            result = notifier.send(_make_payload())
    assert not result.success
    assert "connection refused" in result.error
    assert call_count["n"] == 3


def test_webhook_no_url_returns_failure():
    notifier = WebhookNotifier({"url": ""})
    result = notifier.send(_make_payload())
    assert not result.success


def test_webhook_invalid_url_scheme():
    notifier = WebhookNotifier({"url": "ftp://example.com/hook"})
    result = notifier.send(_make_payload())
    assert not result.success
    assert "invalid url" in result.error


def test_webhook_http_url_includes_warning():
    notifier = WebhookNotifier({"url": "http://example.com/hook"})
    with patch(
        "bluei.engine.notifiers.webhook.urllib.request.urlopen",
        return_value=_mock_response(),
    ):
        result = notifier.send(_make_payload())
    assert result.success
    assert "http://" in result.error
