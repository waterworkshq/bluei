"""Tests for the Slack incoming webhook notifier."""

import json
from unittest.mock import MagicMock, patch

import pytest
import urllib.error

from bluei.engine.notifiers import NotificationPayload
from bluei.engine.notifiers.slack import SlackNotifier, SEVERITY_EMOJI


def _make_payload(**overrides):
    defaults = {
        "title": "3 consecutive merge failures",
        "body": "3 consecutive failures with no successful merges",
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


def test_slack_success_ok():
    notifier = SlackNotifier({"url": "https://hooks.slack.com/services/T00/B00/xxx"})
    with patch(
        "bluei.engine.notifiers.slack.urllib.request.urlopen",
        return_value=_mock_response(),
    ):
        result = notifier.send(_make_payload())
    assert result.success
    assert result.status_code == 200


def test_slack_payload_has_text_field():
    notifier = SlackNotifier({"url": "https://hooks.slack.com/services/T00/B00/xxx"})
    raw = notifier.format_payload(_make_payload())
    data = json.loads(raw)
    assert "text" in data
    assert isinstance(data["text"], str)
    assert len(data["text"]) > 0


def test_slack_payload_has_blocks():
    notifier = SlackNotifier({"url": "https://hooks.slack.com/services/T00/B00/xxx"})
    raw = notifier.format_payload(_make_payload())
    data = json.loads(raw)
    assert "blocks" in data
    assert isinstance(data["blocks"], list)
    assert len(data["blocks"]) >= 2


def test_slack_header_block_has_emoji():
    notifier = SlackNotifier({"url": "https://hooks.slack.com/services/T00/B00/xxx"})
    raw = notifier.format_payload(_make_payload(severity="error"))
    data = json.loads(raw)
    header = data["blocks"][0]
    header_text = header["text"]["text"]
    assert "\U0001f6a8" in header_text


def test_slack_body_truncated_at_2900():
    notifier = SlackNotifier({"url": "https://hooks.slack.com/services/T00/B00/xxx"})
    long_body = "x" * 5000
    raw = notifier.format_payload(_make_payload(body=long_body))
    data = json.loads(raw)
    for block in data["blocks"]:
        if block["type"] == "section" and "text" in block:
            text = block["text"]["text"]
            if long_body[:50] in text:
                assert len(text) <= 2900


def test_slack_max_50_blocks():
    notifier = SlackNotifier({"url": "https://hooks.slack.com/services/T00/B00/xxx"})
    findings = [{"type": "f", "detail": f"detail {i}"} for i in range(60)]
    raw = notifier.format_payload(_make_payload(raw_findings=findings))
    data = json.loads(raw)
    assert len(data["blocks"]) <= 50


def test_slack_fallback_text_independent():
    notifier = SlackNotifier({"url": "https://hooks.slack.com/services/T00/B00/xxx"})
    raw = notifier.format_payload(_make_payload())
    data = json.loads(raw)
    text = data["text"]
    assert "bluei:" in text
    assert "*Repo:*" not in text


def test_slack_400_no_retry():
    notifier = SlackNotifier({"url": "https://hooks.slack.com/services/T00/B00/xxx"})
    error = urllib.error.HTTPError("url", 400, "Bad Request", {}, None)
    error.read = lambda: b"invalid_payload"
    call_count = {"n": 0}

    def counting_urlopen(req, **kwargs):
        call_count["n"] += 1
        raise error

    with patch(
        "bluei.engine.notifiers.slack.urllib.request.urlopen",
        side_effect=counting_urlopen,
    ):
        result = notifier.send(_make_payload())
    assert not result.success
    assert call_count["n"] == 1


def test_slack_404_no_retry():
    notifier = SlackNotifier({"url": "https://hooks.slack.com/services/T00/B00/xxx"})
    error = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
    error.read = lambda: b"channel_not_found"
    call_count = {"n": 0}

    def counting_urlopen(req, **kwargs):
        call_count["n"] += 1
        raise error

    with patch(
        "bluei.engine.notifiers.slack.urllib.request.urlopen",
        side_effect=counting_urlopen,
    ):
        result = notifier.send(_make_payload())
    assert not result.success
    assert result.status_code == 404
    assert "channel_not_found" in result.error


def test_slack_500_retries():
    notifier = SlackNotifier({"url": "https://hooks.slack.com/services/T00/B00/xxx"})
    error = urllib.error.HTTPError("url", 500, "Server Error", {}, None)
    error.read = lambda: b"rollup_error"
    call_count = {"n": 0}

    def counting_urlopen(req, **kwargs):
        call_count["n"] += 1
        raise error

    with patch(
        "bluei.engine.notifiers.slack.urllib.request.urlopen",
        side_effect=counting_urlopen,
    ):
        with patch("bluei.engine.notifiers.slack.time.sleep"):
            result = notifier.send(_make_payload())
    assert not result.success
    assert call_count["n"] == 3


def test_slack_context_block_has_timestamp():
    notifier = SlackNotifier({"url": "https://hooks.slack.com/services/T00/B00/xxx"})
    raw = notifier.format_payload(_make_payload())
    data = json.loads(raw)
    last_block = data["blocks"][-1]
    assert last_block["type"] == "context"
    assert "2026-01-01" in last_block["elements"][0]["text"]


def test_slack_max_5_findings_shown():
    notifier = SlackNotifier({"url": "https://hooks.slack.com/services/T00/B00/xxx"})
    findings = [{"type": "f", "detail": f"detail {i}"} for i in range(10)]
    raw = notifier.format_payload(_make_payload(raw_findings=findings))
    data = json.loads(raw)
    finding_blocks = [
        b
        for b in data["blocks"]
        if b["type"] == "section" and "detail 0" in b.get("text", {}).get("text", "")
    ]
    assert len(finding_blocks) <= 1
    detail_text = finding_blocks[0]["text"]["text"] if finding_blocks else ""
    count = detail_text.count("\u2022")
    assert count <= 5


def test_slack_missing_env_var():
    notifier = SlackNotifier({"url": "${MISSING_SLACK_URL}"})
    result = notifier.send(_make_payload())
    assert not result.success
    assert "missing env var" in result.error


# ── Extracted from test_small_tier23_remaining.py ──
# Additional Slack notifier edge cases


class TestSlackPayloadTruncation:
    def test_payload_truncated_when_exceeds_max_bytes(self):
        notifier = SlackNotifier({"url": "https://hooks.slack.com/test"})
        payload = _make_payload(
            title="\x00" * 5000,
            body="\x00" * 5000,
            raw_findings=[{"detail": "\x00" * 200} for _ in range(5)],
        )
        raw = notifier.format_payload(payload)
        data = json.loads(raw)
        assert len(data["blocks"]) == 3


class TestSlackNoUrlConfigured:
    def test_empty_url_returns_error(self):
        notifier = SlackNotifier({"url": ""})
        result = notifier.send(_make_payload())
        assert not result.success
        assert "no url configured" in result.error


class TestSlackInvalidUrlScheme:
    def test_non_http_url_returns_error(self):
        notifier = SlackNotifier({"url": "ftp://hooks.slack.com/test"})
        result = notifier.send(_make_payload())
        assert not result.success
        assert "invalid url" in result.error


class TestSlackHttpUrlWarning:
    def test_http_url_includes_warning_on_success(self):
        notifier = SlackNotifier({"url": "http://hooks.slack.com/test"})
        with patch(
            "bluei.engine.notifiers.slack.urllib.request.urlopen",
            return_value=_mock_response(),
        ):
            result = notifier.send(_make_payload())
        assert result.success
        assert result.status_code == 200
        assert "http://" in result.error
        assert "not HTTPS" in result.error


class TestSlack200NonOkBody:
    def test_200_with_non_ok_body_fails(self):
        notifier = SlackNotifier({"url": "https://hooks.slack.com/test"})
        with patch(
            "bluei.engine.notifiers.slack.urllib.request.urlopen",
            return_value=_mock_response(status=200, body=b"something else"),
        ):
            result = notifier.send(_make_payload())
        assert not result.success
        assert result.status_code == 200
        assert "something else" in result.error


class TestSlackUrlErrorExhaustion:
    def test_url_error_retries_three_times_then_fails(self):
        notifier = SlackNotifier({"url": "https://hooks.slack.com/test"})
        call_count = {"n": 0}

        def raise_url_error(req, **kwargs):
            call_count["n"] += 1
            raise urllib.error.URLError("connection refused")

        with patch(
            "bluei.engine.notifiers.slack.urllib.request.urlopen",
            side_effect=raise_url_error,
        ):
            with patch("bluei.engine.notifiers.slack.time.sleep"):
                result = notifier.send(_make_payload())
        assert not result.success
        assert call_count["n"] == 3
        assert "connection refused" in result.error
        assert result.latency_ms >= 0
