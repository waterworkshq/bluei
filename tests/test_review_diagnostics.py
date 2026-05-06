#!/usr/bin/env python3
"""Tests for review-care diagnostics in CLI."""

import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

from qa_agent.models import Repo, RepoConfig
from qa_agent.report import ReportGenerator
from qa_agent.state import StateManager


def _cli_path() -> Path:
    return Path(__file__).parents[1] / 'qa-agent'


def _cli_module():
    module_path = _cli_path()
    loader = SourceFileLoader('qa_agent_cli_module', str(module_path))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cli_executable_exists():
    """Test that the qa-agent CLI is executable."""
    cli_path = _cli_path()
    result = subprocess.run([cli_path, '--help'], capture_output=True, text=True)
    assert result.returncode == 0
    assert 'usage:' in result.stdout.lower() or 'Usage' in result.stdout


def test_doctor_command_runs():
    """Test that doctor command runs successfully."""
    cli_path = _cli_path()
    result = subprocess.run([cli_path, 'doctor'], capture_output=True, text=True)
    assert result.returncode in [0, 1]
    assert 'QA Agent Doctor' in result.stdout or result.returncode == 1


def test_status_command_runs():
    """Test that status command runs successfully."""
    cli_path = _cli_path()
    result = subprocess.run([cli_path, 'status'], capture_output=True, text=True)
    assert result.returncode == 0
    assert 'Repositories:' in result.stdout or 'QA Agent Status' in result.stdout


def test_review_diagnostics_exposes_pending_and_failed_push_states(tmp_path):
    cli = _cli_module()
    state = StateManager(tmp_path / 'repos')
    repo_name = 'demo'

    state_dir = state._get_state_dir(repo_name)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'status.json').write_text(json.dumps({
        'review_care': {
            'enabled': True,
            'active_managed_prs': 2,
            'review_blocked_prs': 0,
            'retry_eligible_prs': 0,
            'retry_planned_prs': 0,
            'retry_prepared_prs': 0,
            'retry_executed_prs': 1,
            'retry_failed_prs': 1,
            'retry_exhausted_prs': 0,
            'merge_ready_prs': 0,
            'paused_prs': 0,
            'last_review_cycle_at': '2026-03-21T14:30:00Z',
        }
    }), encoding='utf-8')
    state.save_active_prs(repo_name, {
        'prs': {
            '41': {
                'pr_number': 41,
                'branch': 'qa/review-41',
                'status': 'retry_pending_push',
                'merge_readiness': {'state': 'awaiting_operator_push'},
                'execution_result': {
                    'changed_files': ['src/a.ts', 'src/b.ts'],
                    'push_result': {'status': 'pending_operator_confirmation', 'target_branch': 'qa/review-41'},
                },
            },
            '42': {
                'pr_number': 42,
                'branch': 'qa/review-42',
                'status': 'retry_failed_push',
                'merge_readiness': {'state': 'blocked_by_review'},
                'execution_result': {
                    'changed_files': ['src/c.ts'],
                    'push_result': {'status': 'push_failed', 'target_branch': 'qa/review-42'},
                },
            },
        }
    })
    state.save_review_state(repo_name, {
        'prs': {
            '41': {'attempts_used': 1, 'last_action_reason': 'waiting for explicit approval'},
            '42': {'attempts_used': 2, 'last_action_reason': 'push rejected by remote'},
        }
    })

    diag = cli._load_review_care_diagnostics(state, repo_name)
    assert diag['pending_push_prs'] == 1
    assert diag['failed_push_prs'] == 1
    assert diag['pending_push_prs_detail'][0]['pr_number'] == 41
    assert diag['failed_push_prs_detail'][0]['pr_number'] == 42
    assert diag['pending_push_prs_detail'][0]['push_target_branch'] == 'qa/review-41'


def test_report_markdown_includes_pending_push_details(tmp_path):
    reporter = ReportGenerator(workspace=tmp_path)
    repo = Repo(
        config=RepoConfig(id='repo-1', name='demo', path='/tmp/demo', language='typescript'),
        current_findings_count=5,
        current_health_score=88.0,
        total_prs=2,
    )
    markdown = reporter.generate_markdown_report(
        repo=repo,
        baseline=None,
        health=None,
        history=[],
        findings_by_category={},
        review_care={
            'active_managed_prs': 1,
            'review_blocked_prs': 0,
            'retry_eligible_prs': 0,
            'retry_planned_prs': 0,
            'retry_prepared_prs': 0,
            'retry_executed_prs': 1,
            'pending_push_prs': 1,
            'failed_push_prs': 0,
            'retry_failed_prs': 0,
            'retry_exhausted_prs': 0,
            'merge_ready_prs': 0,
            'paused_prs': 0,
            'pending_push_prs_detail': [
                {'pr_number': 41, 'branch': 'qa/review-41', 'changed_files': ['a', 'b'], 'push_target_branch': 'qa/review-41'}
            ],
        },
    )
    assert 'Pending Push Approval' in markdown
    assert '#41' in markdown
    assert 'qa/review-41' in markdown
