"""Tests for notification base classes, registry, and env var resolution."""

import os

import pytest

from bluei.engine.notifiers import (
    BaseNotifier,
    CHANNEL_REGISTRY,
    DeliveryResult,
    NotificationPayload,
    resolve_env_vars,
)


def _make_payload(**overrides):
    defaults = {
        "title": "test",
        "body": "test body",
        "severity": "warning",
        "escalation_type": "test_type",
        "repo_name": "test-repo",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "raw_findings": [],
    }
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def test_notification_payload_construction():
    p = _make_payload(title="hello", severity="error")
    assert p.title == "hello"
    assert p.severity == "error"
    assert p.metadata == {}


def test_delivery_result_defaults():
    r = DeliveryResult(channel_type="test", success=True)
    assert r.status_code is None
    assert r.error is None
    assert r.latency_ms == 0.0


def test_should_send_no_filter():
    class ConcreteNotifier(BaseNotifier):
        channel_type = "concrete"
        def send(self, payload):
            return DeliveryResult(channel_type=self.channel_type, success=True)

    n = ConcreteNotifier({})
    assert n.should_send(_make_payload(), []) is True


def test_should_send_matching_severity():
    class ConcreteNotifier(BaseNotifier):
        channel_type = "concrete"
        def send(self, payload):
            return DeliveryResult(channel_type=self.channel_type, success=True)

    n = ConcreteNotifier({})
    assert n.should_send(_make_payload(severity="error"), ["warning", "error"]) is True


def test_should_send_non_matching_severity():
    class ConcreteNotifier(BaseNotifier):
        channel_type = "concrete"
        def send(self, payload):
            return DeliveryResult(channel_type=self.channel_type, success=True)

    n = ConcreteNotifier({})
    assert n.should_send(_make_payload(severity="info"), ["warning", "error"]) is False


def test_channel_registry_has_log():
    from bluei.engine.notifiers.log import LogNotifier
    assert "log" in CHANNEL_REGISTRY
    assert CHANNEL_REGISTRY["log"] is LogNotifier


def test_resolve_env_vars_present(monkeypatch):
    monkeypatch.setenv("MY_VAR", "hello")
    assert resolve_env_vars("${MY_VAR}") == "hello"


def test_resolve_env_vars_missing():
    assert resolve_env_vars("${NONEXISTENT_BLUEI_VAR_XYZ}") == "${NONEXISTENT_BLUEI_VAR_XYZ}"


def test_resolve_env_vars_embedded(monkeypatch):
    monkeypatch.setenv("TOKEN", "abc123")
    assert resolve_env_vars("Bearer ${TOKEN}") == "Bearer abc123"
