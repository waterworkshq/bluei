#!/usr/bin/env python3
"""Pytest-native tests for Run Engine."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from bluei.app.models import RepoConfig, Run
from bluei.app.config import ConfigManager
from bluei.app.registry import RepoRegistry
from bluei.app.health import HealthEngine
from bluei.app.state import StateManager
from bluei.app.runner import RunEngine, RunOptions, RunResult


import pytest


@pytest.fixture
def runner_env(tmp_path):
    workspace = tmp_path / 'qa-agent'
    (workspace / 'repos').mkdir(parents=True)
    (workspace / 'plugins').mkdir()
    (workspace / 'logs').mkdir()
    (workspace / 'core').mkdir()

    config = ConfigManager(workspace)
    registry = RepoRegistry(config)
    health = HealthEngine()
    state = StateManager(config.repos_dir)
    runner = RunEngine(registry, state, health, config)
    return {
        'workspace': workspace,
        'config': config,
        'registry': registry,
        'health': health,
        'state': state,
        'runner': runner,
    }


@pytest.fixture
def test_repo(runner_env):
    repo_config = RepoConfig(
        id='test-001',
        name='test-repo',
        path='/tmp/test-repo',
        language='python',
    )
    return runner_env['registry'].create(repo_config)


def test_build_cli_args(runner_env, test_repo):
    args = runner_env['runner']._build_cli_args(test_repo, RunOptions(phase='issue-cycle', dry_run=True))
    assert '--repo-path' in args
    assert '--run-phase' in args
    assert '--dry-run' in args
    assert 'issue-cycle' in args


def test_parse_output(runner_env):
    output = """
    Running issue-cycle...
    findings=5 issues=3 prs=1
    fixes_verified=2 fixes_failed=0
    """
    metrics = runner_env['runner']._parse_output(output)
    assert metrics['findings_detected'] == 5
    assert metrics['issues_created'] == 3
    assert metrics['prs_created'] == 1
    assert metrics['fixes_verified'] == 2


def test_run_result_structure():
    run = Run(
        id='run-001',
        repo_id='test-repo',
        phase='issue-cycle',
        started_at='2026-01-01T00:00:00Z',
        status='completed',
    )
    result = RunResult(run=run, success=True, output='test output')
    assert result.run.id == 'run-001'
    assert result.success is True
    assert result.error is None


def test_run_options_defaults():
    options = RunOptions()
    assert options.phase == 'orchestrated'
    assert options.dry_run is True
    assert options.fix_engine is None


def test_save_and_load_run(runner_env, test_repo):
    run = Run(
        id='run-001',
        repo_id='test-001',
        phase='issue-cycle',
        started_at='2026-01-01T00:00:00Z',
        ended_at='2026-01-01T00:10:00Z',
        duration_seconds=600,
        status='completed',
        findings_detected=5,
    )
    runner_env['state'].save_run('test-repo', run)
    loaded = runner_env['state'].load_run('test-repo', 'run-001')
    assert loaded is not None
    assert loaded.id == 'run-001'
    assert loaded.findings_detected == 5


def test_run_history(runner_env, test_repo):
    for i in range(3):
        run = Run(
            id=f'run-{i:03d}',
            repo_id='test-001',
            phase='issue-cycle',
            started_at=f'2026-01-0{i}T00:00:00Z',
            status='completed',
        )
        runner_env['state'].save_run('test-repo', run)
    history = runner_env['runner'].get_run_history('test-repo', limit=2)
    assert len(history) == 2


def test_dry_run_method_builds_dry_flag(runner_env, test_repo):
    options = RunOptions(phase='issue-cycle', dry_run=True)
    args = runner_env['runner']._build_cli_args(test_repo, options)
    assert '--dry-run' in args


def test_build_cli_args_includes_pattern_store_path(runner_env, test_repo):
    options = RunOptions(phase='issue-cycle', dry_run=True)
    args = runner_env['runner']._build_cli_args(test_repo, options)
    assert '--pattern-store-path' in args
    idx = args.index('--pattern-store-path')
    path = Path(args[idx + 1])
    assert path.name == 'fix_patterns.jsonl'
    assert 'test-repo' in str(path)
    assert 'state' in str(path)


def test_build_cli_args_pattern_store_path_even_when_file_missing(runner_env, test_repo):
    options = RunOptions(phase='issue-cycle', dry_run=False)
    args = runner_env['runner']._build_cli_args(test_repo, options)
    assert '--pattern-store-path' in args
    idx = args.index('--pattern-store-path')
    assert not Path(args[idx + 1]).exists()


def test_state_manager_get_fix_patterns_file(tmp_path):
    state = StateManager(tmp_path / 'repos')
    result = state.get_fix_patterns_file('my-repo')
    assert result == tmp_path / 'repos' / 'my-repo' / 'state' / 'fix_patterns.jsonl'


def test_state_manager_get_fix_patterns_file_creates_parent(tmp_path):
    state = StateManager(tmp_path / 'repos')
    result = state.get_fix_patterns_file('my-repo')
    assert not result.parent.exists()
    result.parent.mkdir(parents=True, exist_ok=True)
    assert result.parent.exists()


def _write_failed_status(runner_env, metrics):
    path = runner_env['state']._get_state_dir('test-repo') / 'status.json'
    path.write_text(json.dumps({'latest_run_metrics': metrics}), encoding='utf-8')
    return path


def _run_failed_engine(runner_env, test_repo, stderr):
    proc = MagicMock(returncode=1, stdout='', stderr=stderr)
    with patch('bluei.app.runner.subprocess.run', return_value=proc):
        return runner_env['runner'].run(
            test_repo, RunOptions(phase='issue-cycle', dry_run=False)
        )


@pytest.mark.parametrize(
    'reason_field, raw_reason',
    [
        ('failure_reason', '  baseline \n checks failed  '),
        ('selected_error', ' selected repair did not verify '),
        ('failure_reason', 'code ' * 200),
    ],
)
def test_failed_run_prefers_current_run_structured_reason(
    runner_env, test_repo, monkeypatch, reason_field, raw_reason
):
    monkeypatch.setattr('bluei.app.runner.generate_id', lambda _prefix: 'run-current')
    status = _write_failed_status(runner_env, {
        'run_id': 'run-current',
        'run_completed': False,
        reason_field: raw_reason,
    })
    result = _run_failed_engine(runner_env, test_repo, 'raw engine stderr')
    expected = ' '.join(raw_reason.split())[:500]
    assert result.success is False
    assert result.run.error == expected
    assert result.error == expected
    assert json.loads(status.read_text())['run_interruption']['error'] == expected


def test_failed_run_ignores_stale_run_structured_reason(
    runner_env, test_repo, monkeypatch
):
    monkeypatch.setattr('bluei.app.runner.generate_id', lambda _prefix: 'run-current')
    status = _write_failed_status(runner_env, {
        'run_id': 'stale-run',
        'run_completed': False,
        'failure_reason': 'stale-failure-code',
        'selected_error': 'stale selected detail',
    })
    result = _run_failed_engine(runner_env, test_repo, 'real engine stderr')
    assert result.run.error == 'real engine stderr'
    assert json.loads(status.read_text())['run_interruption']['error'] == 'real engine stderr'
    assert 'stale' not in (result.run.error or '')


@pytest.mark.parametrize(
    'metrics, stderr, expected',
    [
        ({'run_id': 'run-current', 'run_completed': False, 'failure_reason': None},
         'engine boom', 'engine boom'),
        ({'run_id': 'run-current', 'run_completed': False, 'failure_reason': {'raw': 'dict'}},
         'engine boom', 'engine boom'),
        ({'run_id': 'run-current', 'run_completed': False, 'failure_reason': '   '},
         'engine boom', 'engine boom'),
        ({'run_id': 'run-current', 'run_completed': False, 'failure_reason': '   ',
          'selected_error': 'current selected failure'}, 'engine boom', 'current selected failure'),
        ({'run_id': 'run-current', 'run_completed': False, 'failure_reason': {'bad': 'shape'},
          'selected_error': 'current selected failure'}, 'engine boom', 'current selected failure'),
        ({}, 'engine boom', 'engine boom'),
        ({'run_id': 'run-current', 'run_completed': False},
         '', 'engine exited rc=1; scan incomplete'),
    ],
)
def test_failed_run_without_usable_structured_reason_falls_back(
    runner_env, test_repo, monkeypatch, metrics, stderr, expected
):
    monkeypatch.setattr('bluei.app.runner.generate_id', lambda _prefix: 'run-current')
    _write_failed_status(runner_env, metrics)
    result = _run_failed_engine(runner_env, test_repo, stderr)
    assert result.run.error == expected
