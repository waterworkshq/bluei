import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.sandbox_local_runner import cli
from core.sandbox_local_runner import gh


def test_fetch_open_prs_for_merge_sorts_oldest_first(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gh,
        'gh_json',
        lambda *args, **kwargs: [
            {
                'number': 79,
                'url': 'https://example.com/pr/79',
                'title': 'newer',
                'state': 'OPEN',
                'isDraft': False,
                'createdAt': '2026-04-09T08:00:00+00:00',
                'headRefName': 'qa/live-79',
            },
            {
                'number': 64,
                'url': 'https://example.com/pr/64',
                'title': 'older',
                'state': 'OPEN',
                'isDraft': False,
                'createdAt': '2026-04-01T08:00:00+00:00',
                'headRefName': 'qa/live-64',
            },
            {
                'number': 90,
                'url': 'https://example.com/pr/90',
                'title': 'draft-oldest',
                'state': 'OPEN',
                'isDraft': True,
                'createdAt': '2026-03-30T08:00:00+00:00',
                'headRefName': 'qa/live-90',
            },
        ],
    )

    prs = gh.fetch_open_prs_for_merge('vikasagarwal101/zulip', cwd=tmp_path)
    assert [pr['number'] for pr in prs] == [64, 79, 90]


def test_evaluate_pr_mergeability_triages_dirty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gh,
        'gh_json',
        lambda *args, **kwargs: {
            'number': 78,
            'url': 'https://example.com/pr/78',
            'mergeStateStatus': 'DIRTY',
            'reviewDecision': 'APPROVED',
        },
    )

    result = gh.evaluate_pr_mergeability('vikasagarwal101/zulip', 78, cwd=tmp_path)

    assert result['eligible'] is False
    assert result['requires_pr_fix'] is True
    assert result['reason'] == 'merge-conflict-dirty'


def test_evaluate_pr_mergeability_allows_unknown_cautiously(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gh,
        'gh_json',
        lambda *args, **kwargs: {
            'number': 79,
            'url': 'https://example.com/pr/79',
            'mergeStateStatus': 'UNKNOWN',
            'reviewDecision': 'REVIEW_REQUIRED',
        },
    )

    result = gh.evaluate_pr_mergeability('vikasagarwal101/zulip', 79, cwd=tmp_path)

    assert result['eligible'] is True
    assert result['requires_pr_fix'] is False
    assert result['reason'] == 'merge-state-unknown-proceed-cautiously'


def test_evaluate_pr_mergeability_allows_unstable_when_only_pending_checks(monkeypatch, tmp_path):
    def fake_gh_json(cmd, cwd):
        payload = ' '.join(cmd)
        if 'mergeStateStatus' in payload:
            return {
                'number': 78,
                'url': 'https://example.com/pr/78',
                'mergeStateStatus': 'UNSTABLE',
                'reviewDecision': 'REVIEW_REQUIRED',
            }
        return {
            'number': 78,
            'url': 'https://example.com/pr/78',
            'statusCheckRollup': [
                {
                    'name': 'Greptile Review',
                    'status': 'IN_PROGRESS',
                    'conclusion': '',
                }
            ],
        }

    monkeypatch.setattr(gh, 'gh_json', fake_gh_json)

    result = gh.evaluate_pr_mergeability('vikasagarwal101/zulip', 78, cwd=tmp_path)

    assert result['eligible'] is True
    assert result['requires_pr_fix'] is False
    assert result['reason'] == 'merge-state-unstable-checks-pending-no-failures'


def test_evaluate_pr_mergeability_blocks_unstable_when_checks_fail(monkeypatch, tmp_path):
    def fake_gh_json(cmd, cwd):
        payload = ' '.join(cmd)
        if 'mergeStateStatus' in payload:
            return {
                'number': 78,
                'url': 'https://example.com/pr/78',
                'mergeStateStatus': 'UNSTABLE',
                'reviewDecision': 'REVIEW_REQUIRED',
            }
        return {
            'number': 78,
            'url': 'https://example.com/pr/78',
            'statusCheckRollup': [
                {
                    'name': 'CI',
                    'status': 'COMPLETED',
                    'conclusion': 'FAILURE',
                }
            ],
        }

    monkeypatch.setattr(gh, 'gh_json', fake_gh_json)

    result = gh.evaluate_pr_mergeability('vikasagarwal101/zulip', 78, cwd=tmp_path)

    assert result['eligible'] is False
    assert result['requires_pr_fix'] is False
    assert result['reason'] == 'merge-state-unstable-checks-failing:FAILURE'


def test_merge_failure_requires_pr_fix_detects_common_conflicts():
    assert gh.merge_failure_requires_pr_fix('Pull request is not mergeable: the merge commit cannot be cleanly created.')
    assert gh.merge_failure_requires_pr_fix('Head branch is out of date with the base branch')
    assert not gh.merge_failure_requires_pr_fix('merge blocked by branch protection')


def test_autonomous_review_gate_requires_merge_ready_artifact():
    ok, reason = cli._autonomous_review_gate_passes(
        {
            'prs': {
                '79': {
                    'last_action': 'merge_ready',
                    'last_review_comment_key': 'abc:merge_ready',
                    'last_snapshot': {
                        'merge_state_status': 'CLEAN',
                        'actionable_comment_count': 0,
                        'active_change_requesters': [],
                    },
                }
            }
        },
        79,
    )

    assert ok is True
    assert reason == 'review-artifact-merge-ready'


def test_autonomous_review_gate_allows_cautious_merge_states():
    ok, reason = cli._autonomous_review_gate_passes(
        {
            'prs': {
                '79': {
                    'last_action': 'merge_ready',
                    'last_review_comment_key': 'abc:merge_ready',
                    'last_snapshot': {
                        'merge_state_status': 'UNSTABLE',
                        'actionable_comment_count': 0,
                        'active_change_requesters': [],
                    },
                }
            }
        },
        79,
    )

    assert ok is True
    assert reason == 'review-artifact-merge-ready'


def test_merge_cycle_normalizes_legacy_unknown_merge_state_for_merge_ready_pr(monkeypatch, tmp_path):
    repo_path = Path('/home/vikas/.openclaw/workspace/qa-agent')
    state_file = tmp_path / 'state.json'
    findings_file = tmp_path / 'findings.jsonl'
    log_file = tmp_path / 'run.log'
    status_file = tmp_path / 'status.json'
    docs_index_file = tmp_path / 'docs-index.json'
    issues_file = tmp_path / 'issues.json'
    worktree_root = tmp_path / 'worktrees'
    lessons_file = tmp_path / 'lessons.md'

    issues_file.write_text(json.dumps({'issues': []}, indent=2))

    review_state_file = tmp_path / 'review_state.json'
    review_state_file.write_text(json.dumps({
        'prs': {
            '79': {
                'last_action': 'merge_ready',
                'last_review_comment_key': 'abc:merge_ready',
                'last_snapshot': {
                    'merge_state_status': 'UNKNOWN',
                    'actionable_comment_count': 0,
                    'active_change_requesters': [],
                },
            },
        }
    }))

    monkeypatch.setattr(cli, 'is_mnemo_available', lambda repo_path: False)
    monkeypatch.setattr(cli, 'assert_safe_repo', lambda repo_path: None)
    monkeypatch.setattr(cli, 'get_origin_url', lambda repo_path: 'git@github.com:vikasagarwal101/zulip.git')
    monkeypatch.setattr(cli, 'parse_github_repo', lambda origin_url: ('vikasagarwal101', 'zulip'))
    monkeypatch.setattr(cli, 'repo_is_sandbox', lambda repo_slug: True)
    monkeypatch.setattr(cli, 'fetch_open_prs_for_merge', lambda repo_slug, cwd: [
        {
            'number': 79,
            'url': 'https://example.com/pr/79',
            'title': 'merge ready but unknown state',
            'state': 'OPEN',
            'isDraft': False,
            'createdAt': '2026-04-08T00:00:00+00:00',
        },
    ])
    monkeypatch.setattr(cli, 'evaluate_pr_check_health', lambda *args, **kwargs: {
        'eligible': True,
        'has_checks': True,
        'reason': 'checks-pass-or-neutral',
    })
    monkeypatch.setattr(cli, 'evaluate_pr_reviews', lambda *args, **kwargs: {
        'eligible': False,
        'has_reviews': False,
        'reason': 'no-reviews-block',
    })
    monkeypatch.setattr(
        cli,
        'evaluate_pr_mergeability',
        lambda repo_slug, pr_number, cwd: {
            'eligible': False,
            'requires_pr_fix': False,
            'merge_state_status': 'UNKNOWN',
            'reason': 'merge-state-unknown',
        },
    )
    merge_calls = []
    monkeypatch.setattr(
        cli,
        'merge_pr',
        lambda repo_slug, pr_number, dry_run, cwd: merge_calls.append(pr_number) or (True, 'merged'),
    )
    monkeypatch.setattr(cli, 'reconcile_open_workload', lambda **kwargs: (0, 1, {'reason': 'test'}))

    argv = [
        'qa-agent',
        '--repo-path', str(repo_path),
        '--state-file', str(state_file),
        '--log-file', str(log_file),
        '--findings-file', str(findings_file),
        '--issues-file', str(issues_file),
        '--worktree-root', str(worktree_root),
        '--status-file', str(status_file),
        '--docs-index-file', str(docs_index_file),
        '--lessons-file', str(lessons_file),
        '--review-state-file', str(review_state_file),
        '--run-phase', 'merge-cycle',
        '--live-github-actions',
        '--auto-merge-sandbox',
        '--no-dry-run',
        '--merge-cooldown-minutes', '0',
    ]
    monkeypatch.setattr(sys, 'argv', argv)

    rc = cli.main()
    assert rc == 0
    assert merge_calls == [79]
    assert 'normalized legacy unknown merge-state to cautious pass' in log_file.read_text()


def test_merge_cycle_triages_conflict_then_merges_only_one_pr(monkeypatch, tmp_path):
    repo_path = Path('/home/vikas/.openclaw/workspace/qa-agent')
    state_file = tmp_path / 'state.json'
    findings_file = tmp_path / 'findings.jsonl'
    log_file = tmp_path / 'run.log'
    status_file = tmp_path / 'status.json'
    docs_index_file = tmp_path / 'docs-index.json'
    issues_file = tmp_path / 'issues.json'
    worktree_root = tmp_path / 'worktrees'
    lessons_file = tmp_path / 'lessons.md'

    issues_file.write_text(json.dumps({
        'issues': [
            {
                'issue_id': 'QA-0001',
                'finding_id': 'finding-1',
                'repo': 'zulip',
                'path': 'a.py',
                'line': 1,
                'rule': 'ruff-b904',
                'snippet': 'x',
                'confidence': 0.9,
                'quick_win': True,
                'safe_to_autofix': True,
                'status': 'pr_opened',
                'created_at': '2026-04-09T00:00:00+00:00',
                'updated_at': '2026-04-09T00:00:00+00:00',
                'history': [],
                'github': {
                    'pr_number': 1,
                    'pr_url': 'https://example.com/pr/1',
                    'branch': 'qa/live-ruff-b904-a1',
                },
            },
            {
                'issue_id': 'QA-0002',
                'finding_id': 'finding-2',
                'repo': 'zulip',
                'path': 'b.py',
                'line': 2,
                'rule': 'ruff-b904',
                'snippet': 'y',
                'confidence': 0.9,
                'quick_win': True,
                'safe_to_autofix': True,
                'status': 'pr_opened',
                'created_at': '2026-04-09T00:00:00+00:00',
                'updated_at': '2026-04-09T00:00:00+00:00',
                'history': [],
                'github': {
                    'pr_number': 2,
                    'pr_url': 'https://example.com/pr/2',
                    'branch': 'qa/live-ruff-b904-b2',
                },
            },
        ]
    }, indent=2))

    review_state_file = tmp_path / 'review_state.json'
    review_state_file.write_text(json.dumps({
        'prs': {
            '1': {
                'last_action': 'retry_exhausted',
                'last_review_comment_key': 'k1',
                'last_snapshot': {
                    'merge_state_status': 'CLEAN',
                    'actionable_comment_count': 4,
                    'active_change_requesters': [],
                },
            },
            '2': {
                'last_action': 'merge_ready',
                'last_review_comment_key': 'k2',
                'last_snapshot': {
                    'merge_state_status': 'CLEAN',
                    'actionable_comment_count': 0,
                    'active_change_requesters': [],
                },
            },
        }
    }))

    monkeypatch.setattr(cli, 'is_mnemo_available', lambda repo_path: False)
    monkeypatch.setattr(cli, 'assert_safe_repo', lambda repo_path: None)
    monkeypatch.setattr(cli, 'get_origin_url', lambda repo_path: 'git@github.com:vikasagarwal101/zulip.git')
    monkeypatch.setattr(cli, 'parse_github_repo', lambda origin_url: ('vikasagarwal101', 'zulip'))
    monkeypatch.setattr(cli, 'repo_is_sandbox', lambda repo_slug: True)
    monkeypatch.setattr(cli, 'fetch_open_prs_for_merge', lambda repo_slug, cwd: [
        {
            'number': 1,
            'url': 'https://example.com/pr/1',
            'title': 'conflicted',
            'state': 'OPEN',
            'isDraft': False,
            'createdAt': '2026-04-08T00:00:00+00:00',
        },
        {
            'number': 2,
            'url': 'https://example.com/pr/2',
            'title': 'mergeable',
            'state': 'OPEN',
            'isDraft': False,
            'createdAt': '2026-04-08T00:00:00+00:00',
        },
    ])
    monkeypatch.setattr(cli, 'evaluate_pr_check_health', lambda *args, **kwargs: {
        'eligible': True,
        'has_checks': True,
        'reason': 'checks-pass-or-neutral',
    })
    monkeypatch.setattr(cli, 'evaluate_pr_reviews', lambda repo_slug, pr_number, cwd: {
        1: {
            'eligible': True,
            'has_reviews': True,
            'reason': 'review-check-pass',
        },
        2: {
            'eligible': False,
            'has_reviews': False,
            'reason': 'no-reviews-block',
        },
    }[pr_number])
    monkeypatch.setattr(
        cli,
        'evaluate_pr_mergeability',
        lambda repo_slug, pr_number, cwd: {
            1: {
                'eligible': False,
                'requires_pr_fix': True,
                'merge_state_status': 'DIRTY',
                'reason': 'merge-conflict-dirty',
            },
            2: {
                'eligible': True,
                'requires_pr_fix': False,
                'merge_state_status': 'CLEAN',
                'reason': 'mergeable-state-pass',
            },
        }[pr_number],
    )
    merge_calls = []
    monkeypatch.setattr(
        cli,
        'merge_pr',
        lambda repo_slug, pr_number, dry_run, cwd: merge_calls.append(pr_number) or (True, 'merged'),
    )
    monkeypatch.setattr(cli, 'reconcile_open_workload', lambda **kwargs: (2, 1, {'reason': 'test'}))

    argv = [
        'qa-agent',
        '--repo-path', str(repo_path),
        '--state-file', str(state_file),
        '--log-file', str(log_file),
        '--findings-file', str(findings_file),
        '--issues-file', str(issues_file),
        '--worktree-root', str(worktree_root),
        '--status-file', str(status_file),
        '--docs-index-file', str(docs_index_file),
        '--lessons-file', str(lessons_file),
        '--run-phase', 'merge-cycle',
        '--live-github-actions',
        '--auto-merge-sandbox',
        '--no-dry-run',
        '--merge-cooldown-minutes', '0',
    ]
    monkeypatch.setattr(sys, 'argv', argv)

    rc = cli.main()
    assert rc == 0
    assert merge_calls == [2]

    saved = json.loads(issues_file.read_text())
    statuses = {item['issue_id']: item['status'] for item in saved['issues']}
    assert statuses['QA-0001'] == 'pr_merge_conflict'
    assert statuses['QA-0002'] == 'resolved_merged'
