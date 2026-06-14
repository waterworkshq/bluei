"""Integration tests for the notification pipeline."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from bluei.engine.notify import (
    _detail_hash,
    deliver_digest,
    deliver_escalations,
    load_notification_config,
)


def _write_yaml(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data))


def _mock_response(status=200, body=b"ok"):
    mock = MagicMock()
    mock.status = status
    mock.read.return_value = body
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _setup_webhook_workspace(tmp_path, channels=None, **extra_global):
    channels = channels or [{"type": "webhook", "url": "https://example.com/hook"}]
    global_cfg = {"enabled": True, "channels": channels}
    global_cfg.update(extra_global)
    _write_yaml(tmp_path / "notifications.yaml", global_cfg)
    return tmp_path


def test_end_to_end_escalation_delivery(tmp_path):
    ws = _setup_webhook_workspace(tmp_path)
    findings = [{"type": "consecutive_merge_failures", "detail": "3 failures"}]

    with patch("bluei.engine.notifiers.webhook.urllib.request.urlopen", return_value=_mock_response()):
        results = deliver_escalations(findings, "test-repo", ws)

    assert len(results) == 1
    assert results[0].success
    log_path = ws / "state" / "notification_log.jsonl"
    assert log_path.exists()
    record = json.loads(log_path.read_text().strip().splitlines()[0])
    assert record["repo"] == "test-repo"
    assert record["severity"] == "error"
    assert record["escalation_type"] == "consecutive_merge_failures"
    assert len(record["deliveries"]) == 1
    assert record["deliveries"][0]["success"] is True


def test_rate_limit_blocks_second_delivery(tmp_path):
    ws = _setup_webhook_workspace(tmp_path, rate_limit={"cooldown_seconds": 3600, "max_per_hour": 20})
    findings = [{"type": "consecutive_merge_failures", "detail": "3 failures"}]

    with patch("bluei.engine.notifiers.webhook.urllib.request.urlopen", return_value=_mock_response()):
        r1 = deliver_escalations(findings, "test-repo", ws)
        r2 = deliver_escalations(findings, "test-repo", ws)

    assert len(r1) == 1
    assert len(r2) == 0


def test_rate_limit_allows_different_finding(tmp_path):
    ws = _setup_webhook_workspace(tmp_path, rate_limit={"cooldown_seconds": 3600, "max_per_hour": 20})
    f1 = [{"type": "consecutive_merge_failures", "detail": "3 failures"}]
    f2 = [{"type": "reappearing_finding", "detail": "ruff-b904 came back"}]

    with patch("bluei.engine.notifiers.webhook.urllib.request.urlopen", return_value=_mock_response()):
        r1 = deliver_escalations(f1, "test-repo", ws)
        r2 = deliver_escalations(f2, "test-repo", ws)

    assert len(r1) == 1
    assert len(r2) == 1


def test_disabled_config_blocks_everything(tmp_path):
    _write_yaml(tmp_path / "notifications.yaml", {
        "enabled": False,
        "channels": [{"type": "webhook", "url": "https://example.com/hook"}],
    })
    findings = [{"type": "consecutive_merge_failures", "detail": "3 failures"}]
    results = deliver_escalations(findings, "test-repo", tmp_path)
    assert results == []


def test_digest_delivers_to_digest_channels(tmp_path):
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

    captured_urls = []
    def capture_urlopen(req, **kwargs):
        captured_urls.append(req.full_url)
        return _mock_response()

    with patch("bluei.engine.notifiers.webhook.urllib.request.urlopen", side_effect=capture_urlopen):
        results = deliver_digest("test-repo", tmp_path)

    assert len(results) == 1
    assert results[0].success
    assert captured_urls[0] == "https://digest.example.com"


def test_cli_notify_config_masks_secrets(tmp_path, capsys):
    _write_yaml(tmp_path / "notifications.yaml", {
        "channels": [{"type": "webhook", "url": "https://hooks.slack.com/services/T00/B00/secret"}],
        "enabled": True,
    })

    import bin.bluei as cli
    original_cwd = Path.cwd()
    import os
    os.chdir(tmp_path)
    try:
        cli._notify_config(["--global"])
    finally:
        os.chdir(original_cwd)

    output = capsys.readouterr().out
    assert "secret" not in output
    assert "***" in output


def test_multiple_channels_all_fire(tmp_path):
    _write_yaml(tmp_path / "notifications.yaml", {
        "enabled": True,
        "channels": [
            {"type": "webhook", "url": "https://hook1.example.com"},
            {"type": "webhook", "url": "https://hook2.example.com"},
        ],
    })
    findings = [{"type": "consecutive_merge_failures", "detail": "3 failures"}]

    with patch("bluei.engine.notifiers.webhook.urllib.request.urlopen", return_value=_mock_response()):
        results = deliver_escalations(findings, "test-repo", tmp_path)

    assert len(results) == 2
    assert all(r.success for r in results)


def test_rate_limit_bypass_with_test_flag(tmp_path):
    ws = _setup_webhook_workspace(tmp_path, rate_limit={"cooldown_seconds": 3600, "max_per_hour": 0})
    findings = [{"type": "consecutive_merge_failures", "detail": "3 failures"}]

    with patch("bluei.engine.notifiers.webhook.urllib.request.urlopen", return_value=_mock_response()):
        r1 = deliver_escalations(findings, "test-repo", ws, bypass_rate_limit=True)
        r2 = deliver_escalations(findings, "test-repo", ws, bypass_rate_limit=True)

    assert len(r1) == 1
    assert len(r2) == 1


def test_doctor_check_notifications(tmp_path):
    import bin.bluei as cli
    from pathlib import Path
    from bluei.engine.notify import load_notification_config

    _write_yaml(tmp_path / "notifications.yaml", {
        "channels": [{"type": "webhook", "url": "https://example.com/hook"}],
        "enabled": True,
    })

    import os
    original_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        from bluei.engine.notifiers import CHANNEL_REGISTRY
        config = load_notification_config(None, Path.cwd())
        assert config["enabled"] is True
        assert len(config["channels"]) == 1
        ch = config["channels"][0]
        assert ch["type"] in CHANNEL_REGISTRY
        assert "url" in ch
    finally:
        os.chdir(original_cwd)
