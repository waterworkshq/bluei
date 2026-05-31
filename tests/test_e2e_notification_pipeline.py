#!/usr/bin/env python3
"""E2E notification pipeline tests — config loading, escalation, digest."""

import json
from pathlib import Path

import pytest


class TestLoadNotificationConfig:
    def test_default_config_when_no_file(self, tmp_path):
        from bluei.engine.notify import load_notification_config

        config = load_notification_config("test-repo", tmp_path)
        assert "enabled" in config
        assert config["enabled"] is False

    def test_loads_global_config(self, tmp_path):
        from bluei.engine.notify import load_notification_config

        (tmp_path / "notifications.yaml").write_text(
            "enabled: true\n"
            "channels:\n"
            "  - type: webhook\n"
            "    url: https://example.com/hook\n"
        )

        config = load_notification_config(None, tmp_path)
        assert config["enabled"] is True
        assert len(config["channels"]) == 1
        assert config["channels"][0]["type"] == "webhook"

    def test_repo_config_overrides_global(self, tmp_path):
        from bluei.engine.notify import load_notification_config

        (tmp_path / "notifications.yaml").write_text(
            "enabled: true\n"
            "channels:\n"
            "  - type: webhook\n"
            "    url: https://global.example.com/hook\n"
        )

        repo_dir = tmp_path / "repos" / "my-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("notifications:\n  enabled: false\n")

        config = load_notification_config("my-repo", tmp_path)
        assert config["enabled"] is False

    def test_invalid_yaml_falls_back(self, tmp_path):
        from bluei.engine.notify import load_notification_config

        (tmp_path / "notifications.yaml").write_text("{{invalid yaml")

        config = load_notification_config(None, tmp_path)
        assert "enabled" in config


class TestDeliverEscalations:
    def test_disabled_returns_empty(self, tmp_path):
        from bluei.engine.notify import deliver_escalations

        findings = [{"type": "health_drop", "detail": "score dropped"}]
        results = deliver_escalations(findings, "test-repo", workspace=tmp_path)
        assert results == []

    def test_enabled_with_no_channels_returns_empty(self, tmp_path):
        from bluei.engine.notify import deliver_escalations

        (tmp_path / "notifications.yaml").write_text("enabled: true\nchannels: []\n")

        findings = [{"type": "health_drop", "detail": "score dropped"}]
        results = deliver_escalations(findings, "test-repo", workspace=tmp_path)
        assert results == []


class TestDeliverDigest:
    def test_disabled_returns_empty(self, tmp_path):
        from bluei.engine.notify import deliver_digest

        results = deliver_digest("test-repo", workspace=tmp_path)
        assert results == []

    def test_enabled_no_digest_config_returns_empty(self, tmp_path):
        from bluei.engine.notify import deliver_digest

        (tmp_path / "notifications.yaml").write_text(
            "enabled: true\nchannels:\n  - type: webhook\n    url: http://x\n"
        )

        results = deliver_digest("test-repo", workspace=tmp_path)
        assert results == []


class TestSeverityMap:
    def test_known_types_have_severity(self):
        from bluei.engine.notify import SEVERITY_MAP

        assert len(SEVERITY_MAP) > 0
        for key, value in SEVERITY_MAP.items():
            assert value in ("critical", "warning", "info", "error")
