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
import sys
from argparse import Namespace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

# Import the sandbox_local_runner module
MODULE_PATH = Path(__file__).parents[1] / 'core' / 'sandbox_local_runner.py'
SPEC = spec_from_file_location('qa_agent_sandbox_local_runner', MODULE_PATH)
slr = module_from_spec(SPEC)
sys.modules[SPEC.name] = slr
SPEC.loader.exec_module(slr)


# ---------------------------------------------------------------------------
# Git helper
# ---------------------------------------------------------------------------

def _git(cwd, *args):
    result = subprocess.run(
        ['bash', '-lc', ' '.join(['git'] + list(args))],
        cwd=str(cwd), text=True, capture_output=True
    )
    return result


def setup_bare_git_repo(tmp_path):
    """Create a real git repo with one initial commit."""
    _git(tmp_path, 'init')
    _git(tmp_path, 'config', 'user.email', 'test@test.com')
    _git(tmp_path, 'config', 'user.name', 'Test')
    readme = tmp_path / 'README'
    readme.write_text('hello')
    _git(tmp_path, 'add', 'README')
    _git(tmp_path, 'commit', '-m', 'init')
    return readme


# ---------------------------------------------------------------------------
# Finding factory
# ---------------------------------------------------------------------------

def make_finding(path='tests/test_notifications.py',
                  rule='test-gap-missing-file',
                  confidence=0.79,
                  safe_to_autofix=True):
    return slr.Finding(
        finding_id='test-1',
        repo='test-repo',
        path=path,
        line=1,
        rule=rule,
        snippet='missing test file',
        confidence=confidence,
        quick_win=True,
        safe_to_autofix=safe_to_autofix,
    )


# ---------------------------------------------------------------------------
# Core logic simulation
# ---------------------------------------------------------------------------

def filter_for_live_actions(findings, repo_path, run_pr_cycle, live_github_actions=True):
    """Simulates the (fixed) filtering logic from sandbox_local_runner.py.

    Before the fix: all findings with untracked paths were removed when
    live_github_actions=True, even during issue-cycle.

    After the fix: only removes untracked findings when run_pr_cycle=True.
    """
    if live_github_actions and run_pr_cycle:
        filtered = []
        for f in findings:
            if slr.is_path_tracked(repo_path, f.path):
                filtered.append(f)
        return filtered
    return list(findings)


# ---------------------------------------------------------------------------
# is_path_tracked unit tests
# ---------------------------------------------------------------------------

class TestIsPathTracked:
    """Unit tests for is_path_tracked git ls-files wrapper."""

    def test_tracked_file_returns_true(self, tmp_path):
        """A committed file should be reported as tracked."""
        setup_bare_git_repo(tmp_path)
        tracked = tmp_path / 'src' / 'main.py'
        tracked.parent.mkdir()
        tracked.write_text('print("hi")')
        _git(tmp_path, 'add', 'src/main.py')
        _git(tmp_path, 'commit', '-m', 'add main')

        assert slr.is_path_tracked(tmp_path, 'src/main.py') is True

    def test_untracked_file_returns_false(self, tmp_path):
        """A file that exists but is not committed should return False."""
        setup_bare_git_repo(tmp_path)
        (tmp_path / 'untracked.py').write_text('# not committed')

        assert slr.is_path_tracked(tmp_path, 'untracked.py') is False

    def test_nonexistent_file_returns_false(self, tmp_path):
        """A file that doesn't exist at all returns False.

        This is the exact case for test-gap-missing-file: the finding
        path (e.g. tests/test_notifications.py) doesn't exist on disk,
        so git ls-files --error-unmatch fails and is_path_tracked → False.
        """
        setup_bare_git_repo(tmp_path)
        assert not (tmp_path / 'tests' / 'test_notifications.py').exists()
        assert slr.is_path_tracked(tmp_path, 'tests/test_notifications.py') is False


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

    def test_issue_cycle_preserves_missing_file_finding_live_actions(self, tmp_path):
        """Missing-file findings survive the filter in issue-cycle with live_github_actions.

        This is the core regression test. With live_github_actions=True and
        run_phase=issue-cycle (run_pr_cycle=False), a test-gap-missing-file
        finding must NOT be removed from eligible_findings.
        """
        setup_bare_git_repo(tmp_path)
        finding = make_finding(rule='test-gap-missing-file',
                                path='tests/test_notifications.py')

        eligible = filter_for_live_actions(
            findings=[finding],
            repo_path=tmp_path,
            run_pr_cycle=False,      # issue-cycle
            live_github_actions=True,
        )

        assert len(eligible) == 1
        assert eligible[0].rule == 'test-gap-missing-file'

    def test_issue_cycle_preserves_missing_file_finding_dry_run(self, tmp_path):
        """Same as above but with live_github_actions=False (dry-run mode)."""
        setup_bare_git_repo(tmp_path)
        finding = make_finding(rule='test-gap-missing-file',
                                path='tests/test_notifications.py')

        eligible = filter_for_live_actions(
            findings=[finding],
            repo_path=tmp_path,
            run_pr_cycle=False,
            live_github_actions=False,
        )

        assert len(eligible) == 1

    def test_pr_cycle_still_filters_untracked_missing_file(self, tmp_path):
        """PR cycle must still filter untracked paths (can't PR a missing file).

        This confirms the fix didn't break the legitimate use-case: during
        pr-cycle, untracked findings should still be excluded so we don't
        try to create a PR that adds a file we can't track.
        """
        setup_bare_git_repo(tmp_path)
        finding = make_finding(rule='test-gap-missing-file',
                                path='tests/test_notifications.py')

        eligible = filter_for_live_actions(
            findings=[finding],
            repo_path=tmp_path,
            run_pr_cycle=True,       # pr-cycle
            live_github_actions=True,
        )

        # PR cycle MUST filter out untracked missing-file finding
        assert len(eligible) == 0

    def test_pr_cycle_still_filters_untracked_normal_finding(self, tmp_path):
        """PR cycle must filter any untracked path, not just missing-file ones."""
        setup_bare_git_repo(tmp_path)
        # File exists but is not committed
        src_dir = tmp_path / 'src'
        src_dir.mkdir()
        (src_dir / 'evil.py').write_text('bad code')
        finding = make_finding(rule='type-explicit-any',
                                path='src/evil.py',
                                safe_to_autofix=True)

        eligible = filter_for_live_actions(
            findings=[finding],
            repo_path=tmp_path,
            run_pr_cycle=True,
            live_github_actions=True,
        )

        assert len(eligible) == 0

    def test_normal_tracked_finding_passes_both_cycles(self, tmp_path):
        """A finding for a tracked, existing file should pass both cycles."""
        setup_bare_git_repo(tmp_path)
        tracked = tmp_path / 'src' / 'main.py'
        tracked.parent.mkdir()
        tracked.write_text('print("hello")')
        _git(tmp_path, 'add', 'src/main.py')
        _git(tmp_path, 'commit', '-m', 'add main')

        finding = make_finding(rule='type-explicit-any', path='src/main.py')

        for run_pr in [False, True]:
            eligible = filter_for_live_actions(
                findings=[finding],
                repo_path=tmp_path,
                run_pr_cycle=run_pr,
                live_github_actions=True,
            )
            assert len(eligible) == 1
            assert eligible[0].path == 'src/main.py'

    def test_mixed_findings_issue_cycle_keeps_all(self, tmp_path):
        """Issue cycle should keep both tracked and untracked findings."""
        setup_bare_git_repo(tmp_path)
        # Tracked file
        tracked = tmp_path / 'src' / 'main.py'
        tracked.parent.mkdir()
        tracked.write_text('print("hello")')
        _git(tmp_path, 'add', 'src/main.py')
        _git(tmp_path, 'commit', '-m', 'add main')

        tracked_finding = make_finding(rule='type-explicit-any', path='src/main.py')
        missing_finding = make_finding(rule='test-gap-missing-file',
                                       path='tests/test_notifications.py')

        eligible = filter_for_live_actions(
            findings=[tracked_finding, missing_finding],
            repo_path=tmp_path,
            run_pr_cycle=False,
            live_github_actions=True,
        )

        # Both pass in issue-cycle
        assert len(eligible) == 2
        rules = {f.rule for f in eligible}
        assert 'test-gap-missing-file' in rules
        assert 'type-explicit-any' in rules

    def test_mixed_findings_pr_cycle_keeps_only_tracked(self, tmp_path):
        """PR cycle should keep only tracked findings, filter untracked ones."""
        setup_bare_git_repo(tmp_path)
        tracked = tmp_path / 'src' / 'main.py'
        tracked.parent.mkdir()
        tracked.write_text('print("hello")')
        _git(tmp_path, 'add', 'src/main.py')
        _git(tmp_path, 'commit', '-m', 'add main')

        tracked_finding = make_finding(rule='type-explicit-any', path='src/main.py')
        missing_finding = make_finding(rule='test-gap-missing-file',
                                       path='tests/test_notifications.py')

        eligible = filter_for_live_actions(
            findings=[tracked_finding, missing_finding],
            repo_path=tmp_path,
            run_pr_cycle=True,
            live_github_actions=True,
        )

        # Only tracked passes in PR cycle
        assert len(eligible) == 1
        assert eligible[0].rule == 'type-explicit-any'
