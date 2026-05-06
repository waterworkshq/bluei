#!/usr/bin/env python3
"""Tests for qa-agent runner path hygiene in sandbox_local_runner."""

from argparse import Namespace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import SimpleNamespace

MODULE_PATH = Path(__file__).parents[1] / 'core' / 'sandbox_local_runner.py'
SPEC = spec_from_file_location('qa_agent_sandbox_local_runner', MODULE_PATH)
slr = module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
sys.modules[SPEC.name] = slr
SPEC.loader.exec_module(slr)


def _args() -> Namespace:
    return Namespace(
        repo_path=Path('/tmp/repo'),
        state_file=Path('/tmp/state.json'),
        log_file=Path('/tmp/run.log'),
        findings_file=Path('/tmp/findings.jsonl'),
        issues_file=Path('/tmp/issues.json'),
        worktree_root=Path('/tmp/worktrees'),
        open_issues_cap=20,
        open_prs_cap=5,
        issue_confidence_threshold=0.7,
        max_files_changed=5,
        max_loc_diff=200,
        max_prs_per_run=2,
        max_issues_per_run=10,
        finding_cooldown_seconds=14400,
        merge_cooldown_minutes=30,
        max_fix_attempts_per_issue=3,
        docs_index_file=Path('/tmp/docs_index.json'),
        fix_engine='claude',
        claude_cmd_template='claude --print "Read {prompt_file}"',
        refresh_docs_index=False,
        live_github_actions=True,
        auto_merge_sandbox=False,
        run_phase='issue-cycle',
    )


def test_runner_defaults_no_longer_point_to_pr_automation():
    assert 'pr-automation' not in str(slr.DEFAULT_STATE)
    assert 'pr-automation' not in str(slr.DEFAULT_LOG)
    assert 'pr-automation' not in str(slr.DEFAULT_DOCS_INDEX)
    assert slr.RUNNER_PATH.name == 'sandbox_local_runner.py'
    assert 'qa-agent' in str(slr.RUNNER_PATH)


def test_generated_cycle_commands_use_current_runner_path():
    args = _args()
    issue_cmd = slr.build_issue_cycle_command(args)
    reconcile_cmd = slr.build_reconcile_only_command(args)
    verify_cmd = slr.build_verification_only_command(args)

    runner_path = str(slr.RUNNER_PATH)
    assert runner_path in issue_cmd
    assert runner_path in reconcile_cmd
    assert runner_path in verify_cmd
    assert 'pr-automation/sandbox_local_runner.py' not in issue_cmd
    assert 'pr-automation/sandbox_local_runner.py' not in reconcile_cmd
    assert 'pr-automation/sandbox_local_runner.py' not in verify_cmd


def test_apply_claude_fix_cleans_up_prompt_file(tmp_path, monkeypatch):
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    main_repo = tmp_path / 'repo'
    main_repo.mkdir()
    log_file = tmp_path / 'run.log'
    finding = slr.Finding(
        finding_id='finding-1',
        repo='demo',
        path='src/demo.py',
        line=10,
        rule='demo-rule',
        snippet='bad code',
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
    )

    captured = {}

    def fake_run(*args, **kwargs):
        captured['cwd'] = kwargs.get('cwd')
        return SimpleNamespace(returncode=0, stdout='ok')

    monkeypatch.setattr(slr.apply_claude_fix.__globals__['subprocess'], 'run', fake_run)
    rc, output, prompt_file = slr.apply_claude_fix(
        worktree_path=worktree,
        repo_path=main_repo,
        finding=finding,
        baseline_checks={'baseline': ['pytest', '-q']},
        target_checks={},
        claude_cmd_template='echo ready',
        max_files_changed=5,
        max_loc_diff=200,
        log_file=log_file,
    )

    assert rc == 0
    assert output == 'ok'
    assert captured['cwd'] == str(worktree)
    assert prompt_file.endswith('.qa-fix-prompt.md')
    assert not (worktree / '.qa-fix-prompt.md').exists()


def test_git_commit_all_reports_no_changes_without_failing(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    log_file = tmp_path / 'run.log'

    calls = []

    def fake_run_capture(cmd, cwd):
        calls.append((tuple(cmd), cwd))
        if cmd[:3] == ['git', 'add', '-A']:
            return 0, ''
        if cmd[:4] == ['git', 'diff', '--cached', '--quiet']:
            return 0, ''
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setitem(slr.git_commit_all.__globals__, 'run_capture', fake_run_capture)

    result = slr.git_commit_all(repo, 'demo commit', log_file, dry_run=False)

    assert result == 'no_changes'
    assert any(cmd[:3] == ('git', 'add', '-A') for cmd, _ in calls)
    assert 'no staged changes to commit' in log_file.read_text()


def test_verify_fix_closed_matches_specific_finding_not_whole_file(tmp_path, monkeypatch):
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    log_file = tmp_path / 'run.log'

    target = slr.Finding(
        finding_id='target-finding',
        repo='demo',
        path='src/demo.py',
        line=10,
        rule='ruff-b904',
        snippet='raise AssertionError',
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=False,
    )
    other_same_file = slr.Finding(
        finding_id='other-finding',
        repo='demo',
        path='src/demo.py',
        line=20,
        rule='ruff-b904',
        snippet='raise RuntimeError',
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=False,
    )

    monkeypatch.setitem(
        slr.verify_fix_closed.__globals__,
        'discover_findings',
        lambda *args, **kwargs: [other_same_file],
    )

    assert slr.verify_fix_closed(worktree, target, log_file) is True
    assert 'still_firing=0' in log_file.read_text()
