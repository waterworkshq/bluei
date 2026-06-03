"""Tests for escalation detection and persistence.

Focuses on behavior: "given these conditions, escalation is/is isn't detected."
Uses parametrize to condense threshold logic. Tests the flow via
run_escalation_checks rather than exercising each private helper in isolation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from bluei.app.escalation import (
    check_escalation_status,
    write_escalation,
    DEFAULT_ESCALATION_FILE as APP_DEFAULT_ESCALATION_FILE,
)
from bluei.engine.escalation import (
    EscalationConfig,
    check_cycle_escalation,
    check_dedup_saturation,
    check_max_duplicate_prs,
    check_merge_failure_pattern,
    check_reappearing_findings,
    check_rebase_conflict_trend,
    handle_max_duplicate_escalation,
    run_escalation_checks,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_pr(number: int, finding_id: str) -> dict:
    return {
        "number": number,
        "title": f"[bluei] fix {finding_id}",
        "url": f"https://github.com/owner/repo/pull/{number}",
        "body": f"Automated fix.\n- dedupe_key: {finding_id}\n",
    }


def _patch_esc_file(esc_mod, tmp_path: Path, records: list[dict]):
    """Write records to a temp file, patch module DEFAULT_ESCALATION_FILE, return original."""
    import bluei.engine.escalation as eng_esc

    esc_file = tmp_path / "state" / "escalation_log.jsonl"
    esc_file.parent.mkdir(parents=True, exist_ok=True)
    if records:
        esc_file.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    original_app = esc_mod.DEFAULT_ESCALATION_FILE
    original_eng = eng_esc.DEFAULT_ESCALATION_FILE
    esc_mod.DEFAULT_ESCALATION_FILE = esc_file
    eng_esc.DEFAULT_ESCALATION_FILE = esc_file
    return (original_app, original_eng)


# ── 1. Merge failure pattern ────────────────────────────────────────


class TestCheckMergeFailurePattern:
    """Consecutive merge failure detection."""

    @pytest.mark.parametrize(
        "failures, successes, thresholds, expected_type",
        [
            (3, 0, None, "consecutive_merge_failures"),
            (5, 0, {"consecutive_merge": 5}, "consecutive_merge_failures"),
            (2, 0, None, None),
            (3, 1, None, None),
            (0, 3, None, None),
        ],
    )
    def test_threshold_logic(self, failures, successes, thresholds, expected_type):
        result = check_merge_failure_pattern(failures, successes, thresholds)
        if expected_type is None:
            assert result is None
        else:
            assert result["type"] == expected_type
            assert result["count"] == failures


# ── 2. Reappearing findings ─────────────────────────────────────────


class TestCheckReappearingFindings:
    """Reappearing finding detection from issue history."""

    def test_flags_reappearing_finding(self):
        issues = {
            "issues": [
                {
                    "finding_id": "f42",
                    "rule": "ruff-c408",
                    "path": "src/foo.py",
                    "history": [
                        {"event": "open"},
                        {"event": "fix_failed_verification"},
                        {"event": "fix_failed_verification"},
                        {"event": "fix_failed_verification"},
                    ],
                }
            ]
        }
        result = check_reappearing_findings(issues)
        assert len(result) == 1
        assert result[0]["finding_id"] == "f42"
        assert result[0]["failed_attempts"] == 3

    @pytest.mark.parametrize(
        "issues",
        [
            {"issues": []},
            {
                "issues": [
                    {
                        "finding_id": "f1",
                        "history": [{"event": "open"}, {"event": "fix_success"}],
                    }
                ]
            },
            {"issues": [{"rule": "ruff-x", "history": [{"event": "open"}] * 5}]},
        ],
    )
    def test_no_escalation_for_clean_or_malformed(self, issues):
        assert check_reappearing_findings(issues) == []


# ── 3. Dedup saturation ─────────────────────────────────────────────


class TestCheckDedupSaturation:
    """Dedup guard saturation from log lines."""

    @pytest.mark.parametrize(
        "log_lines, expected_count",
        [
            (["batch-skip-duplicate: batch-1 existing PR #10"] * 3, 1),
            (["batch-skip-duplicate: batch-1"], 0),
            ([], 0),
            (["unrelated"] * 5, 0),
        ],
    )
    def test_saturation(self, log_lines, expected_count):
        result = check_dedup_saturation(log_lines)
        assert len(result) == expected_count
        if expected_count:
            assert result[0]["type"] == "dedup_saturation"


# ── 4. Rebase conflict trend ────────────────────────────────────────


class TestCheckRebaseConflictTrend:
    """Rebase conflict trend detection from stats file."""

    def test_flags_all_conflicts(self, tmp_path: Path):
        stats = tmp_path / "stats.jsonl"
        stats.write_text(
            "\n".join('{"conflicted":[{"pr":%d}],"rebased":[]}' % i for i in (1, 2, 3))
            + "\n"
        )
        result = check_rebase_conflict_trend(stats)
        assert len(result) == 1
        assert result[0]["type"] == "rebase_conflict_trend"

    @pytest.mark.parametrize(
        "content",
        [
            # Mixed: first has no conflicts
            '{"conflicted":[],"rebased":[{"pr":1}]}\n'
            '{"conflicted":[{"pr":2}],"rebased":[]}\n'
            '{"conflicted":[{"pr":3}],"rebased":[]}\n',
            # Not enough records
            '{"conflicted":[{"pr":1}],"rebased":[]}\n',
        ],
    )
    def test_no_escalation(self, tmp_path, content):
        stats = tmp_path / "stats.jsonl"
        stats.write_text(content)
        assert check_rebase_conflict_trend(stats) == []

    def test_missing_file(self, tmp_path):
        assert check_rebase_conflict_trend(tmp_path / "nope.jsonl") == []


# ── 5. Cycle escalation ─────────────────────────────────────────────


class TestCheckCycleEscalation:
    """Cycle-level consecutive failure detection."""

    @pytest.mark.parametrize(
        "threshold, statuses, finding_id, expected",
        [
            (3, ["failure", "failure", "failure"], "f1", "f1"),
            (3, ["failure", "success", "failure"], "f1", None),
            (3, ["failure", "failure"], "f1", None),
            (0, ["failure"], "f1", None),
        ],
    )
    def test_threshold_logic(self, threshold, statuses, finding_id, expected):
        config = EscalationConfig(consecutive_failure_threshold=threshold)
        log = [
            {"finding_id": finding_id, "status": s, "cycle_type": "pr-cycle"}
            for s in statuses
        ]
        result = check_cycle_escalation(config, log)
        if expected is None:
            assert result is None
        else:
            assert result["finding_id"] == expected

    def test_empty_log_and_missing_finding_id(self):
        config = EscalationConfig(consecutive_failure_threshold=2)
        assert check_cycle_escalation(config, []) is None
        assert (
            check_cycle_escalation(
                config, [{"status": "failure", "cycle_type": "pr-cycle"}] * 2
            )
            is None
        )

    def test_multi_finding_first_hit_wins(self):
        config = EscalationConfig(consecutive_failure_threshold=2)
        log = [
            {"finding_id": "f1", "status": "failure", "cycle_type": "pr-cycle"},
            {"finding_id": "f2", "status": "failure", "cycle_type": "pr-cycle"},
            {"finding_id": "f1", "status": "failure", "cycle_type": "pr-cycle"},
        ]
        result = check_cycle_escalation(config, log)
        assert result["finding_id"] == "f1"

    def test_status_case_insensitive(self):
        config = EscalationConfig(consecutive_failure_threshold=2)
        log = [
            {"finding_id": "f1", "status": "FAILURE", "cycle_type": "pr-cycle"},
            {"finding_id": "f1", "status": "Failure", "cycle_type": "merge-cycle"},
        ]
        result = check_cycle_escalation(config, log)
        assert result["cycle_type"] == "merge-cycle"


# ── 6. Max duplicate PRs ────────────────────────────────────────────


class TestCheckMaxDuplicatePRs:
    """Detection of findings with too many open PRs."""

    @patch("bluei.engine.gh.gh_json")
    def test_escalates_single_finding(self, mock_gh):
        mock_gh.return_value = [_make_pr(i, "f-a") for i in (10, 11, 12)]
        result = check_max_duplicate_prs("owner/repo", Path("/tmp"))
        assert len(result) == 1
        assert result[0]["open_pr_count"] == 3
        assert result[0]["pr_numbers"] == [10, 11, 12]

    @patch("bluei.engine.gh.gh_json")
    def test_multiple_findings_exceeding(self, mock_gh):
        mock_gh.return_value = [
            *_pr_list("f-a", 10, 3),
            *_pr_list("f-b", 20, 3),
        ]
        result = check_max_duplicate_prs("owner/repo", Path("/tmp"))
        assert len(result) == 2
        assert {r["finding_id"] for r in result} == {"f-a", "f-b"}

    @pytest.mark.parametrize(
        "gh_return",
        [
            [_make_pr(10, "a"), _make_pr(11, "a")],  # below threshold
            [],  # empty
            None,  # None
            {"error": "not a list"},  # not a list
        ],
    )
    @patch("bluei.engine.gh.gh_json")
    def test_no_escalation_cases(self, mock_gh, gh_return):
        mock_gh.return_value = gh_return
        assert check_max_duplicate_prs("owner/repo", Path("/tmp")) == []

    @pytest.mark.parametrize(
        "body",
        [
            "No key here",  # no dedupe_key
            None,  # None body
            "- dedupe_key:\n",  # empty value
            "- dedupe_key:  \n",  # whitespace only
        ],
    )
    @patch("bluei.engine.gh.gh_json")
    def test_invalid_dedupe_keys_ignored(self, mock_gh, body):
        mock_gh.return_value = [
            {"number": i, "title": "F", "url": "", "body": body} for i in (10, 11, 12)
        ]
        assert check_max_duplicate_prs("owner/repo", Path("/tmp")) == []

    @patch("bluei.engine.gh.gh_json")
    def test_whitespace_trimmed_and_custom_threshold(self, mock_gh):
        mock_gh.return_value = [
            {"number": i, "title": "F", "url": "", "body": "- dedupe_key: f-a  \n"}
            for i in (10, 11)
        ]
        result = check_max_duplicate_prs(
            "owner/repo", Path("/tmp"), thresholds={"max_duplicate_prs": 2}
        )
        assert len(result) == 1
        assert result[0]["finding_id"] == "f-a"
        assert result[0]["threshold"] == 2


def _pr_list(finding_id: str, start: int, count: int) -> list[dict]:
    return [_make_pr(start + i, finding_id) for i in range(count)]


# ── 7. Handle max duplicate escalation ──────────────────────────────


class TestHandleMaxDuplicateEscalation:
    """Closes excess PRs, routes findings to human review."""

    @staticmethod
    def _esc(finding_id="f1", prs=None, count=3, threshold=3):
        return {
            "finding_id": finding_id,
            "pr_numbers": prs or [10, 11, 12],
            "open_pr_count": count,
            "threshold": threshold,
        }

    @staticmethod
    def _issues(finding_id="f1", status="open"):
        return (
            {"issues": [{"finding_id": finding_id, "status": status}]}
            if finding_id
            else {"issues": []}
        )

    @patch("bluei.engine.state.mark_finding_activity")
    @patch("bluei.engine.escalation._append_text")
    def test_closes_excess_keeps_newest(self, _append, _mark, tmp_path):
        log = tmp_path / "log.txt"
        log.write_text("")
        with patch("bluei.engine.clean_prs._close_pr", return_value=True):
            r = handle_max_duplicate_escalation(
                [self._esc()], "o/r", tmp_path, {}, self._issues(), log
            )
        assert r["closed_prs"] == [10, 11]
        assert r["routed_findings"] == ["f1"]
        assert r["paused_findings"] == ["f1"]

    @patch("bluei.engine.state.mark_finding_activity")
    @patch("bluei.engine.escalation._append_text")
    def test_dry_run_skips_close(self, _append, _mark, tmp_path):
        log = tmp_path / "log.txt"
        log.write_text("")
        with patch("bluei.engine.clean_prs._close_pr") as mock_close:
            r = handle_max_duplicate_escalation(
                [self._esc()], "o/r", tmp_path, {}, self._issues(), log, dry_run=True
            )
        mock_close.assert_not_called()
        assert r["closed_prs"] == []

    @pytest.mark.parametrize("side_effect", [False, Exception("boom")])
    @patch("bluei.engine.state.mark_finding_activity")
    @patch("bluei.engine.escalation._append_text")
    def test_close_failure_recorded(self, _append, _mark, tmp_path, side_effect):
        log = tmp_path / "log.txt"
        log.write_text("")
        with patch("bluei.engine.clean_prs._close_pr", side_effect=side_effect):
            r = handle_max_duplicate_escalation(
                [self._esc(prs=[10, 11], count=2, threshold=2)],
                "o/r",
                tmp_path,
                {},
                self._issues(),
                log,
            )
        assert r["close_failed"] == [10]

    @patch("bluei.engine.state.mark_finding_activity")
    @patch("bluei.engine.escalation._append_text")
    def test_already_routed_and_empty_id_skipped(self, _append, _mark, tmp_path):
        log = tmp_path / "log.txt"
        log.write_text("")
        # Empty finding_id → nothing paused
        r1 = handle_max_duplicate_escalation(
            [{"finding_id": "", "pr_numbers": [10], "open_pr_count": 1}],
            "o/r",
            tmp_path,
            {},
            {"issues": []},
            log,
        )
        assert r1["paused_findings"] == []
        # Already routed → no duplicate routing
        with patch("bluei.engine.clean_prs._close_pr", return_value=True):
            r2 = handle_max_duplicate_escalation(
                [self._esc(prs=[10, 11], count=2, threshold=2)],
                "o/r",
                tmp_path,
                {},
                self._issues(status="needs-human-max-duplicates-exceeded"),
                log,
            )
        assert r2["routed_findings"] == []


# ── 8. Run escalation checks (integration) ──────────────────────────


class TestRunEscalationChecks:
    """Integration: run all checks end-to-end."""

    def test_all_clean_no_log(self, tmp_path):
        esc = tmp_path / "esc.jsonl"
        run_escalation_checks(tmp_path / "run.log", esc, {"issues": []}, 0, 2)
        assert not esc.exists()

    def test_merge_failure_logged(self, tmp_path):
        log = tmp_path / "run.log"
        log.write_text("")
        esc = tmp_path / "esc.jsonl"
        result = run_escalation_checks(log, esc, {"issues": []}, 4, 0)
        assert len(result) == 1
        assert result[0]["type"] == "consecutive_merge_failures"
        record = json.loads(esc.read_text().strip())
        assert record["findings"][0]["type"] == "consecutive_merge_failures"

    def test_rebase_trend_detected(self, tmp_path):
        stats = tmp_path / "rebase.jsonl"
        stats.write_text(
            "\n".join('{"conflicted":[{"pr":%d}],"rebased":[]}' % i for i in (1, 2, 3))
            + "\n"
        )
        result = run_escalation_checks(
            tmp_path / "run.log",
            tmp_path / "esc.jsonl",
            {"issues": []},
            0,
            1,
            rebase_stats_file=stats,
        )
        assert any(r["type"] == "rebase_conflict_trend" for r in result)

    @patch("bluei.engine.gh.gh_json")
    def test_max_duplicates_wired(self, mock_gh, tmp_path):
        mock_gh.return_value = [_make_pr(i, "f-a") for i in (10, 11, 12)]
        log = tmp_path / "run.log"
        log.write_text("")
        esc = tmp_path / "esc.jsonl"
        result = run_escalation_checks(
            log, esc, {"issues": []}, 0, 0, repo_slug="o/r", cwd=Path("/tmp")
        )
        assert any(r["type"] == "max_duplicate_prs" for r in result)
        records = [
            json.loads(l) for l in esc.read_text().strip().splitlines() if l.strip()
        ]
        max_dup = [
            f for f in records[0]["findings"] if f["type"] == "max_duplicate_prs"
        ]
        assert max_dup[0]["open_pr_count"] == 3

    def test_no_repo_slug_skips_max_dup(self, tmp_path):
        esc = tmp_path / "esc.jsonl"
        result = run_escalation_checks(tmp_path / "run.log", esc, {"issues": []}, 0, 0)
        assert "max_duplicate_prs" not in [r["type"] for r in result]

    def test_log_read_error_does_not_block(self, tmp_path):
        log = tmp_path / "run.log"
        log.write_text("data")
        esc = tmp_path / "esc.jsonl"
        with patch.object(Path, "read_text", side_effect=OSError("nope")):
            result = run_escalation_checks(log, esc, {"issues": []}, 3, 0)
        assert len(result) == 1


# ── 9. App-layer: write_escalation ──────────────────────────────────


class TestAppWriteEscalation:
    """JSONL record writer."""

    def test_structure_and_repo_prefix(self, tmp_path):
        esc = tmp_path / "state" / "esc.jsonl"
        write_escalation("merge failed", repo="acme/api", escalation_file=esc)
        record = json.loads(esc.read_text().strip())
        assert record["count"] == 1
        assert record["findings"][0]["type"] == "error"
        assert record["findings"][0]["detail"] == "[acme/api] merge failed"
        ts = datetime.fromisoformat(record["timestamp"])
        assert ts.tzinfo is not None

    def test_no_repo_uses_plain_message(self, tmp_path):
        esc = tmp_path / "state" / "esc.jsonl"
        write_escalation("something wrong", escalation_file=esc)
        record = json.loads(esc.read_text().strip())
        assert record["findings"][0]["detail"] == "something wrong"

    def test_severity_levels_and_append(self, tmp_path):
        esc = tmp_path / "state" / "esc.jsonl"
        for sev in ("info", "warning", "error"):
            write_escalation(f"msg-{sev}", severity=sev, escalation_file=esc)
        records = [
            json.loads(l) for l in esc.read_text().strip().splitlines() if l.strip()
        ]
        assert [r["findings"][0]["type"] for r in records] == [
            "info",
            "warning",
            "error",
        ]

    def test_creates_nested_parent_dirs(self, tmp_path):
        esc = tmp_path / "deep" / "nested" / "esc.jsonl"
        write_escalation("test", escalation_file=esc)
        assert esc.exists()

    def test_oserror_silent(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        esc = blocker / "esc.jsonl"
        write_escalation("should fail silently", escalation_file=esc)
        assert not esc.exists()


# ── 10. App-layer: check_escalation_status ──────────────────────────


class TestAppCheckEscalationStatus:
    """Status summary reader."""

    def test_empty_log(self):
        with patch.object(
            type(APP_DEFAULT_ESCALATION_FILE), "exists", lambda self: False
        ):
            status = check_escalation_status("my-repo")
        assert status == {
            "total_escalations": 0,
            "active_escalations": 0,
            "latest": None,
            "types": {},
            "repo": "my-repo",
        }

    def test_filters_by_repo_and_aggregates_types(self, tmp_path):
        import bluei.app.escalation as esc_mod
        import bluei.engine.escalation as eng_esc

        records = [
            {
                "timestamp": "2025-01-01T00:00:00+00:00",
                "findings": [{"type": "error", "detail": "[acme/api] crash"}],
                "count": 1,
            },
            {
                "timestamp": "2025-01-01T00:00:00+00:00",
                "findings": [
                    {"type": "error", "detail": "[acme/api] e2"},
                    {"type": "warning", "detail": "[acme/api] w1"},
                ],
                "count": 2,
            },
            {
                "timestamp": "2025-01-01T00:00:00+00:00",
                "findings": [{"type": "warning", "detail": "[other/repo] flaky"}],
                "count": 1,
            },
        ]
        orig = _patch_esc_file(esc_mod, tmp_path, records)
        try:
            s = check_escalation_status("acme/api")
        finally:
            esc_mod.DEFAULT_ESCALATION_FILE = orig[0]
            eng_esc.DEFAULT_ESCALATION_FILE = orig[1]
        assert s["total_escalations"] == 2
        assert s["types"] == {"error": 2, "warning": 1}

    def test_active_count_within_60_min(self, tmp_path):
        import bluei.app.escalation as esc_mod
        import bluei.engine.escalation as eng_esc

        records = [
            {
                "timestamp": "2020-01-01T00:00:00+00:00",
                "findings": [{"type": "warning", "detail": "[r] old"}],
                "count": 1,
            },
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "findings": [{"type": "error", "detail": "[r] new"}],
                "count": 1,
            },
        ]
        orig = _patch_esc_file(esc_mod, tmp_path, records)
        try:
            s = check_escalation_status("r")
        finally:
            esc_mod.DEFAULT_ESCALATION_FILE = orig[0]
            eng_esc.DEFAULT_ESCALATION_FILE = orig[1]
        assert s["total_escalations"] == 2
        assert s["active_escalations"] == 1
        assert s["latest"]["findings"][0]["detail"] == "[r] new"

    def test_no_matching_repo(self, tmp_path):
        import bluei.app.escalation as esc_mod
        import bluei.engine.escalation as eng_esc

        orig = _patch_esc_file(
            esc_mod,
            tmp_path,
            [
                {
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "findings": [{"type": "error", "detail": "[other/repo] crash"}],
                    "count": 1,
                },
            ],
        )
        try:
            s = check_escalation_status("acme/api")
        finally:
            esc_mod.DEFAULT_ESCALATION_FILE = orig[0]
            eng_esc.DEFAULT_ESCALATION_FILE = orig[1]
        assert s["total_escalations"] == 0

    @pytest.mark.parametrize(
        "label, raw",
        [
            ("corrupt_json", "{this is not valid json}\n"),
            (
                "empty_findings",
                '{"timestamp":"2025-01-01T00:00:00+00:00","findings":[],"count":0}\n'
                '{"timestamp":"2025-01-01T00:00:00+00:00","findings":[{"type":"error","detail":"[r] real"}],"count":1}\n',
            ),
            (
                "bad_timestamp",
                '{"timestamp":"not-a-date","findings":[{"type":"error","detail":"[r] bad"}],"count":1}\n'
                '{"timestamp":"%s","findings":[{"type":"error","detail":"[r] ok"}],"count":1}\n'
                % datetime.now(timezone.utc).isoformat(),
            ),
        ],
    )
    def test_malformed_data_handled(self, tmp_path, label, raw):
        import bluei.app.escalation as esc_mod
        import bluei.engine.escalation as eng_esc

        esc_file = tmp_path / "state" / "esc.jsonl"
        esc_file.parent.mkdir(parents=True)
        esc_file.write_text(raw)
        orig_app = esc_mod.DEFAULT_ESCALATION_FILE
        orig_eng = eng_esc.DEFAULT_ESCALATION_FILE
        esc_mod.DEFAULT_ESCALATION_FILE = esc_file
        eng_esc.DEFAULT_ESCALATION_FILE = esc_file
        try:
            s = check_escalation_status("r")
        finally:
            esc_mod.DEFAULT_ESCALATION_FILE = orig_app
            eng_esc.DEFAULT_ESCALATION_FILE = orig_eng
        assert isinstance(s["total_escalations"], int)
        assert isinstance(s["active_escalations"], int)


# ── 11. EscalationConfig, log_escalation_event, load_escalation_log ───


class TestEscalationConfig:
    """Config dataclass and defaults."""

    def test_default_values(self):
        cfg = EscalationConfig()
        assert cfg.consecutive_failure_threshold == 3
        assert cfg.silence_after_resolved_minutes == 60
        assert cfg.escalation_log_path is None

    def test_silence_seconds_property(self):
        cfg = EscalationConfig(silence_after_resolved_minutes=30)
        assert cfg.silence_seconds == 1800


class TestLogEscalationEvent:
    """log_escalation_event writes structured JSONL records."""

    def test_writes_record_with_envelope(self, tmp_path):
        from bluei.engine.escalation import log_escalation_event

        esc_file = tmp_path / "state" / "esc.jsonl"
        cfg = EscalationConfig(escalation_log_path=esc_file)
        event = {"type": "cycle_escalation", "finding_id": "f1", "detail": "bad"}
        log_escalation_event(cfg, event)

        record = json.loads(esc_file.read_text().strip())
        assert record["count"] == 1
        assert record["findings"][0]["type"] == "cycle_escalation"
        ts = datetime.fromisoformat(record["timestamp"])
        assert ts.tzinfo is not None

    def test_none_log_path_is_noop(self, tmp_path):
        from bluei.engine.escalation import log_escalation_event

        cfg = EscalationConfig(escalation_log_path=None)
        log_escalation_event(cfg, {"type": "test"})  # should not raise

    def test_creates_parent_dirs(self, tmp_path):
        from bluei.engine.escalation import log_escalation_event

        esc_file = tmp_path / "deep" / "nested" / "esc.jsonl"
        cfg = EscalationConfig(escalation_log_path=esc_file)
        log_escalation_event(cfg, {"type": "test"})
        assert esc_file.exists()

    def test_oserror_silent(self, tmp_path):
        from bluei.engine.escalation import log_escalation_event

        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        esc_file = blocker / "esc.jsonl"
        cfg = EscalationConfig(escalation_log_path=esc_file)
        log_escalation_event(cfg, {"type": "test"})  # should not raise
        assert not esc_file.exists()


class TestLoadEscalationLog:
    """load_escalation_log reads and reverses records."""

    def test_returns_newest_first(self, tmp_path):
        from bluei.engine.escalation import load_escalation_log

        esc_file = tmp_path / "esc.jsonl"
        esc_file.write_text(
            '{"timestamp":"2025-01-01T00:00:00Z","findings":[],"count":0}\n'
            '{"timestamp":"2025-01-02T00:00:00Z","findings":[],"count":0}\n'
        )
        cfg = EscalationConfig(escalation_log_path=esc_file)
        records = load_escalation_log(cfg)
        assert len(records) == 2
        assert records[0]["timestamp"] == "2025-01-02T00:00:00Z"

    def test_none_path_returns_empty(self):
        from bluei.engine.escalation import load_escalation_log

        cfg = EscalationConfig(escalation_log_path=None)
        assert load_escalation_log(cfg) == []

    def test_nonexistent_file_returns_empty(self, tmp_path):
        from bluei.engine.escalation import load_escalation_log

        cfg = EscalationConfig(escalation_log_path=tmp_path / "nope.jsonl")
        assert load_escalation_log(cfg) == []

    def test_corrupt_json_returns_empty(self, tmp_path):
        """load_escalation_log wraps entire read in try/except — any bad line → empty."""
        from bluei.engine.escalation import load_escalation_log

        esc_file = tmp_path / "esc.jsonl"
        esc_file.write_text('not-json\n{"timestamp":"t","findings":[],"count":0}\n')
        cfg = EscalationConfig(escalation_log_path=esc_file)
        records = load_escalation_log(cfg)
        assert len(records) == 0


class TestAppendEscalation:
    """_append_escalation writes one record and handles errors."""

    def test_appends_to_existing(self, tmp_path):
        from bluei.engine.escalation import _append_escalation

        esc_file = tmp_path / "esc.jsonl"
        esc_file.write_text('{"old":true}\n')
        _append_escalation(esc_file, {"new": True})
        lines = esc_file.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["new"] is True

    def test_oserror_silent(self, tmp_path):
        from bluei.engine.escalation import _append_escalation

        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        esc_file = blocker / "esc.jsonl"
        _append_escalation(esc_file, {"type": "test"})  # should not raise


class TestHandleMaxDuplicateDryRunLog:
    """Dry-run path writes log message about would-close."""

    @patch("bluei.engine.state.mark_finding_activity")
    def test_dry_run_logs_would_close_message(self, _mark, tmp_path):
        log = tmp_path / "log.txt"
        log.write_text("")
        esc = {
            "finding_id": "f1",
            "pr_numbers": [10, 11, 12],
            "open_pr_count": 3,
            "threshold": 3,
        }
        issues = {"issues": [{"finding_id": "f1", "status": "open"}]}
        r = handle_max_duplicate_escalation(
            [esc], "o/r", tmp_path, {}, issues, log, dry_run=True
        )
        assert r["closed_prs"] == []
        assert r["paused_findings"] == ["f1"]
        log_text = log.read_text()
        assert "max-dup-dry-run" in log_text
        assert "would close 2 PRs" in log_text
