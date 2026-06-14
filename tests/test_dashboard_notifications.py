"""Tests for dashboard notification summaries."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from bluei.engine.notify import deliver_escalations


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


def test_dashboard_notification_summary(tmp_path):
    global_cfg = {
        "enabled": True,
        "channels": [{"type": "webhook", "url": "https://example.com/hook"}],
    }
    _write_yaml(tmp_path / "notifications.yaml", global_cfg)
    findings = [{"type": "consecutive_merge_failures", "detail": "3 failures"}]

    with patch(
        "bluei.engine.notifiers.webhook.urllib.request.urlopen",
        return_value=_mock_response(),
    ):
        deliver_escalations(findings, "test-repo", tmp_path)

    log_path = tmp_path / "state" / "notification_log.jsonl"
    assert log_path.exists()

    from bluei.app.dashboard import _summarize_notifications

    summary = _summarize_notifications(log_path)
    assert summary["total"] == 1
    assert summary["delivered"] == 1
    assert "webhook" in summary["channels"]
