import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.sandbox_local_runner import gh, linters
from core.sandbox_local_runner.cli import _hydrate_worktree_dependencies, _reconcile_issue_pr_link
from core.sandbox_local_runner.lifecycle import choose_validation_baseline, _normalize_check_output
from core.sandbox_local_runner.orchestrator import count_failed_fix_attempts


def test_finding_from_issue_record_normalizes_legacy_refactor_rule_names():
    finding = gh.finding_from_issue_record(
        {
            'finding_id': 'f1',
            'repo': '/tmp/repo',
            'path': 'src/file.ts',
            'line': 12,
            'rule': 'max-lines',
            'snippet': 'too many lines',
            'confidence': 0.85,
            'safe_to_autofix': True,
        }
    )

    assert finding is not None
    assert finding.rule == 'xo-max-lines'


def test_discover_typescript_type_findings_detects_untyped_import(monkeypatch, tmp_path):
    (tmp_path / 'tsconfig.json').write_text('{}')

    compiler_output = (
        f"{tmp_path}/src/main.ts(3,24): error TS7016: Could not find a declaration file for module 'left-pad'. "
        "'/tmp/node_modules/left-pad/index.js' implicitly has an 'any' type.\n"
    )
    monkeypatch.setattr(linters, 'run_capture', lambda *args, **kwargs: (2, compiler_output))

    findings = linters.discover_typescript_type_findings(tmp_path, tmp_path / 'run.log')

    assert len(findings) == 1
    assert findings[0].rule == 'type-untyped-import'
    assert findings[0].line == 3


def test_discover_test_coverage_findings_detects_uncovered_line(tmp_path):
    coverage_dir = tmp_path / 'coverage'
    coverage_dir.mkdir()
    (tmp_path / 'package.json').write_text('{}')
    (coverage_dir / 'coverage-final.json').write_text(
        json.dumps(
            {
                str(tmp_path / 'src' / 'main.ts'): {
                    'branches': {},
                    'functions': {},
                    'statementMap': {
                        '1': {
                            'start': {'line': 14, 'column': 0},
                            'end': {'line': 14, 'column': 20},
                        }
                    },
                    's': {'1': 0},
                }
            }
        )
    )

    findings = linters.discover_test_coverage_findings(tmp_path, tmp_path / 'run.log')

    assert len(findings) == 1
    assert findings[0].rule == 'test-coverage-line'
    assert findings[0].line == 14


def test_reconcile_issue_pr_link_reopens_issue_when_linked_pr_is_closed(monkeypatch, tmp_path):
    issue = {
        'issue_id': 'QA-0025',
        'finding_id': 'abc123',
        'status': 'pr_opened',
        'github': {
            'pr_number': 33,
            'pr_url': 'https://github.com/example/repo/pull/33',
            'branch': 'qa/live-abc123',
        },
        'history': [],
    }

    monkeypatch.setattr(
        'core.sandbox_local_runner.cli.find_existing_github_pr',
        lambda *args, **kwargs: {'number': 33, 'url': 'https://github.com/example/repo/pull/33', 'state': 'CLOSED', 'headRefName': 'qa/live-abc123'},
    )

    should_skip = _reconcile_issue_pr_link(
        issue=issue,
        repo_slug='example/repo',
        repo_path=tmp_path,
        log_file=tmp_path / 'run.log',
    )

    assert should_skip is False
    assert issue['status'] == 'open'
    assert 'pr_number' not in issue['github']
    assert 'pr_url' not in issue['github']


def test_reconcile_issue_pr_link_keeps_open_prs_skipped(monkeypatch, tmp_path):
    issue = {
        'issue_id': 'QA-0026',
        'finding_id': 'def456',
        'status': 'pr_opened',
        'github': {
            'pr_number': 44,
            'pr_url': 'https://github.com/example/repo/pull/44',
        },
        'history': [],
    }

    monkeypatch.setattr(
        'core.sandbox_local_runner.cli.find_existing_github_pr',
        lambda *args, **kwargs: {'number': 44, 'url': 'https://github.com/example/repo/pull/44', 'state': 'OPEN', 'headRefName': 'qa/live-def456'},
    )

    should_skip = _reconcile_issue_pr_link(
        issue=issue,
        repo_slug='example/repo',
        repo_path=tmp_path,
        log_file=tmp_path / 'run.log',
    )

    assert should_skip is True
    assert issue['github']['pr_number'] == 44
    assert issue['github']['branch'] == 'qa/live-def456'


def test_choose_validation_baseline_prefers_worktree_when_repo_and_worktree_drift(tmp_path):
    repo_baseline = {
        'baseline-0': {'rc': 1, 'fingerprint': 'repo-fp'},
        'baseline-1': {'rc': 0, 'fingerprint': ''},
    }
    worktree_baseline = {
        'baseline-0': {'rc': 1, 'fingerprint': 'worktree-fp'},
        'baseline-1': {'rc': 0, 'fingerprint': ''},
    }

    chosen = choose_validation_baseline(
        repo_baseline_results=repo_baseline,
        worktree_baseline_results=worktree_baseline,
        log_file=tmp_path / 'run.log',
    )

    assert chosen is worktree_baseline


def test_choose_validation_baseline_keeps_repo_when_no_drift(tmp_path):
    repo_baseline = {
        'baseline-0': {'rc': 1, 'fingerprint': 'same-fp'},
        'baseline-1': {'rc': 0, 'fingerprint': ''},
    }
    worktree_baseline = {
        'baseline-0': {'rc': 1, 'fingerprint': 'same-fp'},
        'baseline-1': {'rc': 0, 'fingerprint': ''},
    }

    chosen = choose_validation_baseline(
        repo_baseline_results=repo_baseline,
        worktree_baseline_results=worktree_baseline,
        log_file=tmp_path / 'run.log',
    )

    assert chosen is repo_baseline


def test_count_failed_fix_attempts_resets_after_reopen():
    issue = {
        'history': [
            {'event': 'open'},
            {'event': 'fix_failed_verification'},
            {'event': 'needs-human-max-retries-exceeded'},
            {'event': 'open', 'detail': 'manual reset after policy change'},
            {'event': 'fix_failed_verification'},
        ]
    }

    assert count_failed_fix_attempts(issue) == 1


def test_hydrate_worktree_dependencies_links_node_modules(tmp_path):
    repo_path = tmp_path / 'repo'
    worktree_path = tmp_path / 'worktree'
    (repo_path / 'node_modules').mkdir(parents=True)
    worktree_path.mkdir()

    _hydrate_worktree_dependencies(repo_path, worktree_path, tmp_path / 'run.log')

    linked = worktree_path / 'node_modules'
    assert linked.is_symlink()
    assert linked.resolve() == (repo_path / 'node_modules').resolve()


def test_normalize_check_output_ignores_passing_test_noise(tmp_path):
    out = """> ky@1.14.3 test
> xo && npm run build && ava

  test/hooks.ts:1527:1
  ⚠  1527:1  File has too many lines (4203). Maximum allowed is 1500.              max-lines

  1 warning

  ✔ fetch › vendor-specific options like `next` are passed to fetch even when Request is patched
  6.43s
"""

    normalized = _normalize_check_output(out, tmp_path)

    assert 'File has too many lines' in normalized
    assert 'vendor-specific options' not in normalized
    assert '1 warning' not in normalized
    assert '6.43s' not in normalized



def test_verify_fix_closed_treats_legacy_complexity_issue_as_closed_when_only_xo_rule_exists(monkeypatch, tmp_path):
    from core.sandbox_local_runner.lifecycle import verify_fix_closed
    from core.sandbox_local_runner.models import Finding

    finding = Finding(
        finding_id='legacy-complexity-id',
        repo='repo',
        path='source/core/Ky.ts',
        line=381,
        rule='complexity',
        snippet='complexity 23',
        confidence=0.8,
        quick_win=False,
        safe_to_autofix=True,
    )

    monkeypatch.setattr(
        'core.sandbox_local_runner.lifecycle.discover_findings',
        lambda *args, **kwargs: [
            Finding(
                finding_id='new-xo-id',
                repo='repo',
                path='source/types/ResponsePromise.ts',
                line=21,
                rule='xo-no-warning-comments',
                snippet='TODO',
                confidence=0.8,
                quick_win=True,
                safe_to_autofix=True,
            )
        ],
    )

    assert verify_fix_closed(tmp_path, finding, tmp_path / 'run.log') is True
