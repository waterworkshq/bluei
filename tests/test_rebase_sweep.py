"""Tests for bluei.engine.rebase_sweep — post-merge rebase sweep logic."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, call

import pytest

from bluei.engine.rebase_sweep import (
    CONFLICT_MARKER_HEADER,
    _compute_fork_point,
    _fetch_sibling_prs,
    _force_push_rebased,
    _has_conflict_marker,
    _rebase_sibling,
    _update_pr_body_with_conflict,
    append_log,
    sweep_rebase,
)


class TestHasConflictMarker:
    def test_none_body(self) -> None:
        assert _has_conflict_marker(None) is False

    def test_empty_string(self) -> None:
        assert _has_conflict_marker("") is False

    def test_plain_text_no_marker(self) -> None:
        assert _has_conflict_marker("Just a normal PR body") is False

    def test_contains_marker(self) -> None:
        body = f"Some desc\n\n{CONFLICT_MARKER_HEADER}\nFiles: foo.py"
        assert _has_conflict_marker(body) is True

    def test_marker_only(self) -> None:
        assert _has_conflict_marker(CONFLICT_MARKER_HEADER) is True

    def test_marker_embedded_in_longer_body(self) -> None:
        body = "A" * 500 + CONFLICT_MARKER_HEADER + "B" * 500
        assert _has_conflict_marker(body) is True


class TestAppendLog:
    def test_creates_file_and_appends(self, tmp_path: Path) -> None:
        log = tmp_path / "run.log"
        append_log(log, "hello world")
        content = log.read_text()
        assert "hello world" in content
        assert content.endswith("\n")

    def test_appends_multiple_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "run.log"
        append_log(log, "line1")
        append_log(log, "line2")
        lines = log.read_text().strip().splitlines()
        assert len(lines) == 2
        assert "line1" in lines[0]
        assert "line2" in lines[1]

    def test_includes_iso_timestamp(self, tmp_path: Path) -> None:
        # Canonical format is ``[ISO] message`` — bracketed timestamp prefix.
        log = tmp_path / "run.log"
        append_log(log, "ts-test")
        line = log.read_text().strip()
        assert line.startswith("[")
        assert "]" in line
        # ISO marker ``T`` between date and time should be present.
        assert "T" in line

    def test_propagates_on_uncreatable_path(self) -> None:
        # After migrating to canonical bluei.engine.state.append_log, write
        # failures are no longer swallowed. /nonexistent cannot be created by
        # a non-root user, so this must raise.
        with pytest.raises((OSError, PermissionError)):
            append_log(Path("/nonexistent/dir/file.log"), "should raise")


class TestFetchSiblingPrs:
    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_parses_valid_json(self, mock_rc) -> None:
        prs = [
            {"number": 10, "headRefName": "fix-a", "createdAt": "2025-01-01T00:00:00Z"},
            {"number": 20, "headRefName": "fix-b", "createdAt": "2025-01-02T00:00:00Z"},
        ]
        mock_rc.return_value = (0, json.dumps(prs))
        result = _fetch_sibling_prs("owner/repo", "main", 99, cwd=Path("/tmp"))
        assert len(result) == 2

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_excludes_merged_pr(self, mock_rc) -> None:
        prs = [
            {"number": 10, "headRefName": "fix-a", "createdAt": "2025-01-01T00:00:00Z"},
            {"number": 42, "headRefName": "fix-b", "createdAt": "2025-01-02T00:00:00Z"},
        ]
        mock_rc.return_value = (0, json.dumps(prs))
        result = _fetch_sibling_prs("owner/repo", "main", 42, cwd=Path("/tmp"))
        assert len(result) == 1
        assert result[0]["number"] == 10

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_sorted_oldest_first(self, mock_rc) -> None:
        prs = [
            {"number": 1, "headRefName": "b", "createdAt": "2025-03-01T00:00:00Z"},
            {"number": 2, "headRefName": "a", "createdAt": "2025-01-01T00:00:00Z"},
            {"number": 3, "headRefName": "c", "createdAt": "2025-02-01T00:00:00Z"},
        ]
        mock_rc.return_value = (0, json.dumps(prs))
        result = _fetch_sibling_prs("owner/repo", "main", 99, cwd=Path("/tmp"))
        nums = [p["number"] for p in result]
        assert nums == [2, 3, 1]

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_returns_empty_on_nonzero_rc(self, mock_rc) -> None:
        mock_rc.return_value = (1, "error")
        result = _fetch_sibling_prs("owner/repo", "main", 1, cwd=Path("/tmp"))
        assert result == []

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_returns_empty_on_blank_output(self, mock_rc) -> None:
        mock_rc.return_value = (0, "   ")
        result = _fetch_sibling_prs("owner/repo", "main", 1, cwd=Path("/tmp"))
        assert result == []

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_returns_empty_on_bad_json(self, mock_rc) -> None:
        mock_rc.return_value = (0, "not json at all")
        result = _fetch_sibling_prs("owner/repo", "main", 1, cwd=Path("/tmp"))
        assert result == []


class TestComputeForkPoint:
    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_success_returns_sha_pair(self, mock_rc) -> None:
        mock_rc.side_effect = [
            (0, ""),  # git fetch origin base
            (0, "abc123\n"),  # merge-base
            (0, "def456\n"),  # rev-parse
        ]
        fp, ob = _compute_fork_point(Path("/tmp"), "main", "fix-branch")
        assert fp == "abc123"
        assert ob == "def456"

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_fetch_failure_returns_empty(self, mock_rc) -> None:
        mock_rc.return_value = (1, "fetch error")
        fp, ob = _compute_fork_point(Path("/tmp"), "main", "fix-branch")
        assert fp == ""
        assert ob == ""

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_merge_base_failure_returns_empty_fork(self, mock_rc) -> None:
        mock_rc.side_effect = [
            (0, ""),  # fetch ok
            (1, ""),  # merge-base fails
            (0, "def456\n"),  # rev-parse ok
        ]
        fp, ob = _compute_fork_point(Path("/tmp"), "main", "fix-branch")
        assert fp == ""
        assert ob == "def456"

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_rev_parse_failure_returns_empty_old_base(self, mock_rc) -> None:
        mock_rc.side_effect = [
            (0, ""),
            (0, "abc123\n"),
            (1, ""),
        ]
        fp, ob = _compute_fork_point(Path("/tmp"), "main", "fix-branch")
        assert fp == "abc123"
        assert ob == ""


class TestRebaseSibling:
    @patch("bluei.engine.rebase_sweep._compute_fork_point")
    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_clean_rebase_returns_sha(self, mock_rc, mock_fp) -> None:
        mock_fp.return_value = ("fork123", "base456")
        mock_rc.side_effect = [
            (0, ""),  # fetch head
            (0, ""),  # branch -D cleanup
            (0, ""),  # checkout -b local
            (0, ""),  # rebase succeeds
            (0, "newsha999\n"),  # rev-parse HEAD
            (0, ""),  # checkout -f origin/base
        ]
        ok, sha, files = _rebase_sibling(
            Path("/tmp"), "fix-branch", "main", "rebase-sweep-10"
        )
        assert ok is True
        assert sha == "newsha999"
        assert files == []

    @patch("bluei.engine.rebase_sweep._compute_fork_point")
    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_fetch_failure(self, mock_rc, mock_fp) -> None:
        mock_rc.return_value = (1, "fetch failed")
        ok, msg, files = _rebase_sibling(
            Path("/tmp"), "fix-branch", "main", "rebase-sweep-10"
        )
        assert ok is False
        assert msg == "fetch-failed"
        assert files == []

    @patch("bluei.engine.rebase_sweep._compute_fork_point")
    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_merge_base_failure(self, mock_rc, mock_fp) -> None:
        mock_fp.side_effect = [("", ""), ("", "")]
        mock_rc.side_effect = [
            (0, ""),  # fetch head
            (0, ""),  # git fetch origin base (retry)
        ]
        ok, msg, files = _rebase_sibling(
            Path("/tmp"), "fix-branch", "main", "rebase-sweep-10"
        )
        assert ok is False
        assert msg == "merge-base-failed"

    @patch("bluei.engine.rebase_sweep._compute_fork_point")
    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_local_branch_create_failure(self, mock_rc, mock_fp) -> None:
        mock_fp.return_value = ("fork123", "base456")
        mock_rc.side_effect = [
            (0, ""),  # fetch head
            (0, ""),  # branch -D cleanup
            (1, "checkout failed"),  # checkout -b local
        ]
        ok, msg, files = _rebase_sibling(
            Path("/tmp"), "fix-branch", "main", "rebase-sweep-10"
        )
        assert ok is False
        assert msg == "local-branch-create-failed"

    @patch("bluei.engine.rebase_sweep._compute_fork_point")
    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_rebase_conflict_with_files(self, mock_rc, mock_fp) -> None:
        mock_fp.return_value = ("fork123", "base456")
        mock_rc.side_effect = [
            (0, ""),  # fetch head
            (0, ""),  # branch -D cleanup
            (0, ""),  # checkout -b local
            (1, "CONFLICT (content)"),  # rebase fails
            (0, "file_a.py\nfile_b.rs\n"),  # diff --name-only
            (0, ""),  # rebase --abort
            (0, ""),  # checkout -f
            (0, ""),  # branch -D local
        ]
        ok, msg, files = _rebase_sibling(
            Path("/tmp"), "fix-branch", "main", "rebase-sweep-10"
        )
        assert ok is False
        assert "CONFLICT" in msg
        assert files == ["file_a.py", "file_b.rs"]

    @patch("bluei.engine.rebase_sweep._compute_fork_point")
    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_rebase_conflict_empty_output_gives_default_msg(
        self, mock_rc, mock_fp
    ) -> None:
        mock_fp.return_value = ("fork123", "base456")
        mock_rc.side_effect = [
            (0, ""),
            (0, ""),
            (0, ""),
            (1, "   \n"),  # rebase fails, whitespace output
            (1, ""),  # diff fails too
            (0, ""),  # rebase --abort
            (0, ""),  # checkout -f
            (0, ""),  # branch -D local
        ]
        ok, msg, files = _rebase_sibling(
            Path("/tmp"), "fix-branch", "main", "rebase-sweep-10"
        )
        assert ok is False
        assert msg == "rebase-conflict"
        assert files == []


class TestForcePushRebased:
    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_push_success_returns_true(self, mock_rc) -> None:
        mock_rc.return_value = (0, "pushed")
        log = Path("/tmp/fake.log")
        result = _force_push_rebased(
            "local-b", "remote-b", "abc123def456", Path("/tmp"), log, dry_run=False
        )
        assert result is True

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_push_failure_returns_false(self, mock_rc) -> None:
        mock_rc.return_value = (1, "push rejected")
        log = Path("/tmp/fake.log")
        result = _force_push_rebased(
            "local-b", "remote-b", "abc123", Path("/tmp"), log, dry_run=False
        )
        assert result is False

    def test_dry_run_returns_true_without_push(self, tmp_path: Path) -> None:
        log = tmp_path / "run.log"
        result = _force_push_rebased(
            "local-b",
            "remote-b",
            "abc123",
            tmp_path,
            log,
            dry_run=True,
        )
        assert result is True
        assert not log.exists()

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_pre_existing_dirty_log_note(self, mock_rc, tmp_path: Path) -> None:
        mock_rc.return_value = (0, "ok")
        log = tmp_path / "run.log"
        _force_push_rebased(
            "local-b",
            "remote-b",
            "abc123def456",
            tmp_path,
            log,
            dry_run=False,
            pre_existing_dirty=True,
        )
        content = log.read_text()
        assert "pre-existing-dirty" in content

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_clean_log_note(self, mock_rc, tmp_path: Path) -> None:
        mock_rc.return_value = (0, "ok")
        log = tmp_path / "run.log"
        _force_push_rebased(
            "local-b", "remote-b", "abc123def456", tmp_path, log, dry_run=False
        )
        content = log.read_text()
        assert "note=clean" in content

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_push_failure_writes_log(self, mock_rc, tmp_path: Path) -> None:
        mock_rc.return_value = (1, "remote rejected")
        log = tmp_path / "run.log"
        _force_push_rebased(
            "local-b", "remote-b", "abc123", tmp_path, log, dry_run=False
        )
        content = log.read_text()
        assert "rebase-push-fail" in content


class TestUpdatePrBodyWithConflict:
    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_appends_marker_to_body(self, mock_rc) -> None:
        pr_data = {"body": "Original description"}
        mock_rc.side_effect = [
            (0, json.dumps(pr_data)),  # gh pr view
            (0, ""),  # gh pr update
        ]
        _update_pr_body_with_conflict(
            "owner/repo", 42, ["foo.py", "bar.rs"], cwd=Path("/tmp"), dry_run=False
        )
        update_call = mock_rc.call_args_list[1]
        body_arg = update_call[0][0]
        assert CONFLICT_MARKER_HEADER in " ".join(body_arg)
        assert "foo.py" in " ".join(body_arg)

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_skips_if_already_marked(self, mock_rc) -> None:
        pr_data = {"body": f"desc\n{CONFLICT_MARKER_HEADER}\nmore"}
        mock_rc.return_value = (0, json.dumps(pr_data))
        _update_pr_body_with_conflict(
            "owner/repo", 42, ["foo.py"], cwd=Path("/tmp"), dry_run=False
        )
        assert mock_rc.call_count == 1  # only view, no update

    def test_dry_run_does_nothing(self) -> None:
        with patch("bluei.engine.rebase_sweep.run_capture") as mock_rc:
            _update_pr_body_with_conflict(
                "owner/repo",
                42,
                ["foo.py"],
                cwd=Path("/tmp"),
                dry_run=True,
            )
            mock_rc.assert_not_called()

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_view_failure_skips_update(self, mock_rc) -> None:
        mock_rc.return_value = (1, "not found")
        _update_pr_body_with_conflict(
            "owner/repo", 42, ["foo.py"], cwd=Path("/tmp"), dry_run=False
        )
        assert mock_rc.call_count == 1

    @patch("bluei.engine.rebase_sweep.run_capture")
    def test_bad_json_skips_update(self, mock_rc) -> None:
        mock_rc.return_value = (0, "not json")
        _update_pr_body_with_conflict(
            "owner/repo", 42, ["foo.py"], cwd=Path("/tmp"), dry_run=False
        )
        assert mock_rc.call_count == 1


class TestSweepRebase:
    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_full_sweep_clean(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = [
            {
                "number": 10,
                "headRefName": "fix-a",
                "createdAt": "2025-01-01T00:00:00Z",
                "body": "",
            },
        ]
        mock_rebase.return_value = (True, "abcdef1234567890", [])
        mock_push.return_value = True
        log = tmp_path / "run.log"
        result = sweep_rebase(tmp_path, "owner/repo", 1, "main", log)
        assert len(result["rebased"]) == 1
        assert result["rebased"][0]["pr_number"] == 10
        assert result["conflicted"] == []
        assert result["skipped"] == []

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_full_sweep_conflict(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = [
            {
                "number": 10,
                "headRefName": "fix-a",
                "createdAt": "2025-01-01T00:00:00Z",
                "body": "",
            },
        ]
        mock_rebase.return_value = (False, "rebase-conflict", ["main.py", "util.rs"])
        mock_push.return_value = False
        log = tmp_path / "run.log"
        with patch("bluei.engine.rebase_sweep._update_pr_body_with_conflict"):
            result = sweep_rebase(tmp_path, "owner/repo", 1, "main", log)
        assert len(result["conflicted"]) == 1
        assert result["conflicted"][0]["files"] == ["main.py", "util.rs"]
        assert result["rebased"] == []

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_empty_siblings_returns_empty(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = []
        log = tmp_path / "run.log"
        result = sweep_rebase(tmp_path, "owner/repo", 1, "main", log)
        assert result["rebased"] == []
        assert result["conflicted"] == []
        assert result["skipped"] == []
        content = log.read_text()
        assert "no siblings found" in content

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_skips_already_conflict_marked(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = [
            {
                "number": 10,
                "headRefName": "fix-a",
                "createdAt": "2025-01-01T00:00:00Z",
                "body": f"desc\n{CONFLICT_MARKER_HEADER}\nmore",
            },
        ]
        log = tmp_path / "run.log"
        result = sweep_rebase(tmp_path, "owner/repo", 1, "main", log)
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["reason"] == "already-conflict-marked"
        mock_rebase.assert_not_called()

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_skips_no_head_branch(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = [
            {
                "number": 10,
                "headRefName": "",
                "createdAt": "2025-01-01T00:00:00Z",
                "body": "",
            },
        ]
        log = tmp_path / "run.log"
        result = sweep_rebase(tmp_path, "owner/repo", 1, "main", log)
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["reason"] == "no-head-branch"

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_dry_run_skips_push(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = [
            {
                "number": 10,
                "headRefName": "fix-a",
                "createdAt": "2025-01-01T00:00:00Z",
                "body": "",
            },
        ]
        mock_rebase.return_value = (True, "abcdef1234567890", [])
        log = tmp_path / "run.log"
        result = sweep_rebase(tmp_path, "owner/repo", 1, "main", log, dry_run=True)
        assert len(result["rebased"]) == 1
        mock_push.assert_called_once_with(
            local_branch="rebase-sweep-10",
            remote_branch="fix-a",
            new_head_sha="abcdef1234567890",
            repo_path=tmp_path,
            log_file=log,
            dry_run=True,
            pre_existing_dirty=False,
        )

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_max_prs_limits_siblings(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        prs = [
            {
                "number": i,
                "headRefName": f"fix-{i}",
                "createdAt": f"2025-01-{i:02d}T00:00:00Z",
                "body": "",
            }
            for i in range(1, 11)
        ]
        mock_fetch.return_value = prs
        mock_rebase.return_value = (True, "abcdef1234567890", [])
        mock_push.return_value = True
        log = tmp_path / "run.log"
        result = sweep_rebase(tmp_path, "owner/repo", 99, "main", log, max_prs=3)
        assert len(result["rebased"]) == 3
        assert mock_rebase.call_count == 3

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_force_push_failure_goes_to_skipped(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = [
            {
                "number": 10,
                "headRefName": "fix-a",
                "createdAt": "2025-01-01T00:00:00Z",
                "body": "",
            },
        ]
        mock_rebase.return_value = (True, "abcdef1234567890", [])
        mock_push.return_value = False
        log = tmp_path / "run.log"
        result = sweep_rebase(tmp_path, "owner/repo", 1, "main", log)
        assert result["rebased"] == []
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["reason"] == "force-push-failed"

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_empty_sha_after_rebase_goes_to_skipped(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = [
            {
                "number": 10,
                "headRefName": "fix-a",
                "createdAt": "2025-01-01T00:00:00Z",
                "body": "",
            },
        ]
        mock_rebase.return_value = (True, "", [])
        log = tmp_path / "run.log"
        result = sweep_rebase(tmp_path, "owner/repo", 1, "main", log)
        assert result["rebased"] == []
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["reason"] == "empty-HEAD-after-rebase"

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_rebase_error_no_conflict_files_goes_to_skipped(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = [
            {
                "number": 10,
                "headRefName": "fix-a",
                "createdAt": "2025-01-01T00:00:00Z",
                "body": "",
            },
        ]
        mock_rebase.return_value = (False, "fetch-failed", [])
        log = tmp_path / "run.log"
        result = sweep_rebase(tmp_path, "owner/repo", 1, "main", log)
        assert result["rebased"] == []
        assert result["conflicted"] == []
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["reason"] == "fetch-failed"

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_writes_rebase_stats_file(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = [
            {
                "number": 10,
                "headRefName": "fix-a",
                "createdAt": "2025-01-01T00:00:00Z",
                "body": "",
            },
        ]
        mock_rebase.return_value = (True, "abcdef1234567890", [])
        mock_push.return_value = True
        log = tmp_path / "run.log"
        stats_file = tmp_path / "rebase_stats.jsonl"
        result = sweep_rebase(
            tmp_path,
            "owner/repo",
            1,
            "main",
            log,
            rebase_stats_file=stats_file,
        )
        assert stats_file.exists()
        entries = [json.loads(l) for l in stats_file.read_text().strip().splitlines()]
        assert len(entries) == 1
        assert entries[0]["rebases_attempted"] == 1
        assert entries[0]["rebases_succeeded"] == 1

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_writes_rebase_stats_on_empty_siblings(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = []
        log = tmp_path / "run.log"
        stats_file = tmp_path / "rebase_stats.jsonl"
        sweep_rebase(
            tmp_path, "owner/repo", 1, "main", log, rebase_stats_file=stats_file
        )
        entries = [json.loads(l) for l in stats_file.read_text().strip().splitlines()]
        assert len(entries) == 1
        assert entries[0]["rebases_attempted"] == 0

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_multiple_prs_mixed_outcomes(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = [
            {
                "number": 10,
                "headRefName": "fix-a",
                "createdAt": "2025-01-01T00:00:00Z",
                "body": "",
            },
            {
                "number": 20,
                "headRefName": "fix-b",
                "createdAt": "2025-01-02T00:00:00Z",
                "body": "",
            },
            {
                "number": 30,
                "headRefName": "fix-c",
                "createdAt": "2025-01-03T00:00:00Z",
                "body": "",
            },
        ]
        mock_rebase.side_effect = [
            (True, "sha_aaaa1111bbbb", []),
            (False, "conflict", ["x.py"]),
            (True, "sha_cccc3333dddd", []),
        ]
        mock_push.side_effect = [True, True]
        log = tmp_path / "run.log"
        with patch("bluei.engine.rebase_sweep._update_pr_body_with_conflict"):
            result = sweep_rebase(tmp_path, "owner/repo", 1, "main", log)
        assert len(result["rebased"]) == 2
        assert len(result["conflicted"]) == 1
        assert result["conflicted"][0]["pr_number"] == 20

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_log_file_records_sweep_events(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = [
            {
                "number": 10,
                "headRefName": "fix-a",
                "createdAt": "2025-01-01T00:00:00Z",
                "body": "",
            },
        ]
        mock_rebase.return_value = (True, "abcdef1234567890", [])
        mock_push.return_value = True
        log = tmp_path / "run.log"
        sweep_rebase(tmp_path, "owner/repo", 1, "main", log)
        content = log.read_text()
        assert "rebase-sweep-start" in content
        assert "rebase-attempt" in content
        assert "rebase-sweep-end" in content

    @patch("bluei.engine.rebase_sweep.time.sleep")
    @patch("bluei.engine.rebase_sweep._rebase_sibling")
    @patch("bluei.engine.rebase_sweep._force_push_rebased")
    @patch("bluei.engine.rebase_sweep._fetch_sibling_prs")
    def test_no_stats_file_when_none(
        self,
        mock_fetch,
        mock_push,
        mock_rebase,
        mock_sleep,
        tmp_path: Path,
    ) -> None:
        mock_fetch.return_value = []
        log = tmp_path / "run.log"
        result = sweep_rebase(
            tmp_path, "owner/repo", 1, "main", log, rebase_stats_file=None
        )
        stats_candidates = list(tmp_path.glob("*.jsonl"))
        assert stats_candidates == []
