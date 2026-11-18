#!/usr/bin/env python3
"""Live-gh containment for process_batch() under the unqualified-refusal contract.

Current contract (ACCEPTANCE-BOUNDARY-3ks3txvh): batch repair is refused
BEFORE any GitHub interaction. These tests call the live lane with
dry_run=False, mock at the module boundary (bluei.engine.gh.*, the batch_pr
facade, batch record persistence) and prove the refusal fires first: origin
URL/slug/dedup are never consulted, no PR is created or linked, no issue is
commented on, and no batch record is saved.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

from bluei.engine.models import (
    BatchGroup,
    BatchStatus,
    Finding,
)
from bluei.engine.batch_pr import process_batch

UNQUALIFIED_DETAIL = (
    "batch repair unqualified: missing configured scope/validation/closure acceptance"
)


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


class TestLiveGHHappyPath:
    @patch("bluei.engine.state.save_batch_record")
    @patch("bluei.engine.batch_pr.link_issues_to_batch_pr")
    @patch("bluei.engine.batch_pr.create_batch_pr")
    def test_full_happy_path(self, mock_create_pr, mock_link, mock_save, tmp_path):
        # current contract: the live lane is refused before ANY GitHub
        # interaction — origin/slug/dedup are never consulted, no PR is
        # created or linked, and no batch record is saved even with a
        # batch_state_file configured.
        batch = make_batch_group(n_findings=3)
        args = LiveArgs(batch_state_file=str(tmp_path / "batches.jsonl"))
        log_file = tmp_path / "test.log"

        with (
            patch(
                "bluei.engine.gh.get_origin_url",
                return_value="https://github.com/acme/widget.git",
            ) as mock_origin,
            patch(
                "bluei.engine.gh.parse_github_repo", return_value=("acme", "widget")
            ) as mock_parse,
            patch("bluei.engine.gh.find_batch_pr_by_rule", return_value=None),
        ):
            success, detail = process_batch(batch, tmp_path / "repo", args, log_file)

        assert success is False
        assert detail == UNQUALIFIED_DETAIL
        assert batch.status == BatchStatus.OPEN.value
        assert batch.pr_number is None
        assert batch.pr_url is None
        mock_origin.assert_not_called()
        mock_parse.assert_not_called()
        mock_create_pr.assert_not_called()
        mock_link.assert_not_called()
        mock_save.assert_not_called()
        assert not Path(args.batch_state_file).exists()
        log = log_file.read_text()
        assert "batch-refused" in log
        assert "batch-success" not in log


class TestLiveGHDedupDetection:
    def test_duplicate_pr_skips_batch(self, tmp_path):
        # current contract: refusal outranks the duplicate-PR classification —
        # even with an open duplicate PR one consultation away, the batch is
        # refused as unqualified before the dedup check ever runs.
        dup_pr = {
            "number": 55,
            "title": "fix: resolve 3 ruff-c408 specks",
            "url": "https://github.com/acme/widget/pull/55",
            "headRefName": "qa/batch-c408-20240101000000-abc123",
        }
        batch = make_batch_group(n_findings=3)
        args = LiveArgs()
        log_file = tmp_path / "test.log"

        with patch(
            "bluei.engine.gh.find_batch_pr_by_rule", return_value=dup_pr
        ) as mock_find_dup:
            success, detail = process_batch(batch, tmp_path / "repo", args, log_file)

        assert success is False
        assert detail == UNQUALIFIED_DETAIL
        assert detail != "duplicate-existing-pr-#55"
        assert batch.status == BatchStatus.OPEN.value  # not SKIPPED
        mock_find_dup.assert_not_called()
        assert "batch-skip-duplicate" not in log_file.read_text()


class TestLiveGHPRCreationFailure:
    @patch("bluei.engine.batch_pr.link_issues_to_batch_pr")
    @patch(
        "bluei.engine.batch_pr.create_batch_pr",
        side_effect=RuntimeError("gh pr create failed: server error"),
    )
    def test_runtime_error_sets_failed_status(self, mock_create_pr, mock_link, tmp_path):
        # current contract: create_batch_pr is never reached, so its
        # RuntimeError can neither escape nor flip the batch to FAILED — the
        # refusal result stands.
        batch = make_batch_group(n_findings=2)
        args = LiveArgs()
        log_file = tmp_path / "test.log"

        success, detail = process_batch(batch, tmp_path / "repo", args, log_file)

        assert success is False
        assert detail == UNQUALIFIED_DETAIL
        assert batch.status == BatchStatus.OPEN.value  # not FAILED
        mock_create_pr.assert_not_called()
        mock_link.assert_not_called()


class TestLiveGHNoRepoSlug:
    def test_empty_origin_url_graceful_failure(self, tmp_path):
        # current contract: the refusal precedes repo-slug derivation — an
        # empty origin URL no longer produces "no-repo-slug" (nor a FAILED
        # batch); get_origin_url is never consulted.
        batch = make_batch_group(n_findings=2)
        args = LiveArgs()
        log_file = tmp_path / "test.log"

        with patch("bluei.engine.gh.get_origin_url", return_value="") as mock_origin:
            success, detail = process_batch(batch, tmp_path / "repo", args, log_file)

        assert success is False
        assert detail == UNQUALIFIED_DETAIL
        assert detail != "no-repo-slug"
        assert batch.status == BatchStatus.OPEN.value  # not FAILED
        mock_origin.assert_not_called()


class TestLiveGHIssueLinkingPartialFailure:
    @patch("bluei.engine.state.save_batch_record")
    @patch("bluei.engine.batch_pr.create_batch_pr")
    def test_partial_comment_failure_still_succeeds(self, mock_create_pr, mock_save, tmp_path):
        # current contract: issue linking is never reached — no issue is
        # commented on, and no issue dict is stamped with PR metadata.
        batch = make_batch_group(n_findings=3)
        args = LiveArgs()
        log_file = tmp_path / "test.log"

        with patch("bluei.engine.gh.gh_issue_comment") as mock_comment:
            success, detail = process_batch(batch, tmp_path / "repo", args, log_file)

        assert success is False
        assert detail == UNQUALIFIED_DETAIL
        mock_comment.assert_not_called()
        mock_create_pr.assert_not_called()
        for issue in batch.issues:
            github = issue.get("github", {})
            assert "pr_number" not in github
            assert "batch_id" not in github
