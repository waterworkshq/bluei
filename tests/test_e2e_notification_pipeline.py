#!/usr/bin/env python3
"""E2E notification pipeline tests — config loading, escalation, digest."""

import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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


class TestIntegratedDelivery:
    """Integrated scenarios: real config + real rate-limit log + mocked HTTP.

    These exercise the full path: detector finding → deliver_escalations →
    notifier channel → urllib (mocked) → notification_log.jsonl (real file).
    Only the HTTP boundary is mocked; every other component is exercised for
    real against a tmp_path workspace.
    """

    # ── helpers ──

    @staticmethod
    def _ok_response():
        """A context-manager mock that urllib.request.urlopen can `with`."""
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = b"ok"
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    @staticmethod
    def _http_error(url: str, code: int, body: bytes = b"server error"):
        """An HTTPError with a working .read() (fp=None breaks the default)."""
        err = urllib.error.HTTPError(
            url=url,
            code=code,
            msg="Server Error",
            hdrs={},
            fp=None,
        )
        err.read = lambda: body
        return err

    # ── test 1: full pipeline end-to-end ──

    def test_full_pipeline_escalation_to_delivery(self, tmp_path):
        """Real escalation finding → deliver_escalations → webhook receives
        POST with the correct envelope → notification_log.jsonl records it.

        Crosses: bluei.engine.escalation (detector)
               → bluei.engine.notify (dispatcher + severity map + title builder)
               → bluei.engine.notifiers.webhook (payload formatter + send)
               → bluei.engine.notifiers.log (jsonl writer)
               → bluei.engine.jsonl (append_jsonl).
        """
        from bluei.engine.escalation import check_merge_failure_pattern
        from bluei.engine.notify import deliver_escalations

        # 1. Real channel config on disk (no bypass flags).
        (tmp_path / "notifications.yaml").write_text(
            "enabled: true\n"
            "channels:\n"
            "  - type: webhook\n"
            "    url: https://hooks.example.com/bluei-escalation\n"
        )

        # 2. Use the production detector to mint a real escalation finding —
        #    no hand-rolled dicts.
        finding = check_merge_failure_pattern(
            merges_failed=4,
            merges_succeeded=0,
        )
        assert finding is not None, "detector should fire on 4 failures, 0 successes"
        findings = [finding]

        # 3. Mock the HTTP boundary only; capture every request for inspection.
        captured_requests = []

        def _capture_urlopen(req, timeout=10):
            captured_requests.append(req)
            return self._ok_response()

        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            side_effect=_capture_urlopen,
        ):
            results = deliver_escalations(
                findings,
                repo_name="acme-repo",
                workspace=tmp_path,
            )

        # 4. DeliveryResult observable outcome.
        assert len(results) == 1, "one channel should deliver once"
        assert results[0].success
        assert results[0].channel_type == "webhook"
        assert results[0].status_code == 200

        # 5. The webhook endpoint received a POST with the right envelope.
        assert len(captured_requests) == 1, "endpoint should be hit exactly once"
        sent = captured_requests[0]
        assert sent.full_url == "https://hooks.example.com/bluei-escalation"
        assert sent.get_method() == "POST"
        assert sent.get_header("Content-type") == "application/json"
        assert sent.get_header("User-agent") == "bluei/2.6.0"

        body = json.loads(sent.data.decode("utf-8"))
        assert body["source"] == "bluei"
        assert body["repo"] == "acme-repo"
        assert body["escalation_type"] == "consecutive_merge_failures"
        assert body["severity"] == "error"  # SEVERITY_MAP entry
        assert "consecutive merge failures" in body["title"]
        assert body["body"] == finding["detail"]
        assert body["findings"] == findings, "raw findings must round-trip"

        # 6. notification_log.jsonl contains a delivery record matching the run.
        log_path = tmp_path / "state" / "notification_log.jsonl"
        assert log_path.exists(), "log file should be created"
        lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1, "exactly one log entry for one delivery"

        record = json.loads(lines[0])
        assert record["repo"] == "acme-repo"
        assert record["severity"] == "error"
        assert record["escalation_type"] == "consecutive_merge_failures"
        assert record["detail_hash"], "detail_hash must be populated for cooldown"

        deliveries = record["deliveries"]
        assert len(deliveries) == 1
        d = deliveries[0]
        assert d["channel_type"] == "webhook"
        assert d["success"] is True
        assert d["status_code"] == 200
        assert d["url"] == "https://hooks.example.com/bluei-escalation"

    # ── test 2: multi-channel partial failure ──

    def test_multi_channel_partial_failure(self, tmp_path):
        """Configure webhook + slack → webhook returns 200, slack raises 500
        → webhook delivered, slack marked failed (after 3 retries), log
        records BOTH outcomes.

        Crosses: bluei.engine.notify (multi-channel fan-out)
               → bluei.engine.notifiers.webhook (success path)
               → bluei.engine.notifiers.slack (retry-on-5xx path)
               → bluei.engine.notifiers.log (records every attempt).
        """
        from bluei.engine.notify import deliver_escalations

        # 1. Two real channels.
        (tmp_path / "notifications.yaml").write_text(
            "enabled: true\n"
            "channels:\n"
            "  - type: webhook\n"
            "    url: https://hooks.example.com/webhook\n"
            "  - type: slack\n"
            "    url: https://hooks.slack.com/services/T00/B00/xxx\n"
        )
        findings = [
            {"type": "reappearing_finding", "detail": "ruff-S602 keeps coming back"},
        ]

        # 2. Mock the HTTP boundary once — webhook.py and slack.py both call
        #    the same urllib.request.urlopen symbol, so a single patch with a
        #    URL-dispatching side_effect covers both channels. Slack 500s
        #    (retried 3× with a 1s sleep); webhook 200s.
        webhook_calls = []
        slack_calls = []

        def _dispatch(req, timeout=10):
            url = req.full_url
            if "hooks.slack.com" in url:
                slack_calls.append(url)
                raise self._http_error(url, code=500, body=b"internal error")
            webhook_calls.append(url)
            return self._ok_response()

        # NOTE: webhook.py and slack.py both `import urllib.request`, so
        # patching either reference patches the shared global. We patch the
        # webhook path; slack.send() picks up the same mock automatically.
        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            side_effect=_dispatch,
        ):
            # Skip the 1s retry sleeps on the slack 5xx path so the test
            # does not take 2s of wall time.
            with patch("bluei.engine.notifiers.slack.time.sleep"):
                results = deliver_escalations(
                    findings,
                    repo_name="acme-repo",
                    workspace=tmp_path,
                )

        # 3. Mixed outcome: one success (webhook), one failure (slack).
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        assert len(successes) == 1, "webhook should succeed exactly once"
        assert successes[0].channel_type == "webhook"
        assert successes[0].status_code == 200

        assert len(failures) == 1, "slack should fail"
        assert failures[0].channel_type == "slack"
        assert failures[0].status_code == 500
        assert "internal error" in failures[0].error

        # 4. Slack retried 3× (slack.py: 500 ∉ {400,403,404,410} → retry up to 3).
        #    Webhook is non-retrying on 200, so it fires once.
        assert len(slack_calls) == 3, "slack should retry 5xx up to 3 times"
        assert len(webhook_calls) == 1, "webhook should fire exactly once"
        assert slack_calls[0] == "https://hooks.slack.com/services/T00/B00/xxx"
        assert webhook_calls[0] == "https://hooks.example.com/webhook"

        # 5. notification_log.jsonl records BOTH outcomes in a single entry.
        log_path = tmp_path / "state" / "notification_log.jsonl"
        assert log_path.exists()
        lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1, "one deliver_escalations call → one log entry"

        record = json.loads(lines[0])
        deliveries = record["deliveries"]
        assert len(deliveries) == 2, "both channel attempts must be logged"

        by_type = {d["channel_type"]: d for d in deliveries}
        assert by_type["webhook"]["success"] is True
        assert by_type["webhook"]["status_code"] == 200
        assert by_type["slack"]["success"] is False
        assert by_type["slack"]["status_code"] == 500

    # ── test 3: rate-limit cooldown across time ──

    def test_rate_limit_blocks_second_delivery(self, tmp_path):
        """First delivery succeeds → same finding blocked by 300s cooldown →
        advance time past cooldown → delivery succeeds again.

        Uses a real notification_log.jsonl on tmp_path; rate limiting and the
        cooldown cutoff are exercised for real. Time is advanced by patching
        bluei.engine.notify.datetime with a frozen subclass (freezegun is not
        a dependency).

        Crosses: bluei.engine.notify (_check_cooldown, _check_hourly_cap,
                                      _detail_hash, deliver_escalations)
               → bluei.engine.notifiers.webhook (send)
               → bluei.engine.notifiers.log (writes the cooldown key).
        """
        from bluei.engine.notify import _detail_hash, deliver_escalations

        # 1. Real config with a 300s cooldown.
        (tmp_path / "notifications.yaml").write_text(
            "enabled: true\n"
            "channels:\n"
            "  - type: webhook\n"
            "    url: https://hooks.example.com/webhook\n"
            "rate_limit:\n"
            "  cooldown_seconds: 300\n"
            "  max_per_hour: 20\n"
        )
        findings = [
            {"type": "consecutive_merge_failures", "detail": "rate-limit scenario"},
        ]

        http_calls = {"n": 0}

        def _counting_urlopen(req, timeout=10):
            http_calls["n"] += 1
            return self._ok_response()

        # ── Phase 1: first delivery succeeds, log written ──
        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            side_effect=_counting_urlopen,
        ):
            r1 = deliver_escalations(
                findings,
                repo_name="acme-repo",
                workspace=tmp_path,
            )
        assert len(r1) == 1
        assert r1[0].success
        assert http_calls["n"] == 1, "webhook should fire on first delivery"

        log_path = tmp_path / "state" / "notification_log.jsonl"
        assert log_path.exists()
        lines_after_p1 = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
        assert len(lines_after_p1) == 1
        record1 = json.loads(lines_after_p1[0])
        expected_hash = _detail_hash("rate-limit scenario")
        assert record1["detail_hash"] == expected_hash
        assert record1["escalation_type"] == "consecutive_merge_failures"

        # ── Phase 2: same finding — blocked by cooldown, no HTTP, no log ──
        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            side_effect=_counting_urlopen,
        ):
            r2 = deliver_escalations(
                findings,
                repo_name="acme-repo",
                workspace=tmp_path,
            )
        assert r2 == [], "cooldown should block re-delivery of same finding"
        assert http_calls["n"] == 1, "webhook must NOT fire while in cooldown"

        lines_after_p2 = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
        assert len(lines_after_p2) == 1, "blocked attempts must not append a log entry"

        # ── Phase 3: advance simulated time past cooldown → succeeds again ──
        # FakeDateTime subclasses datetime so isinstance checks pass and
        # inherited classmethods (fromisoformat) still work for parsing the
        # Phase 1 entry written with the real clock.
        frozen = datetime.now(timezone.utc) + timedelta(seconds=301)

        class _FakeDateTime(datetime):
            _frozen = frozen

            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return cls._frozen
                return cls._frozen.astimezone(tz)

        with patch("bluei.engine.notify.datetime", _FakeDateTime):
            with patch(
                "bluei.engine.notifiers.webhook.urllib.request.urlopen",
                side_effect=_counting_urlopen,
            ):
                r3 = deliver_escalations(
                    findings,
                    repo_name="acme-repo",
                    workspace=tmp_path,
                )

        assert len(r3) == 1, "delivery should succeed once cooldown expires"
        assert r3[0].success
        assert http_calls["n"] == 2, "webhook should fire again after cooldown"

        lines_after_p3 = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
        assert len(lines_after_p3) == 2, "second successful delivery must be logged"
        record3 = json.loads(lines_after_p3[-1])
        assert record3["detail_hash"] == expected_hash, (
            "same finding → same detail_hash (the cooldown key)"
        )
