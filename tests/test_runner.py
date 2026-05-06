#!/usr/bin/env python3
"""Pytest-native tests for Run Engine."""

from pathlib import Path

from qa_agent.models import RepoConfig, Run
from qa_agent.config import ConfigManager
from qa_agent.registry import RepoRegistry
from qa_agent.health import HealthEngine
from qa_agent.state import StateManager
from qa_agent.runner import RunEngine, RunOptions, RunResult


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
