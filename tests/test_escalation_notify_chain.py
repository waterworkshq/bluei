"""Integration tests: escalation detection → notification delivery chain.

Validates that run_escalation_checks() output is directly consumable by
deliver_escalations(), and that the full pipeline writes both
escalation_log.jsonl and notification_log.jsonl with correct structure.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from bluei.engine.escalation import run_escalation_checks
from bluei.engine.notify import deliver_escalations


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data))


def _mock_response(status: int = 200, body: bytes = b"ok") -> MagicMock:
    mock = MagicMock()
    mock.status = status
    mock.read.return_value = body
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _setup_workspace(tmp_path: Path) -> dict:
    _write_yaml(
        tmp_path / "notifications.yaml",
        {
            "enabled": True,
            "channels": [{"type": "webhook", "url": "https://example.com/hook"}],
        },
    )
    return {
        "run_log": tmp_path / "run.log",
        "escalation_file": tmp_path / "state" / "escalation_log.jsonl",
    }


class TestMergeFailureChain:
    """Consecutive merge failures detected → webhook delivered."""

    def test_full_pipeline(self, tmp_path: Path) -> None:
        paths = _setup_workspace(tmp_path)
        paths["run_log"].write_text("")

        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            return_value=_mock_response(),
        ):
            findings = run_escalation_checks(
                run_log_file=paths["run_log"],
                escalation_file=paths["escalation_file"],
                issues_data={"issues": []},
                merges_failed=3,
                merges_succeeded=0,
            )
            results = deliver_escalations(
                findings, "test-repo", tmp_path, bypass_rate_limit=True
            )

        assert len(findings) == 1
        assert findings[0]["type"] == "consecutive_merge_failures"
        assert len(results) == 1
        assert results[0].success

        esc_log = tmp_path / "state" / "escalation_log.jsonl"
        assert esc_log.exists()
        esc_record = json.loads(esc_log.read_text().strip().splitlines()[0])
        assert esc_record["count"] == 1
        assert esc_record["findings"][0]["type"] == "consecutive_merge_failures"

        notif_log = tmp_path / "state" / "notification_log.jsonl"
        assert notif_log.exists()
        notif_record = json.loads(notif_log.read_text().strip().splitlines()[0])
        assert notif_record["repo"] == "test-repo"
        assert notif_record["severity"] == "error"
        assert notif_record["escalation_type"] == "consecutive_merge_failures"
        assert len(notif_record["deliveries"]) == 1
        assert notif_record["deliveries"][0]["success"] is True


class TestDedupSaturationChain:
    """Dedup saturation detected → notification delivered."""

    def test_full_pipeline(self, tmp_path: Path) -> None:
        paths = _setup_workspace(tmp_path)
        paths["run_log"].write_text(
            "batch-skip-duplicate: batch-1 existing PR #10\n"
            "batch-skip-duplicate: batch-2 existing PR #11\n"
            "batch-skip-duplicate: batch-3 existing PR #12\n"
        )

        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            return_value=_mock_response(),
        ):
            findings = run_escalation_checks(
                run_log_file=paths["run_log"],
                escalation_file=paths["escalation_file"],
                issues_data={"issues": []},
                merges_failed=0,
                merges_succeeded=1,
            )
            results = deliver_escalations(
                findings, "test-repo", tmp_path, bypass_rate_limit=True
            )

        assert len(findings) == 1
        assert findings[0]["type"] == "dedup_saturation"
        assert len(results) == 1
        assert results[0].success

        notif_log = tmp_path / "state" / "notification_log.jsonl"
        notif_record = json.loads(notif_log.read_text().strip().splitlines()[0])
        assert notif_record["escalation_type"] == "dedup_saturation"
        assert notif_record["severity"] == "warning"


class TestMultipleFindingTypesChain:
    """Multiple escalation types in one run → all delivered."""

    def test_full_pipeline(self, tmp_path: Path) -> None:
        paths = _setup_workspace(tmp_path)
        paths["run_log"].write_text(
            "batch-skip-duplicate: batch-1 existing PR #10\n"
            "batch-skip-duplicate: batch-2 existing PR #11\n"
            "batch-skip-duplicate: batch-3 existing PR #12\n"
        )

        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            return_value=_mock_response(),
        ):
            findings = run_escalation_checks(
                run_log_file=paths["run_log"],
                escalation_file=paths["escalation_file"],
                issues_data={"issues": []},
                merges_failed=3,
                merges_succeeded=0,
            )
            results = deliver_escalations(
                findings, "test-repo", tmp_path, bypass_rate_limit=True
            )

        assert len(findings) == 2
        types = {f["type"] for f in findings}
        assert "consecutive_merge_failures" in types
        assert "dedup_saturation" in types
        assert len(results) == 2
        assert all(r.success for r in results)

        esc_log = tmp_path / "state" / "escalation_log.jsonl"
        esc_record = json.loads(esc_log.read_text().strip().splitlines()[0])
        assert esc_record["count"] == 2
        assert len(esc_record["findings"]) == 2

        notif_log = tmp_path / "state" / "notification_log.jsonl"
        notif_lines = notif_log.read_text().strip().splitlines()
        assert len(notif_lines) == 2
        notif_types = {json.loads(line)["escalation_type"] for line in notif_lines}
        assert notif_types == {"consecutive_merge_failures", "dedup_saturation"}


class TestCleanRunNoNotifications:
    """No findings → nothing delivered, no logs written."""

    def test_full_pipeline(self, tmp_path: Path) -> None:
        paths = _setup_workspace(tmp_path)
        paths["run_log"].write_text("all good\n")

        findings = run_escalation_checks(
            run_log_file=paths["run_log"],
            escalation_file=paths["escalation_file"],
            issues_data={"issues": []},
            merges_failed=0,
            merges_succeeded=2,
        )

        assert findings == []

        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            return_value=_mock_response(),
        ):
            results = deliver_escalations(
                findings, "test-repo", tmp_path, bypass_rate_limit=True
            )

        assert results == []
        assert not (tmp_path / "state" / "escalation_log.jsonl").exists()
        assert not (tmp_path / "state" / "notification_log.jsonl").exists()


class TestRebaseConflictChain:
    """Rebase conflict trend → notification delivered."""

    def test_full_pipeline(self, tmp_path: Path) -> None:
        paths = _setup_workspace(tmp_path)
        paths["run_log"].write_text("")

        rebase_stats = tmp_path / "rebase_stats.jsonl"
        rebase_stats.write_text(
            '{"conflicted": [{"pr": 1}], "rebased": []}\n'
            '{"conflicted": [{"pr": 2}], "rebased": []}\n'
            '{"conflicted": [{"pr": 3}], "rebased": []}\n'
        )

        with patch(
            "bluei.engine.notifiers.webhook.urllib.request.urlopen",
            return_value=_mock_response(),
        ):
            findings = run_escalation_checks(
                run_log_file=paths["run_log"],
                escalation_file=paths["escalation_file"],
                issues_data={"issues": []},
                merges_failed=0,
                merges_succeeded=1,
                rebase_stats_file=rebase_stats,
            )
            results = deliver_escalations(
                findings, "test-repo", tmp_path, bypass_rate_limit=True
            )

        assert len(findings) == 1
        assert findings[0]["type"] == "rebase_conflict_trend"
        assert len(results) == 1
        assert results[0].success

        notif_log = tmp_path / "state" / "notification_log.jsonl"
        notif_record = json.loads(notif_log.read_text().strip().splitlines()[0])
        assert notif_record["escalation_type"] == "rebase_conflict_trend"
        assert notif_record["severity"] == "warning"


class TestEscalationLogSharedAcrossLayers:
    """engine run_escalation_checks and app write_escalation share the same log."""

    def test_both_write_to_same_file(self, tmp_path: Path) -> None:
        from bluei.app.escalation import write_escalation

        esc_file = tmp_path / "state" / "escalation_log.jsonl"
        paths = _setup_workspace(tmp_path)

        write_escalation(
            "manual escalation",
            severity="warning",
            repo="my-repo",
            escalation_file=esc_file,
        )

        run_log = tmp_path / "run.log"
        run_log.write_text("")
        run_escalation_checks(
            run_log_file=run_log,
            escalation_file=esc_file,
            issues_data={"issues": []},
            merges_failed=3,
            merges_succeeded=0,
        )

        lines = [l for l in esc_file.read_text().strip().splitlines() if l.strip()]
        assert len(lines) == 2
        details = [json.loads(l)["findings"][0]["detail"] for l in lines]
        assert "[my-repo] manual escalation" in details
        assert any("consecutive merge failures" in d for d in details)
