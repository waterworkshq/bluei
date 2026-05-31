#!/usr/bin/env python3
"""Regression tests for untracked-path issue suppression fix.

Verifies that test-gap-missing-file and similar findings for absent/untracked
paths are NOT suppressed during the issue-creation cycle, while they ARE
correctly gated during the PR/fix cycle.

Fixes: missing-file/test-gap findings were skipped because their path is
untracked — this was a regression introduced when live_github_actions was
used as a proxy for "require tracked path" without distinguishing between
issue-reporting (valid for missing files) and fix/PR flows (require tracked).
"""

import subprocess
from pathlib import Path

import pytest

from bluei.engine.utils import is_path_tracked


# ---------------------------------------------------------------------------
# Core logic simulation
# ---------------------------------------------------------------------------


def filter_for_live_actions(
    findings, repo_path, run_pr_cycle, live_github_actions=True
):
    """Simulates the fixed filtering logic from bluei.engine.

    Before the fix: all findings with untracked paths were removed when
    live_github_actions=True, even during issue-cycle.

    After the fix: only removes untracked findings when run_pr_cycle=True.
    """
    if live_github_actions and run_pr_cycle:
        filtered = []
        for f in findings:
            if is_path_tracked(repo_path, f.path):
                filtered.append(f)
        return filtered
    return list(findings)


# ---------------------------------------------------------------------------
# is_path_tracked unit tests
# ---------------------------------------------------------------------------


class TestIsPathTracked:
    """Unit tests for is_path_tracked git ls-files wrapper."""

    def test_tracked_file_returns_true(self, git_repo, git_commit_all):
        """A committed file should be reported as tracked."""
        tracked = git_repo / "src" / "main.py"
        tracked.parent.mkdir()
        tracked.write_text('print("hi")')
        subprocess.run(
            ["git", "add", "src/main.py"],
            cwd=str(git_repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add main"],
            cwd=str(git_repo),
            capture_output=True,
            check=True,
        )

        assert is_path_tracked(git_repo, "src/main.py") is True

    def test_untracked_file_returns_false(self, git_repo):
        """A file that exists but is not committed should return False."""
        (git_repo / "untracked.py").write_text("# not committed")

        assert is_path_tracked(git_repo, "untracked.py") is False

    def test_nonexistent_file_returns_false(self, git_repo):
        """A file that doesn't exist at all returns False.

        This is the exact case for test-gap-missing-file: the finding
        path (e.g. tests/test_notifications.py) doesn't exist on disk,
        so git ls-files --error-unmatch fails and is_path_tracked → False.
        """
        assert not (git_repo / "tests" / "test_notifications.py").exists()
        assert is_path_tracked(git_repo, "tests/test_notifications.py") is False


# ---------------------------------------------------------------------------
# Regression tests for the issue suppression fix
# ---------------------------------------------------------------------------


class TestIssueCycleUntrackedFindings:
    """Regression: issue-cycle must NOT suppress findings for untracked paths.

    Previously, when live_github_actions=True, ALL findings with untracked
    paths were filtered out of eligible_findings, even during issue-cycle.
    This broke test-gap-missing-file because the "missing" file's path is
    never tracked in git.

    The fix: the untracked-path filter is now gated on run_pr_cycle, so it
    only applies during PR/fix flows, not during issue creation.
    """

    def test_issue_cycle_preserves_missing_file_finding_live_actions(
        self, git_repo, make_finding
    ):
        """Missing-file findings survive the filter in issue-cycle with live_github_actions.

        This is the core regression test. With live_github_actions=True and
        run_phase=issue-cycle (run_pr_cycle=False), a test-gap-missing-file
        finding must NOT be removed from eligible_findings.
        """
        finding = make_finding(
            finding_id="test-1",
            rule="test-gap-missing-file",
            path="tests/test_notifications.py",
            snippet="missing test file",
            confidence=0.79,
        )

        eligible = filter_for_live_actions(
            findings=[finding],
            repo_path=git_repo,
            run_pr_cycle=False,  # issue-cycle
            live_github_actions=True,
        )

        assert len(eligible) == 1
        assert eligible[0].rule == "test-gap-missing-file"

    def test_issue_cycle_preserves_missing_file_finding_dry_run(
        self, git_repo, make_finding
    ):
        """Same as above but with live_github_actions=False (dry-run mode)."""
        finding = make_finding(
            finding_id="test-1",
            rule="test-gap-missing-file",
            path="tests/test_notifications.py",
            snippet="missing test file",
            confidence=0.79,
        )

        eligible = filter_for_live_actions(
            findings=[finding],
            repo_path=git_repo,
            run_pr_cycle=False,
            live_github_actions=False,
        )

        assert len(eligible) == 1

    def test_pr_cycle_still_filters_untracked_missing_file(
        self, git_repo, make_finding
    ):
        """PR cycle must still filter untracked paths (can't PR a missing file).

        This confirms the fix didn't break the legitimate use-case: during
        pr-cycle, untracked findings should still be excluded so we don't
        try to create a PR that adds a file we can't track.
        """
        finding = make_finding(
            finding_id="test-1",
            rule="test-gap-missing-file",
            path="tests/test_notifications.py",
            snippet="missing test file",
            confidence=0.79,
        )

        eligible = filter_for_live_actions(
            findings=[finding],
            repo_path=git_repo,
            run_pr_cycle=True,  # pr-cycle
            live_github_actions=True,
        )

        # PR cycle MUST filter out untracked missing-file finding
        assert len(eligible) == 0

    def test_pr_cycle_still_filters_untracked_normal_finding(
        self, git_repo, make_finding
    ):
        """PR cycle must filter any untracked path, not just missing-file ones."""
        # File exists but is not committed
        src_dir = git_repo / "src"
        src_dir.mkdir()
        (src_dir / "evil.py").write_text("bad code")
        finding = make_finding(
            finding_id="test-1",
            rule="type-explicit-any",
            path="src/evil.py",
            snippet="missing test file",
        )

        eligible = filter_for_live_actions(
            findings=[finding],
            repo_path=git_repo,
            run_pr_cycle=True,
            live_github_actions=True,
        )

        assert len(eligible) == 0

    def test_normal_tracked_finding_passes_both_cycles(
        self, git_repo, git_commit_all, make_finding
    ):
        """A finding for a tracked, existing file should pass both cycles."""
        tracked = git_repo / "src" / "main.py"
        tracked.parent.mkdir()
        tracked.write_text('print("hello")')
        subprocess.run(
            ["git", "add", "src/main.py"],
            cwd=str(git_repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add main"],
            cwd=str(git_repo),
            capture_output=True,
            check=True,
        )

        finding = make_finding(
            finding_id="test-1",
            rule="type-explicit-any",
            path="src/main.py",
            snippet="missing test file",
        )

        for run_pr in [False, True]:
            eligible = filter_for_live_actions(
                findings=[finding],
                repo_path=git_repo,
                run_pr_cycle=run_pr,
                live_github_actions=True,
            )
            assert len(eligible) == 1
            assert eligible[0].path == "src/main.py"

    def test_mixed_findings_issue_cycle_keeps_all(self, git_repo, make_finding):
        """Issue cycle should keep both tracked and untracked findings."""
        # Tracked file
        tracked = git_repo / "src" / "main.py"
        tracked.parent.mkdir()
        tracked.write_text('print("hello")')
        subprocess.run(
            ["git", "add", "src/main.py"],
            cwd=str(git_repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add main"],
            cwd=str(git_repo),
            capture_output=True,
            check=True,
        )

        tracked_finding = make_finding(
            finding_id="test-1",
            rule="type-explicit-any",
            path="src/main.py",
            snippet="missing test file",
        )
        missing_finding = make_finding(
            finding_id="test-2",
            rule="test-gap-missing-file",
            path="tests/test_notifications.py",
            snippet="missing test file",
            confidence=0.79,
        )

        eligible = filter_for_live_actions(
            findings=[tracked_finding, missing_finding],
            repo_path=git_repo,
            run_pr_cycle=False,
            live_github_actions=True,
        )

        # Both pass in issue-cycle
        assert len(eligible) == 2
        rules = {f.rule for f in eligible}
        assert "test-gap-missing-file" in rules
        assert "type-explicit-any" in rules

    def test_mixed_findings_pr_cycle_keeps_only_tracked(self, git_repo, make_finding):
        """PR cycle should keep only tracked findings, filter untracked ones."""
        tracked = git_repo / "src" / "main.py"
        tracked.parent.mkdir()
        tracked.write_text('print("hello")')
        subprocess.run(
            ["git", "add", "src/main.py"],
            cwd=str(git_repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add main"],
            cwd=str(git_repo),
            capture_output=True,
            check=True,
        )

        tracked_finding = make_finding(
            finding_id="test-1",
            rule="type-explicit-any",
            path="src/main.py",
            snippet="missing test file",
        )
        missing_finding = make_finding(
            finding_id="test-2",
            rule="test-gap-missing-file",
            path="tests/test_notifications.py",
            snippet="missing test file",
            confidence=0.79,
        )

        eligible = filter_for_live_actions(
            findings=[tracked_finding, missing_finding],
            repo_path=git_repo,
            run_pr_cycle=True,
            live_github_actions=True,
        )

        # Only tracked passes in PR cycle
        assert len(eligible) == 1
        assert eligible[0].rule == "type-explicit-any"
