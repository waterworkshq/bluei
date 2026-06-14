"""Tests for bluei/engine/notify.py — digest generation, edge cases, title building."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from bluei.engine.notify import (
    _build_title,
    _check_cooldown,
    _check_hourly_cap,
    _generate_digest,
    _read_json,
    _read_jsonl,
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


class TestBuildTitle:
    def test_uses_detail_when_present(self):
        result = _build_title({"type": "test", "detail": "Something went wrong"})
        assert result == "Something went wrong"

    def test_truncates_long_detail(self):
        detail = "x" * 200
        result = _build_title({"type": "test", "detail": detail})
        assert len(result) == 120

    def test_falls_back_to_type(self):
        result = _build_title({"type": "consecutive_merge_failures"})
        assert "consecutive_merge_failures" in result

    def test_unknown_type(self):
        result = _build_title({"type": "unknown"})
        assert "unknown" in result


class TestReadJsonl:
    def test_nonexistent_file(self, tmp_path):
        result = _read_jsonl(tmp_path / "missing.jsonl")
        assert result == []

    def test_reads_entries(self, tmp_path):
        p = tmp_path / "test.jsonl"
        _write_jsonl(p, [{"a": 1}, {"b": 2}])
        result = _read_jsonl(p)
        assert len(result) == 2

    def test_respects_limit(self, tmp_path):
        p = tmp_path / "test.jsonl"
        for i in range(30):
            _write_jsonl(p, [{"i": i}])
        result = _read_jsonl(p, limit=5)
        assert len(result) == 5

    def test_corrupt_json_skipped(self, tmp_path):
        p = tmp_path / "test.jsonl"
        p.write_text('{"a":1}\nnot-json\n{"b":2}\n')
        result = _read_jsonl(p)
        assert result == []

    def test_empty_lines_skipped(self, tmp_path):
        p = tmp_path / "test.jsonl"
        p.write_text('{"a":1}\n\n\n{"b":2}\n')
        result = _read_jsonl(p)
        assert len(result) == 2


class TestReadJson:
    def test_nonexistent_file(self, tmp_path):
        assert _read_json(tmp_path / "missing.json") == {}

    def test_reads_json(self, tmp_path):
        p = tmp_path / "test.json"
        p.write_text('{"key": "value"}')
        assert _read_json(p) == {"key": "value"}

    def test_corrupt_json_returns_empty(self, tmp_path):
        p = tmp_path / "test.json"
        p.write_text("not json")
        assert _read_json(p) == {}


class TestGenerateDigest:
    def test_no_state_data(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = _generate_digest(state_dir)
        assert "no state data" in result

    def test_health_trend_summary(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _write_jsonl(
            state_dir / "health_trend.jsonl",
            [
                {"repo": "proj-a", "vitality": 85, "open_issues": 3},
                {"repo": "proj-b", "vitality": 70, "open_issues": 8},
            ],
        )
        result = _generate_digest(state_dir)
        assert "proj-a" in result
        assert "proj-b" in result

    def test_review_stats_idle(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _write_jsonl(
            state_dir / "review_stats.jsonl",
            [{"active_prs": 0, "blocked_prs": 0}],
        )
        result = _generate_digest(state_dir)
        assert "idle" in result

    def test_review_stats_active(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _write_jsonl(
            state_dir / "review_stats.jsonl",
            [{"active_prs": 3, "blocked_prs": 1, "retry_failed": 2, "merge_ready": 1}],
        )
        result = _generate_digest(state_dir)
        assert "active=3" in result
        assert "blocked=1" in result

    def test_escalation_findings(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _write_jsonl(
            state_dir / "escalation_log.jsonl",
            [
                {
                    "findings": [
                        {"type": "warning", "detail": "Something bad"},
                        {"type": "resolved", "detail": "Fixed"},
                    ]
                }
            ],
        )
        result = _generate_digest(state_dir)
        assert "escalations:" in result
        assert "Something bad" in result


class TestCooldownEdgeCases:
    def test_malformed_timestamp_in_log(self, tmp_path):
        log_path = tmp_path / "state" / "notification_log.jsonl"
        _write_jsonl(
            log_path,
            [
                {
                    "timestamp": "not-a-date",
                    "escalation_type": "test",
                    "detail_hash": "abc",
                },
            ],
        )
        config = {"rate_limit": {"cooldown_seconds": 300}}
        result = _check_cooldown(config, "test", "abc", tmp_path)
        assert result is True

    def test_empty_url_passes_hourly(self, tmp_path):
        config = {"rate_limit": {"max_per_hour": 5}}
        result = _check_hourly_cap(config, "", tmp_path)
        assert result is True

    def test_log_with_empty_lines(self, tmp_path):
        log_path = tmp_path / "state" / "notification_log.jsonl"
        log_path.parent.mkdir(parents=True)
        log_path.write_text("\n\n\n")
        config = {"rate_limit": {"cooldown_seconds": 300}}
        assert _check_cooldown(config, "test", "abc", tmp_path) is True


class TestDeliverEscalationsEdgeCases:
    def test_bypass_rate_limit(self, tmp_path):
        _write_yaml(
            tmp_path / "notifications.yaml",
            {
                "enabled": True,
                "channels": [{"type": "webhook", "url": "https://example.com/hook"}],
                "rate_limit": {"cooldown_seconds": 3600, "max_per_hour": 1},
            },
        )
        log_path = tmp_path / "state" / "notification_log.jsonl"
        now = datetime.now(timezone.utc).isoformat()
        for i in range(25):
            _write_jsonl(
                log_path,
                [
                    {
                        "timestamp": now,
                        "escalation_type": f"type_{i}",
                        "detail_hash": f"hash_{i}",
                        "deliveries": [
                            {
                                "channel_type": "webhook",
                                "success": True,
                                "url": "https://example.com/hook",
                            }
                        ],
                    }
                ],
            )

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            findings = [{"type": "consecutive_merge_failures", "detail": "bypass test"}]
            results = deliver_escalations(
                findings, "test-repo", tmp_path, bypass_rate_limit=True
            )
        assert len(results) == 1


class TestDeliverDigestEdgeCases:
    def test_digest_disabled_returns_empty(self, tmp_path):
        _write_yaml(
            tmp_path / "notifications.yaml",
            {"enabled": True, "digest": {"enabled": False}},
        )
        results = deliver_digest("test-repo", tmp_path)
        assert results == []

    def test_digest_no_channels_at_all(self, tmp_path):
        _write_yaml(
            tmp_path / "notifications.yaml",
            {
                "enabled": True,
                "channels": [],
                "digest": {"enabled": True, "schedule": "daily", "channels": []},
            },
        )
        results = deliver_digest("test-repo", tmp_path)
        assert results == []

    def test_digest_with_state_data(self, tmp_path):
        _write_yaml(
            tmp_path / "notifications.yaml",
            {
                "enabled": True,
                "channels": [{"type": "webhook", "url": "https://digest.example.com"}],
                "digest": {"enabled": True, "schedule": "daily"},
            },
        )
        state_dir = tmp_path / "repos" / "test-repo" / "state"
        state_dir.mkdir(parents=True)
        _write_jsonl(
            state_dir / "health_trend.jsonl",
            [{"repo": "test-repo", "vitality": 90, "open_issues": 2}],
        )

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            results = deliver_digest("test-repo", tmp_path)
        assert len(results) == 1


class TestMaskSensitiveEdgeCases:
    def test_long_value_without_scheme(self):
        result = mask_sensitive("abcdefghijkl")
        assert result == "abcd***"

    def test_short_token_fully_masked(self):
        result = mask_sensitive("abcdef")
        assert result == "***"

    def test_colon_separator(self):
        result = mask_sensitive("key:secret123")
        assert result == "key:***"

    def test_exactly_four_chars(self):
        result = mask_sensitive("abcd")
        assert result == "***"


# ── Gap-closing tests: cooldown/hourly-cap through deliver_escalations ──


class TestCooldownThroughDelivery:
    """Test cooldown behavior via the public deliver_escalations entry point."""

    def test_hourly_cap_blocks_channel_delivery(self, tmp_path):
        """When hourly cap is hit for a channel URL, delivery is skipped."""
        _write_yaml(
            tmp_path / "notifications.yaml",
            {
                "enabled": True,
                "channels": [{"type": "webhook", "url": "https://hook.example.com"}],
                "rate_limit": {"cooldown_seconds": 0, "max_per_hour": 1},
            },
        )
        log_path = tmp_path / "state" / "notification_log.jsonl"
        now = datetime.now(timezone.utc).isoformat()
        _write_jsonl(
            log_path,
            [
                {
                    "timestamp": now,
                    "escalation_type": "other_type",
                    "detail_hash": "other_hash",
                    "deliveries": [
                        {
                            "channel_type": "webhook",
                            "success": True,
                            "url": "https://hook.example.com",
                        }
                    ],
                }
            ],
        )

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            findings = [{"type": "consecutive_merge_failures", "detail": "3 failures"}]
            results = deliver_escalations(findings, "test-repo", tmp_path)
        assert results == []

    def test_multiple_findings_one_cooldown_blocked(self, tmp_path):
        """First finding is in cooldown, second finding passes through."""
        _write_yaml(
            tmp_path / "notifications.yaml",
            {
                "enabled": True,
                "channels": [{"type": "webhook", "url": "https://hook.example.com"}],
                "rate_limit": {"cooldown_seconds": 3600, "max_per_hour": 20},
            },
        )
        log_path = tmp_path / "state" / "notification_log.jsonl"
        now = datetime.now(timezone.utc).isoformat()
        from bluei.engine.notify import _detail_hash

        _write_jsonl(
            log_path,
            [
                {
                    "timestamp": now,
                    "escalation_type": "consecutive_merge_failures",
                    "detail_hash": _detail_hash("stale detail"),
                    "deliveries": [{"channel_type": "webhook", "success": True}],
                }
            ],
        )

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            findings = [
                {"type": "consecutive_merge_failures", "detail": "stale detail"},
                {"type": "reappearing_finding", "detail": "new detail"},
            ]
            results = deliver_escalations(findings, "test-repo", tmp_path)
        assert len(results) == 1
        assert results[0].success

    def test_unknown_escalation_type_gets_default_severity(self, tmp_path):
        """Unknown escalation types map to 'warning' (the default)."""
        _write_yaml(
            tmp_path / "notifications.yaml",
            {
                "enabled": True,
                "channels": [{"type": "webhook", "url": "https://hook.example.com"}],
            },
        )
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            findings = [{"type": "totally_unknown_type", "detail": "mystery"}]
            results = deliver_escalations(findings, "test-repo", tmp_path)

        log_path = tmp_path / "state" / "notification_log.jsonl"
        record = json.loads(log_path.read_text().strip().splitlines()[0])
        assert record["severity"] == "warning"


class TestConfigEdgeCases:
    """Config loading edge cases: parse errors, falsy overrides."""

    def test_global_yaml_parse_error_uses_defaults(self, tmp_path):
        bad_yaml = tmp_path / "notifications.yaml"
        bad_yaml.write_text("enabled: true\nchannels: [not valid yaml {{{")
        config = load_notification_config(None, tmp_path)
        assert config["enabled"] is False

    def test_repo_yaml_parse_error_uses_global(self, tmp_path):
        _write_yaml(tmp_path / "notifications.yaml", {"enabled": True})
        repo_dir = tmp_path / "repos" / "my-project"
        repo_dir.mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("notifications: {enabled: true\nbad yaml")
        config = load_notification_config("my-project", tmp_path)
        assert config["enabled"] is True

    def test_falsy_repo_notifications_does_not_override(self, tmp_path):
        _write_yaml(
            tmp_path / "notifications.yaml",
            {
                "enabled": True,
                "channels": [{"type": "webhook", "url": "https://global.com"}],
            },
        )
        repo_dir = tmp_path / "repos" / "my-project"
        _write_yaml(repo_dir / "config.yaml", {"notifications": None})
        config = load_notification_config("my-project", tmp_path)
        assert config["enabled"] is True
        assert config["channels"][0]["url"] == "https://global.com"


class TestDigestGenerationEdgeCases:
    """Digest content generation edge cases."""

    def test_multiple_repos_grouped_in_digest(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _write_jsonl(
            state_dir / "health_trend.jsonl",
            [
                {"repo": "alpha", "vitality": 90, "open_issues": 1},
                {"repo": "beta", "vitality": 60, "open_issues": 5},
                {"repo": "alpha", "vitality": 92, "open_issues": 0},
            ],
        )
        result = _generate_digest(state_dir)
        lines = result.split("\n")
        repo_lines = [l for l in lines if ": vitality=" in l]
        assert len(repo_lines) == 2
        assert any("alpha" in l and "92" in l for l in repo_lines)

    def test_escalation_findings_truncated_to_five(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        findings = [{"type": "warning", "detail": f"Finding {i}"} for i in range(8)]
        _write_jsonl(
            state_dir / "escalation_log.jsonl",
            [{"findings": findings}],
        )
        result = _generate_digest(state_dir)
        detail_lines = [l for l in result.split("\n") if l.strip().startswith("- ")]
        assert len(detail_lines) == 5

    def test_resolved_findings_excluded_from_digest(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _write_jsonl(
            state_dir / "escalation_log.jsonl",
            [
                {
                    "findings": [
                        {"type": "resolved", "detail": "All good"},
                        {"type": "warning", "detail": "Still broken"},
                    ]
                }
            ],
        )
        result = _generate_digest(state_dir)
        assert "All good" not in result
        assert "Still broken" in result


class TestDeliverDigestLogWritten:
    """Verify digest delivery writes a notification log entry."""

    def test_digest_writes_notification_log(self, tmp_path):
        _write_yaml(
            tmp_path / "notifications.yaml",
            {
                "enabled": True,
                "channels": [{"type": "webhook", "url": "https://digest.example.com"}],
                "digest": {"enabled": True, "schedule": "daily"},
            },
        )
        state_dir = tmp_path / "repos" / "test-repo" / "state"
        state_dir.mkdir(parents=True)

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            results = deliver_digest("test-repo", tmp_path)

        log_path = tmp_path / "state" / "notification_log.jsonl"
        assert log_path.exists()
        record = json.loads(log_path.read_text().strip())
        assert record["escalation_type"] == "health_digest"
