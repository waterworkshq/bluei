#!/usr/bin/env python3
"""Integration tests for the live-gh path in process_batch().

All existing tests use dry_run=True, skipping lines 1277-1446 that call
get_origin_url, parse_github_repo, find_batch_pr_by_rule, create_batch_pr,
link_issues_to_batch_pr, and save_batch_record.

These tests mock at the module boundary (bluei.engine.gh.*, bluei.engine.utils.run_capture)
and exercise the live path with dry_run=False.
"""

from __future__ import annotations

import subprocess
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, call, patch

import pytest

from bluei.engine.models import (
    BatchGroup,
    BatchRule,
    BatchStatus,
    Finding,
    FixResult,
)
from bluei.engine.batch_pr import process_batch


def make_issue(finding_id: str = "f001", **overrides) -> Dict[str, Any]:
    defaults = {
        "finding_id": finding_id,
        "issue_id": f"ISS-{finding_id[:4]}",
        "status": "open",
        "github": {"issue_number": 1},
    }
    defaults.update(overrides)
    return defaults


def make_batch_group(n_findings=3, rule="ruff-c408") -> BatchGroup:
    findings = []
    issues = []
    for i in range(n_findings):
        fid = f"f{i:03d}"
        findings.append(
            Finding(
                finding_id=fid,
                repo="test-repo",
                path=f"zerver/lib/file_{i}.py",
                line=10 + i,
                rule=rule,
                snippet="dict(a=1)",
                confidence=0.72,
                quick_win=True,
                safe_to_autofix=True,
            )
        )
        issues.append(make_issue(finding_id=fid, github={"issue_number": i + 10}))
    return BatchGroup(
        batch_id=f"batch-test-{rule}",
        rule_pattern=rule,
        group_by="rule",
        findings=findings,
        issues=issues,
    )


@dataclass
class LiveArgs:
    worktree_root: str = "/tmp/test-worktrees"
    dry_run: bool = False
    live_github_actions: bool = False
    fix_engine: str = "deterministic"
    claude_cmd_template: str = 'echo "mock"'
    max_files_changed: int = 5
    max_loc_diff: int = 200
    batch_state_file: str = "/tmp/test-batches.jsonl"
    batch_pr_enabled: bool = True
    batch_dedup_hours: int = 24
    batch_pr_split_on_failure: bool = True
    max_split_depth: int = 3


def _live_patches():
    return [
        patch(
            "bluei.engine.gh.get_origin_url",
            return_value="https://github.com/acme/widget.git",
        ),
        patch("bluei.engine.gh.parse_github_repo", return_value=("acme", "widget")),
        patch("bluei.engine.gh.find_batch_pr_by_rule", return_value=None),
        patch("bluei.engine.batch_pr._create_worktree", return_value=True),
        patch("bluei.engine.worktree.hydrate_worktree"),
        patch("bluei.engine.batch_pr.apply_batch_fixes", return_value=(3, 0)),
        patch("bluei.engine.git_ops.git_commit_all", return_value="committed"),
        patch("bluei.engine.git_ops.git_push_branch", return_value=True),
        patch("bluei.engine.utils.run_no_capture"),
        patch("bluei.engine.worktree.run_no_capture"),
        patch("bluei.engine.state._append_text"),
    ]


class _PatchStack(ExitStack):
    def __init__(self, patches_list: List):
        super().__init__()
        self._patches = patches_list
        self.mocks: List = []

    def __enter__(self):
        super().__enter__()
        for p in self._patches:
            self.mocks.append(self.enter_context(p))
        return self.mocks


class TestLiveGHHappyPath:
    @patch("bluei.engine.state.save_batch_record")
    @patch("bluei.engine.batch_pr.link_issues_to_batch_pr")
    @patch("bluei.engine.batch_pr.create_batch_pr")
    def test_full_happy_path(self, mock_create_pr, mock_link, mock_save):
        mock_create_pr.return_value = {
            "number": 99,
            "url": "https://github.com/acme/widget/pull/99",
        }

        with _PatchStack(_live_patches()) as mocks:
            mock_origin, mock_parse, mock_find_dup = mocks[0], mocks[1], mocks[2]
            batch = make_batch_group(n_findings=3)
            args = LiveArgs()
            log_file = Path("/tmp/test.log")

            success, detail = process_batch(batch, Path("/tmp/repo"), args, log_file)

        assert success is True
        assert "pr-created-#99" == detail
        assert batch.status == BatchStatus.PR_CREATED.value
        assert batch.pr_number == 99
        assert batch.pr_url == "https://github.com/acme/widget/pull/99"

        mock_origin.assert_called_once_with(Path("/tmp/repo"))
        mock_parse.assert_called_once_with("https://github.com/acme/widget.git")
        mock_find_dup.assert_called_once()
        mock_create_pr.assert_called_once()
        assert mock_create_pr.call_args[0][0] is batch
        assert mock_create_pr.call_args[0][1] == "acme/widget"

        mock_link.assert_called_once_with(
            batch=batch,
            pr_number=99,
            pr_url="https://github.com/acme/widget/pull/99",
            repo_slug="acme/widget",
            repo_path=Path("/tmp/repo"),
            log_file=log_file,
        )

        mock_save.assert_called_once()
        saved_record = mock_save.call_args[0][1]
        assert saved_record["batch_id"] == batch.batch_id
        assert saved_record["status"] == BatchStatus.PR_CREATED.value

    @patch("bluei.engine.state.save_batch_record")
    @patch("bluei.engine.batch_pr.link_issues_to_batch_pr")
    @patch("bluei.engine.batch_pr.create_batch_pr")
    def test_worktree_cleanup_in_finally(self, mock_create_pr, mock_link, mock_save):
        mock_create_pr.return_value = {
            "number": 42,
            "url": "https://github.com/acme/widget/pull/42",
        }

        with _PatchStack(_live_patches()) as mocks:
            mock_run_no_capture = mocks[9]  # worktree module's run_no_capture
            mock_create_wt = mocks[3]
            batch = make_batch_group(n_findings=2)
            args = LiveArgs()
            log_file = Path("/tmp/test.log")

            success, detail = process_batch(batch, Path("/tmp/repo"), args, log_file)

        assert success is True

        all_calls = mock_run_no_capture.call_args_list
        branch_delete_calls = [
            c for c in all_calls if "branch" in c[0][0] and "-D" in c[0][0]
        ]
        assert len(branch_delete_calls) >= 1
        assert batch.branch in branch_delete_calls[0][0][0]

    @patch("bluei.engine.state.save_batch_record")
    @patch("bluei.engine.batch_pr.link_issues_to_batch_pr")
    @patch("bluei.engine.batch_pr.create_batch_pr")
    def test_repo_slug_derived_correctly(self, mock_create_pr, mock_link, mock_save):
        mock_create_pr.return_value = {
            "number": 7,
            "url": "https://github.com/acme/widget/pull/7",
        }

        with _PatchStack(_live_patches()) as mocks:
            mock_create_pr.assert_not_called()
            batch = make_batch_group(n_findings=2)
            args = LiveArgs()
            log_file = Path("/tmp/test.log")

            process_batch(batch, Path("/tmp/repo"), args, log_file)

        assert mock_create_pr.call_args[0][1] == "acme/widget"


class TestLiveGHDedupDetection:
    def test_duplicate_pr_skips_batch(self):
        dup_pr = {
            "number": 55,
            "title": "fix: resolve 3 ruff-c408 specks",
            "url": "https://github.com/acme/widget/pull/55",
            "headRefName": "qa/batch-c408-20240101000000-abc123",
        }

        with _PatchStack(_live_patches()) as mocks:
            mock_find_dup = mocks[2]
            mock_find_dup.return_value = dup_pr
            mock_create_wt = mocks[3]
            batch = make_batch_group(n_findings=3)
            args = LiveArgs()
            log_file = Path("/tmp/test.log")

            success, detail = process_batch(batch, Path("/tmp/repo"), args, log_file)

        assert success is False
        assert "duplicate-existing-pr-#55" == detail
        assert batch.status == BatchStatus.SKIPPED.value
        mock_create_wt.assert_not_called()

    def test_no_duplicate_proceeds_normally(self):
        with _PatchStack(_live_patches()) as mocks:
            mock_find_dup = mocks[2]
            mock_find_dup.return_value = None
            mock_create_wt = mocks[3]
            batch = make_batch_group(n_findings=2)
            args = LiveArgs()
            log_file = Path("/tmp/test.log")

            with (
                patch(
                    "bluei.engine.batch_pr.create_batch_pr",
                    return_value={
                        "number": 1,
                        "url": "https://github.com/acme/widget/pull/1",
                    },
                ),
                patch("bluei.engine.batch_pr.link_issues_to_batch_pr"),
                patch("bluei.engine.state.save_batch_record"),
            ):
                success, detail = process_batch(
                    batch, Path("/tmp/repo"), args, log_file
                )

        assert success is True
        assert batch.status == BatchStatus.PR_CREATED.value


class TestLiveGHPRCreationFailure:
    @patch("bluei.engine.batch_pr.link_issues_to_batch_pr")
    @patch(
        "bluei.engine.batch_pr.create_batch_pr",
        side_effect=RuntimeError("gh pr create failed: server error"),
    )
    def test_runtime_error_sets_failed_status(self, mock_create_pr, mock_link):
        with _PatchStack(_live_patches()) as mocks:
            batch = make_batch_group(n_findings=2)
            args = LiveArgs()
            log_file = Path("/tmp/test.log")

            success, detail = process_batch(batch, Path("/tmp/repo"), args, log_file)

        assert success is False
        assert detail == "pr-creation-failed"
        assert batch.status == BatchStatus.FAILED.value
        mock_link.assert_not_called()


class TestLiveGHNoRepoSlug:
    @patch("bluei.engine.batch_pr._create_worktree")
    @patch("bluei.engine.state._append_text")
    def test_empty_origin_url_graceful_failure(self, mock_append, mock_wt):
        with patch("bluei.engine.gh.get_origin_url", return_value=""):
            batch = make_batch_group(n_findings=2)
            args = LiveArgs()
            log_file = Path("/tmp/test.log")

            success, detail = process_batch(batch, Path("/tmp/repo"), args, log_file)

        assert success is False
        assert detail == "no-repo-slug"
        assert batch.status == BatchStatus.FAILED.value
        mock_wt.assert_not_called()

    @patch("bluei.engine.batch_pr._create_worktree")
    @patch("bluei.engine.state._append_text")
    def test_non_github_origin_graceful_failure(self, mock_append, mock_wt):
        with patch(
            "bluei.engine.gh.get_origin_url",
            return_value="https://gitlab.com/acme/widget.git",
        ):
            batch = make_batch_group(n_findings=2)
            args = LiveArgs()
            log_file = Path("/tmp/test.log")

            success, detail = process_batch(batch, Path("/tmp/repo"), args, log_file)

        assert success is False
        assert detail == "no-repo-slug"
        assert batch.status == BatchStatus.FAILED.value

    @patch("bluei.engine.batch_pr._create_worktree")
    @patch("bluei.engine.state._append_text")
    @patch("bluei.engine.gh.parse_github_repo", return_value=("", ""))
    def test_parse_failure_graceful_failure(self, mock_parse, mock_append, mock_wt):
        with patch(
            "bluei.engine.gh.get_origin_url", return_value="https://github.com/invalid"
        ):
            batch = make_batch_group(n_findings=2)
            args = LiveArgs()
            log_file = Path("/tmp/test.log")

            success, detail = process_batch(batch, Path("/tmp/repo"), args, log_file)

        assert success is False
        assert detail == "no-repo-slug"
        assert batch.status == BatchStatus.FAILED.value


class TestLiveGHIssueLinkingPartialFailure:
    @patch("bluei.engine.state.save_batch_record")
    @patch("bluei.engine.batch_pr.create_batch_pr")
    def test_partial_comment_failure_still_succeeds(self, mock_create_pr, mock_save):
        mock_create_pr.return_value = {
            "number": 88,
            "url": "https://github.com/acme/widget/pull/88",
        }

        comment_call_count = 0

        def side_effect_comment(repo_slug, issue_number, body, cwd):
            nonlocal comment_call_count
            comment_call_count += 1
            if issue_number == 11:
                raise subprocess.CalledProcessError(1, "gh issue comment")
            return True

        with _PatchStack(_live_patches()) as mocks:
            batch = make_batch_group(n_findings=3)
            args = LiveArgs()
            log_file = Path("/tmp/test.log")

            with (
                patch(
                    "bluei.engine.gh.gh_issue_comment", side_effect=side_effect_comment
                ) as mock_comment,
                patch("bluei.engine.orchestrator.set_issue_status"),
            ):
                success, detail = process_batch(
                    batch, Path("/tmp/repo"), args, log_file
                )

        assert success is True
        assert batch.status == BatchStatus.PR_CREATED.value
        assert batch.pr_number == 88
        assert comment_call_count == 3

        for issue in batch.issues:
            assert issue["github"]["pr_number"] == 88
            assert issue["github"]["batch_id"] == batch.batch_id

    @patch("bluei.engine.state.save_batch_record")
    @patch("bluei.engine.batch_pr.create_batch_pr")
    def test_all_comment_failures_still_succeeds(self, mock_create_pr, mock_save):
        mock_create_pr.return_value = {
            "number": 77,
            "url": "https://github.com/acme/widget/pull/77",
        }

        with _PatchStack(_live_patches()) as mocks:
            batch = make_batch_group(n_findings=2)
            args = LiveArgs()
            log_file = Path("/tmp/test.log")

            with (
                patch(
                    "bluei.engine.gh.gh_issue_comment",
                    side_effect=subprocess.CalledProcessError(1, "gh issue comment"),
                ),
                patch("bluei.engine.orchestrator.set_issue_status"),
            ):
                success, detail = process_batch(
                    batch, Path("/tmp/repo"), args, log_file
                )

        assert success is True
        assert batch.status == BatchStatus.PR_CREATED.value

    @patch("bluei.engine.state.save_batch_record")
    @patch("bluei.engine.batch_pr.create_batch_pr")
    def test_no_issue_number_skips_comment(self, mock_create_pr, mock_save):
        mock_create_pr.return_value = {
            "number": 66,
            "url": "https://github.com/acme/widget/pull/66",
        }

        with _PatchStack(_live_patches()) as mocks:
            batch = make_batch_group(n_findings=2)
            batch.issues[0]["github"] = {}
            batch.issues[1]["github"] = {"issue_number": 12}
            args = LiveArgs()
            log_file = Path("/tmp/test.log")

            with (
                patch(
                    "bluei.engine.gh.gh_issue_comment", return_value=True
                ) as mock_comment,
                patch("bluei.engine.orchestrator.set_issue_status"),
            ):
                success, detail = process_batch(
                    batch, Path("/tmp/repo"), args, log_file
                )

        assert success is True
        assert mock_comment.call_count == 1
        assert mock_comment.call_args[0][1] == 12
