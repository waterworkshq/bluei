#!/usr/bin/env python3
"""Pytest-native tests for State Manager."""

from qa_agent.models import Finding, Run
from qa_agent.state import StateManager

import pytest


@pytest.fixture
def state_manager(tmp_path):
    return StateManager(tmp_path / 'repos')


def make_finding(**overrides):
    data = {
        'finding_id': 'f1',
        'repo': '/tmp/test',
        'path': 'test.py',
        'line': 10,
        'rule': 'test',
        'snippet': 'test',
        'confidence': 0.8,
        'quick_win': True,
        'safe_to_autofix': True,
    }
    data.update(overrides)
    return Finding(**data)


def test_append_findings(state_manager):
    count = state_manager.append_findings('test-repo', [make_finding()])
    assert count == 1
    loaded = state_manager.load_findings('test-repo')
    assert len(loaded) == 1


def test_no_duplicate_findings(state_manager):
    findings = [make_finding()]
    state_manager.append_findings('test-repo', findings)
    count = state_manager.append_findings('test-repo', findings)
    assert count == 0


def test_clear_findings(state_manager):
    state_manager.append_findings('test-repo', [make_finding()])
    state_manager.clear_findings('test-repo')
    assert len(state_manager.load_findings('test-repo')) == 0


def test_save_load_issues(state_manager):
    issues = {'issues': [{'id': 'QA-001', 'title': 'Test issue'}]}
    state_manager.save_issues('test-repo', issues)
    assert state_manager.load_issues('test-repo') == issues


def test_save_load_state(state_manager):
    state = {'open_issues': 5, 'open_prs': 2, 'created': ['item1'], 'finding_activity': {}}
    state_manager.save_state('test-repo', state)
    loaded = state_manager.load_state('test-repo')
    assert loaded['open_issues'] == 5
    assert loaded['open_prs'] == 2


def test_save_load_run(state_manager):
    run = Run(
        id='run-001',
        repo_id='test-repo',
        phase='orchestrated',
        started_at='2026-01-01T00:00:00Z',
        ended_at='2026-01-01T00:10:00Z',
        duration_seconds=600,
        status='completed',
    )
    state_manager.save_run('test-repo', run)
    loaded = state_manager.load_run('test-repo', 'run-001')
    assert loaded is not None
    assert loaded.id == 'run-001'
    assert loaded.status == 'completed'


def test_list_runs(state_manager):
    for i in range(3):
        run = Run(
            id=f'run-{i:03d}',
            repo_id='test-repo',
            phase='issue-cycle',
            started_at=f'2026-01-0{i}T00:00:00Z',
            status='completed',
        )
        state_manager.save_run('test-repo', run)
    runs = state_manager.list_runs('test-repo', limit=2)
    assert len(runs) == 2


def test_save_load_baseline(state_manager):
    baseline = {
        'id': 'baseline-001',
        'repo_id': 'test-repo',
        'captured_at': '2026-01-01T00:00:00Z',
        'findings_total': 10,
        'health_score': 75.0,
    }
    state_manager.save_baseline('test-repo', baseline)
    loaded = state_manager.load_baseline('test-repo', 'baseline-001')
    assert loaded is not None
    assert loaded['id'] == 'baseline-001'
    assert loaded['health_score'] == 75.0


def test_list_baselines(state_manager):
    for i in range(2):
        baseline = {'id': f'baseline-{i:03d}', 'repo_id': 'test-repo', 'findings_total': i * 5}
        state_manager.save_baseline('test-repo', baseline)
    baselines = state_manager.list_baselines('test-repo')
    assert len(baselines) == 2


def test_review_state_round_trip(state_manager):
    active = {'prs': {'12': {'pr_number': 12, 'status': 'pending_review'}}}
    review = {'prs': {'12': {'last_snapshot_fingerprint': 'abc', 'retry_eligible': True}}}
    state_manager.save_active_prs('test-repo', active)
    state_manager.save_review_state('test-repo', review)

    loaded_active = state_manager.load_active_prs('test-repo')
    loaded_review = state_manager.load_review_state('test-repo')
    assert loaded_active['prs']['12']['pr_number'] == 12
    assert loaded_review['prs']['12']['retry_eligible'] is True


def test_append_review_event(state_manager):
    state_manager.append_review_event('test-repo', {'event': 'review_feedback_detected', 'pr_number': 12})
    path = state_manager.get_review_events_file('test-repo')
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert 'review_feedback_detected' in lines[0]


def test_atomic_json_write_leaves_no_tmp_files(state_manager):
    """Verify that save methods do not leave stray .tmp files behind."""
    issues = {'issues': [{'id': 'QA-001', 'title': 'Test issue'}]}
    state_manager.save_issues('test-repo', issues)

    state = {'open_issues': 5, 'open_prs': 2, 'created': [], 'finding_activity': {}}
    state_manager.save_state('test-repo', state)

    state_manager.save_active_prs('test-repo', {'prs': {'1': {'pr_number': 1}}})
    state_manager.save_review_state('test-repo', {'prs': {}})

    # Collect all state dirs that may have been created
    repo_dir = state_manager._get_repo_dir('test-repo')
    tmp_files = list(repo_dir.rglob('*.tmp'))
    assert tmp_files == [], f"Unexpected .tmp files found: {tmp_files}"


# --- Phase B2: Autonomous-review state surfaces ---

def test_save_load_review_run(state_manager):
    run_data = {
        'run_id': 'rr-20260329-001',
        'repo': 'test-repo',
        'pr_number': 42,
        'started_at': '2026-03-29T00:00:00Z',
        'status': 'running',
    }
    path = state_manager.save_review_run('test-repo', run_data)
    loaded = state_manager.load_review_run('test-repo', 'rr-20260329-001')
    assert loaded is not None
    assert loaded['run_id'] == 'rr-20260329-001'
    assert loaded['pr_number'] == 42
    assert loaded['status'] == 'running'
    # versioned defaults are applied
    assert loaded['version'] == 1
    assert loaded['findings_count'] == 0
    assert loaded['publish_status'] == 'none'


def test_list_review_runs(state_manager):
    for i in range(3):
        state_manager.save_review_run('test-repo', {
            'run_id': f'rr-{i:03d}',
            'repo': 'test-repo',
            'status': 'completed',
            'started_at': f'2026-01-01T00:00:0{i}Z',
        })
    runs = state_manager.list_review_runs('test-repo', limit=2)
    assert len(runs) == 2
    # newest first
    assert runs[0]['run_id'] == 'rr-002'


def test_load_nonexistent_review_run(state_manager):
    assert state_manager.load_review_run('test-repo', 'does-not-exist') is None


def test_save_load_review_finding(state_manager):
    finding_data = {
        'finding_id': 'rf-abc123',
        'repo': 'test-repo',
        'path': 'src/foo.py',
        'line': 10,
        'rule': 'unused-import',
        'snippet': 'import os  # never used',
        'confidence': 0.95,
        'severity': 'low',
    }
    path = state_manager.save_review_finding('test-repo', 'rf-abc123', finding_data)
    loaded = state_manager.load_review_finding('test-repo', 'rf-abc123')
    assert loaded is not None
    assert loaded['finding_id'] == 'rf-abc123'
    assert loaded['rule'] == 'unused-import'
    assert loaded['version'] == 1
    assert 'saved_at' in loaded


def test_load_nonexistent_review_finding(state_manager):
    assert state_manager.load_review_finding('test-repo', 'does-not-exist') is None


def test_append_review_findings_dedupe(state_manager):
    findings = [
        {'finding_id': 'rf-001', 'repo': 'test-repo', 'rule': 'a'},
        {'finding_id': 'rf-002', 'repo': 'test-repo', 'rule': 'b'},
    ]
    written = state_manager.append_review_findings('test-repo', findings)
    assert written == 2

    # append same IDs again — should be deduplicated
    written2 = state_manager.append_review_findings('test-repo', findings)
    assert written2 == 0

    all_findings = state_manager.load_review_findings('test-repo')
    assert len(all_findings) == 2


def test_load_review_findings_empty(state_manager):
    assert state_manager.load_review_findings('nonexistent-repo') == []


def test_append_feedback_event(state_manager):
    event = {
        'source': 'github_review_comment',
        'pr_number': 12,
        'finding_id': 'rf-001',
        'signal': 'positive',
        'payload': {'body': 'LGTM'},
    }
    state_manager.append_feedback_event('test-repo', event)
    events = state_manager.load_feedback_events('test-repo')
    assert len(events) == 1
    assert events[0]['source'] == 'github_review_comment'
    assert events[0]['signal'] == 'positive'
    assert events[0]['version'] == 1
    assert events[0]['timestamp'] is not None


def test_load_feedback_events_empty(state_manager):
    assert state_manager.load_feedback_events('nonexistent-repo') == []


def test_save_load_learned_rules(state_manager):
    rules_data = {
        'rules': [
            {'id': 'rule-001', 'pattern': 'TODO without author', 'status': 'active'},
            {'id': 'rule-002', 'pattern': 'print() left in', 'status': 'tentative'},
        ],
        'active_count': 1,
        'tentative_count': 1,
    }
    state_manager.save_learned_rules('test-repo', rules_data)
    loaded = state_manager.load_learned_rules('test-repo')
    assert len(loaded['rules']) == 2
    assert loaded['active_count'] == 1
    assert loaded['tentative_count'] == 1
    assert loaded['version'] == 1


def test_learned_rules_default_when_missing(state_manager):
    loaded = state_manager.load_learned_rules('brand-new-repo')
    assert loaded['version'] == 1
    assert loaded['rules'] == []
    assert loaded['active_count'] == 0


def test_save_load_review_publish_state(state_manager):
    pub_state = {
        'findings': {
            'rf-001': {'status': 'published', 'published_at': '2026-03-29T01:00:00Z'},
            'rf-002': {'status': 'pending'},
        },
        'runs': {
            'rr-001': {'status': 'published'},
        },
    }
    state_manager.save_review_publish_state('test-repo', pub_state)
    loaded = state_manager.load_review_publish_state('test-repo')
    assert 'rf-001' in loaded['findings']
    assert loaded['findings']['rf-001']['status'] == 'published'
    assert loaded['version'] == 1


def test_review_publish_state_default_when_missing(state_manager):
    loaded = state_manager.load_review_publish_state('nonexistent-repo')
    assert loaded['version'] == 1
    assert loaded['findings'] == {}
    assert loaded['runs'] == {}


def test_review_runs_no_tmp_files(state_manager):
    """Phase B2 surfaces should not leave stray .tmp files."""
    state_manager.save_review_run('test-repo', {'run_id': 'rr-tmp-test', 'repo': 'test-repo'})
    state_manager.save_learned_rules('test-repo', {'rules': []})
    state_manager.save_review_publish_state('test-repo', {'findings': {}})
    repo_dir = state_manager._get_repo_dir('test-repo')
    tmp_files = list(repo_dir.rglob('*.tmp'))
    assert tmp_files == [], f"Unexpected .tmp files: {tmp_files}"


def test_review_findings_separate_from_issue_findings(state_manager):
    """Review findings (rf-*) and issue findings (f-*) must not share storage."""
    # issue finding (old surface)
    state_manager.append_findings('test-repo', [make_finding(finding_id='f-old', rule='old-rule')])
    # review finding (new surface)
    state_manager.append_review_findings('test-repo', [
        {'finding_id': 'rf-new', 'repo': 'test-repo', 'rule': 'review-rule'}
    ])

    issue_findings = state_manager.load_findings('test-repo')
    review_findings = state_manager.load_review_findings('test-repo')

    issue_ids = {f.finding_id for f in issue_findings}
    review_ids = {f['finding_id'] for f in review_findings}

    assert 'f-old' in issue_ids
    assert 'rf-new' not in issue_ids
    assert 'rf-new' in review_ids
    assert 'f-old' not in review_ids


# --- Shallow-copy / nested-default leak regression tests ---

def test_review_publish_state_deep_copy_isolation(state_manager):
    """Mutating a loaded-default publish state must not leak into module-level default."""
    from qa_agent.state import DEFAULT_REVIEW_PUBLISH_STATE

    # Load two repos that both have no file on disk
    state_a = state_manager.load_review_publish_state('repo-a')
    state_b = state_manager.load_review_publish_state('repo-b')

    # Mutate state_a's nested structures
    state_a['findings']['leaked-finding'] = {'status': 'published'}
    state_a['runs']['leaked-run'] = {'status': 'published'}

    # DEFAULT must be pristine
    assert DEFAULT_REVIEW_PUBLISH_STATE['findings'] == {}, \
        "Module-level DEFAULT_REVIEW_PUBLISH_STATE was mutated!"
    assert DEFAULT_REVIEW_PUBLISH_STATE['runs'] == {}, \
        "Module-level DEFAULT_REVIEW_PUBLISH_STATE was mutated!"

    # state_b must also be pristine (not contaminated by state_a's mutation)
    assert state_b['findings'] == {}
    assert state_b['runs'] == {}


def test_active_prs_deep_copy_isolation(state_manager):
    """Mutating a loaded-default active_prs state must not affect module-level default."""
    from qa_agent.state import DEFAULT_ACTIVE_PRS_STATE

    prs_a = state_manager.load_active_prs('repo-a')
    prs_b = state_manager.load_active_prs('repo-b')

    prs_a['prs']['123'] = {'pr_number': 123, 'status': 'tracking'}

    assert DEFAULT_ACTIVE_PRS_STATE['prs'] == {}
    assert prs_b['prs'] == {}


def test_review_state_deep_copy_isolation(state_manager):
    """Mutating a loaded-default review_state must not affect module-level default."""
    from qa_agent.state import DEFAULT_REVIEW_STATE

    rev_a = state_manager.load_review_state('repo-a')
    rev_b = state_manager.load_review_state('repo-b')

    rev_a['prs']['999'] = {'pr_number': 999, 'retry_eligible': True}

    assert DEFAULT_REVIEW_STATE['prs'] == {}
    assert rev_b['prs'] == {}


def test_learned_rules_deep_copy_isolation(state_manager):
    """Mutating a loaded-default learned_rules must not affect module-level default."""
    from qa_agent.state import DEFAULT_LEARNED_RULES

    rules_a = state_manager.load_learned_rules('repo-a')
    rules_b = state_manager.load_learned_rules('repo-b')

    rules_a['rules'].append({'id': 'injected-rule', 'pattern': 'TEST', 'status': 'active'})

    assert DEFAULT_LEARNED_RULES['rules'] == []
    assert rules_b['rules'] == []
