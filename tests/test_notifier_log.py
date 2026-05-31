"""Tests for the JSONL log notifier."""

import json

import pytest

from bluei.engine.notifiers import DeliveryResult, NotificationPayload
from bluei.engine.notifiers.log import LogNotifier


def _make_payload(**overrides):
    defaults = {
        "title": "test",
        "body": "test body",
        "severity": "warning",
        "escalation_type": "test_type",
        "repo_name": "test-repo",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "raw_findings": [],
        "metadata": {"detail_hash": "abc123"},
    }
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def test_log_notifier_creates_file(tmp_path):
    log_file = tmp_path / "notification_log.jsonl"
    notifier = LogNotifier({"log_path": str(log_file)})
    result = notifier.send(_make_payload())
    assert result.success
    assert log_file.exists()


def test_log_notifier_appends_jsonl(tmp_path):
    log_file = tmp_path / "notification_log.jsonl"
    notifier = LogNotifier({"log_path": str(log_file)})
    notifier.send(_make_payload(title="first"))
    notifier.send(_make_payload(title="second"))
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["severity"] == "warning"
    assert second["severity"] == "warning"


def test_log_record_has_required_fields(tmp_path):
    log_file = tmp_path / "notification_log.jsonl"
    notifier = LogNotifier({"log_path": str(log_file)})
    notifier.send(
        _make_payload(
            metadata={
                "detail_hash": "deadbeef",
                "deliveries": [{"channel_type": "slack", "success": True}],
            }
        )
    )
    record = json.loads(log_file.read_text().strip())
    assert "timestamp" in record
    assert record["repo"] == "test-repo"
    assert record["severity"] == "warning"
    assert record["escalation_type"] == "test_type"
    assert record["detail_hash"] == "deadbeef"
    assert len(record["deliveries"]) == 1


def test_log_notifier_mkdirs(tmp_path):
    log_file = tmp_path / "nested" / "deep" / "notification_log.jsonl"
    notifier = LogNotifier({"log_path": str(log_file)})
    result = notifier.send(_make_payload())
    assert result.success
    assert log_file.exists()


def test_log_notifier_oserror_returns_failure(tmp_path):
    log_file = tmp_path / "readonly" / "log.jsonl"
    log_file.parent.mkdir()
    log_file.parent.chmod(0o444)
    try:
        notifier = LogNotifier({"log_path": str(log_file)})
        result = notifier.send(_make_payload())
        assert not result.success
    finally:
        log_file.parent.chmod(0o755)
