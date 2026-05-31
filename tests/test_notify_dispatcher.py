"""Tests for the notification dispatcher — config loading, rate limiting, delivery."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from bluei.engine.notifiers import DeliveryResult, NotificationPayload
from bluei.engine.notify import (
    _check_cooldown,
    _check_hourly_cap,
    _detail_hash,
    _DEFAULT_NOTIFICATIONS,
    deliver_digest,
    deliver_escalations,
    load_notification_config,
    mask_sensitive,
)


def _write_yaml(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data))


def _write_jsonl(path: Path, records: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")


# ── Config loading ──────────────────────────────────────────


def test_load_config_defaults(tmp_path):
    config = load_notification_config(None, tmp_path)
    assert config["enabled"] is False
    assert config["channels"] == []


def test_load_config_global_only(tmp_path):
    _write_yaml(tmp_path / "notifications.yaml", {
        "channels": [{"type": "webhook", "url": "https://example.com/hook"}],
        "enabled": True,
    })
    config = load_notification_config(None, tmp_path)
    assert config["enabled"] is True
    assert len(config["channels"]) == 1
    assert config["_global"]["channels"] == config["channels"]


def test_load_config_per_repo_override(tmp_path):
    _write_yaml(tmp_path / "notifications.yaml", {
        "channels": [{"type": "webhook", "url": "https://global.example.com"}],
        "enabled": True,
    })
    repo_dir = tmp_path / "repos" / "my-project"
    _write_yaml(repo_dir / "config.yaml", {
        "notifications": {
            "channels": [{"type": "webhook", "url": "https://repo.example.com"}],
            "enabled": True,
        },
    })
    config = load_notification_config("my-project", tmp_path)
    assert config["channels"][0]["url"] == "https://repo.example.com"


def test_load_config_merge_rate_limit(tmp_path):
    _write_yaml(tmp_path / "notifications.yaml", {
        "rate_limit": {"cooldown_seconds": 600, "max_per_hour": 10},
    })
    repo_dir = tmp_path / "repos" / "my-project"
    _write_yaml(repo_dir / "config.yaml", {
        "notifications": {
            "rate_limit": {"max_per_hour": 5},
        },
    })
    config = load_notification_config("my-project", tmp_path)
    assert config["rate_limit"]["cooldown_seconds"] == 600
    assert config["rate_limit"]["max_per_hour"] == 5


def test_load_config_per_repo_channels_replace(tmp_path):
    _write_yaml(tmp_path / "notifications.yaml", {
        "channels": [{"type": "webhook", "url": "https://a.com"}],
    })
    repo_dir = tmp_path / "repos" / "my-project"
    _write_yaml(repo_dir / "config.yaml", {
        "notifications": {
            "channels": [{"type": "webhook", "url": "https://b.com"}],
        },
    })
    config = load_notification_config("my-project", tmp_path)
    assert len(config["channels"]) == 1
    assert config["channels"][0]["url"] == "https://b.com"


# ── Rate limiting ───────────────────────────────────────────


def test_rate_limit_allows_when_no_log(tmp_path):
    config = {"rate_limit": {"cooldown_seconds": 300, "max_per_hour": 20}}
    assert _check_cooldown(config, "test_type", "hash1", tmp_path) is True


def test_rate_limit_blocks_cooldown(tmp_path):
    log_path = tmp_path / "state" / "notification_log.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    _write_jsonl(log_path, [{
        "timestamp": now,
        "escalation_type": "consecutive_merge_failures",
        "detail_hash": "abc123",
        "deliveries": [{"channel_type": "webhook", "success": True}],
    }])
    config = {"rate_limit": {"cooldown_seconds": 300, "max_per_hour": 20}}
    assert _check_cooldown(config, "consecutive_merge_failures", "abc123", tmp_path) is False


def test_rate_limit_allows_after_cooldown(tmp_path):
    log_path = tmp_path / "state" / "notification_log.jsonl"
    old_ts = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    _write_jsonl(log_path, [{
        "timestamp": old_ts,
        "escalation_type": "consecutive_merge_failures",
        "detail_hash": "abc123",
        "deliveries": [{"channel_type": "webhook", "success": True}],
    }])
    config = {"rate_limit": {"cooldown_seconds": 300, "max_per_hour": 20}}
    assert _check_cooldown(config, "consecutive_merge_failures", "abc123", tmp_path) is True


def test_rate_limit_blocks_hourly_cap(tmp_path):
    log_path = tmp_path / "state" / "notification_log.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    for i in range(20):
        _write_jsonl(log_path, [{
            "timestamp": now,
            "escalation_type": f"type_{i}",
            "detail_hash": f"hash_{i}",
            "deliveries": [{"channel_type": "webhook", "success": True, "url": "https://hook.example.com"}],
        }])
    config = {"rate_limit": {"cooldown_seconds": 0, "max_per_hour": 20}}
    assert _check_hourly_cap(config, "https://hook.example.com", tmp_path) is False


def test_rate_limit_allows_under_cap(tmp_path):
    log_path = tmp_path / "state" / "notification_log.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    for i in range(10):
        _write_jsonl(log_path, [{
            "timestamp": now,
            "escalation_type": f"type_{i}",
            "detail_hash": f"hash_{i}",
            "deliveries": [{"channel_type": "webhook", "success": True, "url": "https://hook.example.com"}],
        }])
    config = {"rate_limit": {"cooldown_seconds": 0, "max_per_hour": 20}}
    assert _check_hourly_cap(config, "https://hook.example.com", tmp_path) is True


def test_rate_limit_per_url_separate(tmp_path):
    log_path = tmp_path / "state" / "notification_log.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    for i in range(20):
        _write_jsonl(log_path, [{
            "timestamp": now,
            "escalation_type": f"type_{i}",
            "detail_hash": f"hash_{i}",
            "deliveries": [{"channel_type": "webhook", "success": True, "url": "https://url1.example.com"}],
        }])
    config = {"rate_limit": {"cooldown_seconds": 0, "max_per_hour": 20}}
    assert _check_hourly_cap(config, "https://url1.example.com", tmp_path) is False
    assert _check_hourly_cap(config, "https://url2.example.com", tmp_path) is True


# ── Deliver escalations ─────────────────────────────────────


def test_deliver_escalations_disabled(tmp_path):
    findings = [{"type": "consecutive_merge_failures", "detail": "3 failures"}]
    results = deliver_escalations(findings, "test-repo", tmp_path)
    assert results == []


def test_deliver_escalations_no_channels(tmp_path):
    _write_yaml(tmp_path / "notifications.yaml", {"enabled": True, "channels": []})
    findings = [{"type": "consecutive_merge_failures", "detail": "3 failures"}]
    results = deliver_escalations(findings, "test-repo", tmp_path)
    assert results == []


def test_deliver_escalations_sends_to_channels(tmp_path):
    _write_yaml(tmp_path / "notifications.yaml", {
        "enabled": True,
        "channels": [{"type": "webhook", "url": "https://example.com/hook"}],
    })
    findings = [{"type": "consecutive_merge_failures", "detail": "3 failures"}]

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b"ok"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("bluei.engine.notifiers.webhook.urllib.request.urlopen", return_value=mock_resp):
        results = deliver_escalations(findings, "test-repo", tmp_path)
    assert len(results) == 1
    assert results[0].success


def test_deliver_escalations_respects_severity_filter(tmp_path):
    _write_yaml(tmp_path / "notifications.yaml", {
        "enabled": True,
        "channels": [{"type": "webhook", "url": "https://example.com/hook", "severity_filter": ["error"]}],
    })
    findings = [{"type": "reappearing_finding", "detail": "found again"}]

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b"ok"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("bluei.engine.notifiers.webhook.urllib.request.urlopen", return_value=mock_resp):
        results = deliver_escalations(findings, "test-repo", tmp_path)
    assert len(results) == 0


def test_deliver_escalations_rate_limited_finding_skipped(tmp_path):
    _write_yaml(tmp_path / "notifications.yaml", {
        "enabled": True,
        "channels": [{"type": "webhook", "url": "https://example.com/hook"}],
        "rate_limit": {"cooldown_seconds": 3600, "max_per_hour": 20},
    })
    log_path = tmp_path / "state" / "notification_log.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    dh = _detail_hash("3 failures")
    _write_jsonl(log_path, [{
        "timestamp": now,
        "escalation_type": "consecutive_merge_failures",
        "detail_hash": dh,
        "deliveries": [{"channel_type": "webhook", "success": True}],
    }])
    findings = [{"type": "consecutive_merge_failures", "detail": "3 failures"}]
    results = deliver_escalations(findings, "test-repo", tmp_path)
    assert len(results) == 0


def test_deliver_escalations_writes_notification_log(tmp_path):
    _write_yaml(tmp_path / "notifications.yaml", {
        "enabled": True,
        "channels": [{"type": "webhook", "url": "https://example.com/hook"}],
    })
    findings = [{"type": "consecutive_merge_failures", "detail": "3 failures"}]

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b"ok"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("bluei.engine.notifiers.webhook.urllib.request.urlopen", return_value=mock_resp):
        deliver_escalations(findings, "test-repo", tmp_path)

    log_path = tmp_path / "state" / "notification_log.jsonl"
    assert log_path.exists()
    record = json.loads(log_path.read_text().strip().splitlines()[0])
    assert record["repo"] == "test-repo"
    assert record["severity"] == "error"
    assert len(record["deliveries"]) == 1


# ── Deliver digest ──────────────────────────────────────────


def test_deliver_digest_disabled(tmp_path):
    results = deliver_digest("test-repo", tmp_path)
    assert results == []


def test_deliver_digest_uses_digest_channels(tmp_path):
    _write_yaml(tmp_path / "notifications.yaml", {
        "enabled": True,
        "channels": [{"type": "webhook", "url": "https://main.example.com"}],
        "digest": {
            "enabled": True,
            "schedule": "daily",
            "channels": [{"type": "webhook", "url": "https://digest.example.com"}],
        },
    })
    state_dir = tmp_path / "repos" / "test-repo" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b"ok"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("bluei.engine.notifiers.webhook.urllib.request.urlopen", return_value=mock_resp):
        results = deliver_digest("test-repo", tmp_path)
    assert len(results) == 1


def test_deliver_digest_falls_back_to_top_channels(tmp_path):
    _write_yaml(tmp_path / "notifications.yaml", {
        "enabled": True,
        "channels": [{"type": "webhook", "url": "https://main.example.com"}],
        "digest": {
            "enabled": True,
            "schedule": "daily",
        },
    })
    state_dir = tmp_path / "repos" / "test-repo" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b"ok"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("bluei.engine.notifiers.webhook.urllib.request.urlopen", return_value=mock_resp):
        results = deliver_digest("test-repo", tmp_path)
    assert len(results) == 1


# ── Sensitive masking ───────────────────────────────────────


def test_mask_url():
    assert mask_sensitive("https://hooks.slack.com/services/T00/B00/xxx") == "https://***"


def test_mask_short_token():
    assert mask_sensitive("abc") == "***"


def test_mask_bearer_token():
    result = mask_sensitive("Bearer abc123xyz")
    assert "***" in result
