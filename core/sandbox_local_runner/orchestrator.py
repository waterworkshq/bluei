"""sandbox_local_runner.orchestrator — Cycle command builders and discover_findings."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .constants import (
    CLAUDE_REQUIRED_RULES,
    DEFAULT_DOCS_INDEX,
    DEFAULT_LOG,
    DETECTOR_CATALOG,
    MAX_LINES_REFACTOR_LIMIT,
    MAX_LINES_REFACTOR_TARGET,
    RULE_TARGET_CHECKS,
    RUNNER_PATH,
)
from .git_utils import _git_last_commit_for_path, load_docs_index
from .linters import (
    discover_python_linter_findings,
    discover_test_coverage_findings,
    discover_typescript_type_findings,
    discover_xo_linter_findings,
)
from .models import Finding, now_iso, parse_iso, stable_finding_id
from .reforge import RefactorClass, RefactorWork, can_auto_refactor, classify_finding
from .refactor_queue import enqueue_refactor_work
from .state import _append_text, save_refactor_work
from .utils import command_list_to_shell


def _build_base_cycle_command(args: argparse.Namespace) -> List[str]:
    cmd = [
        'python3',
        str(RUNNER_PATH),
        '--repo-path',
        str(args.repo_path),
        '--state-file',
        str(args.state_file),
        '--log-file',
        str(args.log_file),
        '--findings-file',
        str(args.findings_file),
        '--issues-file',
        str(args.issues_file),
        '--worktree-root',
        str(args.worktree_root),
        '--open-issues-cap',
        str(args.open_issues_cap),
        '--open-prs-cap',
        str(args.open_prs_cap),
        '--issue-confidence-threshold',
        str(args.issue_confidence_threshold),
        '--max-files-changed',
        str(args.max_files_changed),
        '--max-loc-diff',
        str(args.max_loc_diff),
        '--max-prs-per-run',
        str(args.max_prs_per_run),
        '--max-issues-per-run',
        str(args.max_issues_per_run),
        '--finding-cooldown-seconds',
        str(args.finding_cooldown_seconds),
        '--merge-cooldown-minutes',
        str(args.merge_cooldown_minutes),
        '--max-fix-attempts-per-issue',
        str(args.max_fix_attempts_per_issue),
        '--docs-index-file',
        str(args.docs_index_file),
        '--fix-engine',
        str(args.fix_engine),
        '--claude-cmd-template',
        str(args.claude_cmd_template),
    ]
    if getattr(args, 'refresh_docs_index', False):
        cmd.append('--refresh-docs-index')
    if getattr(args, 'live_github_actions', False):
        cmd.append('--live-github-actions')
    if getattr(args, 'auto_merge_sandbox', False):
        cmd.append('--auto-merge-sandbox')
    return cmd


def build_active_cycle_command(args: argparse.Namespace) -> str:
    cmd = _build_base_cycle_command(args) + ['--run-phase', str(args.run_phase), '--no-dry-run']
    return command_list_to_shell(cmd)


def build_issue_cycle_command(args: argparse.Namespace) -> str:
    cmd = _build_base_cycle_command(args) + ['--run-phase', 'issue-cycle', '--no-dry-run']
    return command_list_to_shell(cmd)


def build_pr_cycle_command(args: argparse.Namespace) -> str:
    cmd = _build_base_cycle_command(args) + ['--run-phase', 'pr-cycle', '--no-dry-run']
    return command_list_to_shell(cmd)


def build_merge_cycle_command(args: argparse.Namespace) -> str:
    cmd = _build_base_cycle_command(args) + ['--run-phase', 'merge-cycle', '--no-dry-run', '--auto-merge-sandbox']
    return command_list_to_shell(cmd)


def build_orchestrated_cycle_command(args: argparse.Namespace) -> str:
    cmd = _build_base_cycle_command(args) + ['--run-phase', 'orchestrated', '--no-dry-run', '--auto-merge-sandbox']
    return command_list_to_shell(cmd)


def build_refactor_cycle_command(args: argparse.Namespace) -> str:
    cmd = _build_base_cycle_command(args) + ['--run-phase', 'refactor-cycle', '--no-dry-run']
    if getattr(args, 'max_queue_items', None) is not None:
        cmd.extend(['--max-queue-items', str(args.max_queue_items)])
    if getattr(args, 'auto_approve', False):
        cmd.append('--auto-approve')
    return command_list_to_shell(cmd)


def build_reconcile_only_command(args: argparse.Namespace) -> str:
    cmd = [
        'python3',
        str(RUNNER_PATH),
        '--reconcile-only',
        '--repo-path',
        str(args.repo_path),
        '--state-file',
        str(args.state_file),
        '--log-file',
        str(args.log_file),
        '--findings-file',
        str(args.findings_file),
        '--issues-file',
        str(args.issues_file),
        '--worktree-root',
        str(args.worktree_root),
    ]
    if getattr(args, 'live_github_actions', False):
        cmd.append('--live-github-actions')
    return command_list_to_shell(cmd)


def build_docs_index_refresh_command(args: argparse.Namespace) -> str:
    cmd = [
        'python3',
        str(RUNNER_PATH),
        '--run-phase',
        'docs-index',
        '--repo-path',
        str(args.repo_path),
        '--log-file',
        str(args.log_file),
        '--docs-index-file',
        str(args.docs_index_file),
        '--refresh-docs-index',
    ]
    return command_list_to_shell(cmd)


def build_verification_only_command(args: argparse.Namespace) -> str:
    cmd = [
        'python3',
        str(RUNNER_PATH),
        '--run-phase',
        'verify-only',
        '--repo-path',
        str(args.repo_path),
        '--state-file',
        str(args.state_file),
        '--log-file',
        str(args.log_file),
        '--findings-file',
        str(args.findings_file),
        '--issues-file',
        str(args.issues_file),
        '--worktree-root',
        str(args.worktree_root),
    ]
    if getattr(args, 'live_github_actions', False):
        cmd.append('--live-github-actions')
    return command_list_to_shell(cmd)


def discover_findings(
    repo_path: Path,
    log_file: Path = DEFAULT_LOG,
    docs_index_file: Path = DEFAULT_DOCS_INDEX,
) -> List[Finding]:
    """
    Discover findings for the repository.
    
    For ky repo, skip internal discovery since files don't exist.
    Use external discovery via qa-ky-discover script instead.
    """
    # Skip internal discovery for ky repos
    if '--skip-internal-discovery' in os.environ:
        _append_text(log_file, 'discover_findings: skipping internal discovery for ky repo - using external discovery')
        return []
    
    findings: List[Finding] = []

    def _read_lines(relative_path: str) -> List[str]:
        target = repo_path / relative_path
        if not target.exists():
            return []
        return target.read_text(encoding='utf-8').splitlines()

    def _add_finding(
        relative_path: str,
        line_number: int,
        rule: str,
        snippet: str,
        confidence: float,
        quick_win: bool = False,
        safe_to_autofix: bool = False,
    ) -> None:
        findings.append(
            Finding(
                finding_id=stable_finding_id(str(repo_path), relative_path, line_number, rule, snippet),
                repo=str(repo_path),
                path=relative_path,
                line=line_number,
                rule=rule,
                snippet=snippet,
                confidence=confidence,
                quick_win=quick_win,
                safe_to_autofix=safe_to_autofix,
            )
        )

    rule_meta = {entry['rule']: entry for entry in DETECTOR_CATALOG}

    # Bug detectors
    for idx, line in enumerate(_read_lines('price.py'), start=1):
        if 'return amount + discount' in line:
            _add_finding(
                'price.py',
                idx,
                'discount-math-sign',
                line.strip(),
                rule_meta['discount-math-sign']['confidence'],
                quick_win=True,
                safe_to_autofix=True,
            )

    for idx, line in enumerate(_read_lines('src/qa_sandbox/catalog.py'), start=1):
        if 'if item == query' in line:
            _add_finding(
                'src/qa_sandbox/catalog.py',
                idx,
                'catalog-query-not-normalized',
                line.strip(),
                rule_meta['catalog-query-not-normalized']['confidence'],
                quick_win=True,
                safe_to_autofix=True,
            )

    orders_lines = _read_lines('src/qa_sandbox/orders.py')
    for idx, line in enumerate(orders_lines, start=1):
        if 'int(order.subtotal * order.tax_rate)' in line:
            _add_finding(
                'src/qa_sandbox/orders.py',
                idx,
                'orders-tax-truncation',
                line.strip(),
                rule_meta['orders-tax-truncation']['confidence'],
            )
        if 'except Exception' in line:
            _add_finding(
                'src/qa_sandbox/orders.py',
                idx,
                'broad-except',
                line.strip(),
                rule_meta['broad-except']['confidence'],
            )

    for idx, line in enumerate(_read_lines('src/qa_sandbox/notifications.py'), start=1):
        if 'return value.lower()' in line:
            _add_finding(
                'src/qa_sandbox/notifications.py',
                idx,
                'notifications-email-no-trim',
                line.strip(),
                rule_meta['notifications-email-no-trim']['confidence'],
                quick_win=True,
                safe_to_autofix=True,
            )

    # Check for missing type guard in notifications module
    notifications_lines = _read_lines('src/qa_sandbox/notifications.py')
    has_type_guard = any('isinstance' in line and 'str' in line for line in notifications_lines)
    if notifications_lines and not has_type_guard:
        for idx, line in enumerate(notifications_lines, start=1):
            if 'def normalize_email' in line:
                _add_finding(
                    'src/qa_sandbox/notifications.py',
                    idx,
                    'notifications-type-guard-missing',
                    'missing isinstance(value, str) type guard',
                    rule_meta['notifications-type-guard-missing']['confidence'],
                    quick_win=True,
                    safe_to_autofix=True,
                )
                break

    inventory_lines = _read_lines('src/qa_sandbox/inventory.py')
    if inventory_lines and not any('quantity <= 0' in line for line in inventory_lines):
        for idx, line in enumerate(inventory_lines, start=1):
            if 'stock[sku] < quantity' in line:
                _add_finding(
                    'src/qa_sandbox/inventory.py',
                    idx,
                    'inventory-invalid-quantity',
                    'missing quantity <= 0 guard',
                    rule_meta['inventory-invalid-quantity']['confidence'],
                    quick_win=True,
                    safe_to_autofix=True,
                )
                break
    for idx, line in enumerate(inventory_lines, start=1):
        if 'pop(0)' in line:
            _add_finding(
                'src/qa_sandbox/inventory.py',
                idx,
                'perf-pop-front-loop',
                line.strip(),
                rule_meta['perf-pop-front-loop']['confidence'],
            )

    for idx, line in enumerate(_read_lines('src/qa_sandbox/analytics.py'), start=1):
        if 'not in seen' in line:
            _add_finding(
                'src/qa_sandbox/analytics.py',
                idx,
                'perf-list-membership-loop',
                line.strip(),
                rule_meta['perf-list-membership-loop']['confidence'],
            )

    # Debt/TODO detectors
    todo_targets = [
        'src/qa_sandbox/catalog.py',
        'src/qa_sandbox/orders.py',
        'src/qa_sandbox/analytics.py',
        'scripts/report_health.py',
    ]
    for rel_path in todo_targets:
        for idx, line in enumerate(_read_lines(rel_path), start=1):
            stripped = line.strip()
            if stripped.startswith('# TODO:') or stripped.startswith('# FIXME:'):
                _add_finding(
                    rel_path,
                    idx,
                    'debt-todo-marker',
                    stripped,
                    rule_meta['debt-todo-marker']['confidence'],
                )

    # Lint/style detector
    for idx, line in enumerate(_read_lines('scripts/report_health.py'), start=1):
        if 'Path("/tmp/' in line or "Path('/tmp/" in line:
            _add_finding(
                'scripts/report_health.py',
                idx,
                'hardcoded-tmp-path',
                line.strip(),
                rule_meta['hardcoded-tmp-path']['confidence'],
                quick_win=True,
                safe_to_autofix=True,
            )

    # Trailing whitespace detector for Python source files
    py_files = [
        'src/qa_sandbox/catalog.py',
        'src/qa_sandbox/orders.py',
        'src/qa_sandbox/notifications.py',
        'src/qa_sandbox/inventory.py',
        'src/qa_sandbox/analytics.py',
        'scripts/report_health.py',
        'price.py',
    ]
    for rel_path in py_files:
        for idx, line in enumerate(_read_lines(rel_path), start=1):
            # Check for trailing whitespace (space or tab at end of non-empty line)
            if line and line != line.rstrip():
                _add_finding(
                    rel_path,
                    idx,
                    'trailing-whitespace',
                    f"line ends with whitespace: '{line[-10:]}'",
                    rule_meta['trailing-whitespace']['confidence'],
                    quick_win=True,
                    safe_to_autofix=True,
                )

    # Docs detectors
    for rel_path in ['docs/ARCHITECTURE.md', 'docs/TROUBLESHOOTING.md']:
        for idx, line in enumerate(_read_lines(rel_path), start=1):
            if 'legacy_pricer.py' in line:
                _add_finding(
                    rel_path,
                    idx,
                    'docs-legacy-reference',
                    line.strip(),
                    rule_meta['docs-legacy-reference']['confidence'],
                    quick_win=True,
                    safe_to_autofix=True,
                )

    operations_lines = _read_lines('docs/OPERATIONS.md')
    operations_text = '\n'.join(operations_lines)
    has_rollback_section = '## Rollback' in operations_text or '## rollback' in operations_text.lower()
    has_revert_instruction = 'git revert' in operations_text.lower() or 'revert the' in operations_text.lower()
    if operations_lines and not (has_rollback_section and has_revert_instruction):
        _add_finding(
            'docs/OPERATIONS.md',
            1,
            'docs-missing-rollback',
            'missing rollback runbook section with git revert instructions',
            rule_meta['docs-missing-rollback']['confidence'],
            quick_win=True,
            safe_to_autofix=True,
        )

    readme_lines = _read_lines('README.md')
    readme_text = '\n'.join(readme_lines)
    if 'pytest -q' in readme_text and 'pip install pytest' not in readme_text and 'uv pip install pytest' not in readme_text:
        _add_finding(
            'README.md',
            1,
            'docs-quickstart-gap',
            'pytest command present without setup/install note',
            rule_meta['docs-quickstart-gap']['confidence'],
            quick_win=True,
            safe_to_autofix=True,
        )

    # Docs index-backed gap/drift detectors
    docs_index_entries = load_docs_index(docs_index_file)
    for entry in docs_index_entries:
        code_path = str(entry.get('code_path') or '').strip()
        if not code_path:
            continue
        target = repo_path / code_path
        if not target.exists() or not target.is_file():
            continue

        coverage_status = str(entry.get('coverage_status') or '').strip().lower()
        if coverage_status == 'uncovered':
            _add_finding(
                code_path,
                1,
                'doc-gap-uncovered-module',
                'missing inline and external docs coverage (from docs index)',
                rule_meta['doc-gap-uncovered-module']['confidence'],
            )

        has_external = bool(entry.get('has_external_doc_ref', False))
        current_sha = _git_last_commit_for_path(repo_path, code_path)
        indexed_sha = str(entry.get('last_seen_sha') or '').strip()
        index_updated = parse_iso(str(entry.get('last_updated') or ''))
        try:
            file_mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
        except Exception:
            file_mtime = None
        changed_since_index = bool(index_updated and file_mtime and file_mtime > index_updated)
        sha_changed = bool(indexed_sha and current_sha and indexed_sha != current_sha)

        if has_external and (sha_changed or changed_since_index):
            _add_finding(
                code_path,
                1,
                'doc-drift-stale-reference',
                'external docs may be stale after code changes (from docs index)',
                rule_meta['doc-drift-stale-reference']['confidence'],
            )

    # Test-gap detectors
    notifications_test = repo_path / 'tests' / 'test_notifications.py'
    if not notifications_test.exists():
        _add_finding(
            'tests/test_notifications.py',
            1,
            'test-gap-missing-file',
            'missing notification tests for invalid input and trimming behavior',
            rule_meta['test-gap-missing-file']['confidence'],
            quick_win=True,
            safe_to_autofix=True,
        )

    orders_test_lines = _read_lines('tests/test_orders.py')
    if orders_test_lines and not any('invalid' in line.lower() and 'coupon' in line.lower() for line in orders_test_lines):
        _add_finding(
            'tests/test_orders.py',
            1,
            'test-gap-missing-case',
            'missing invalid coupon behavior test',
            rule_meta['test-gap-missing-case']['confidence'],
            quick_win=True,
            safe_to_autofix=True,
        )

    inventory_test_lines = _read_lines('tests/test_inventory.py')
    if inventory_test_lines and not any('negative' in line.lower() for line in inventory_test_lines):
        _add_finding(
            'tests/test_inventory.py',
            1,
            'test-gap-missing-case',
            'missing negative quantity test for reserve_stock',
            rule_meta['test-gap-missing-case']['confidence'],
            quick_win=True,
            safe_to_autofix=True,
        )

    # Also run type safety discovery for TypeScript repos
    type_findings = discover_typescript_type_findings(repo_path, log_file)
    if type_findings:
        findings.extend(type_findings)
        _append_text(log_file, f'type-discovery: added {len(type_findings)} type safety findings')
    
    # Also run test coverage discovery
    coverage_findings = discover_test_coverage_findings(repo_path, log_file)
    if coverage_findings:
        findings.extend(coverage_findings)
        _append_text(log_file, f'coverage-discovery: added {len(coverage_findings)} test coverage findings')
    
    # Run xo linter discovery for TypeScript/JavaScript repos
    xo_findings = discover_xo_linter_findings(repo_path, log_file)
    if xo_findings:
        findings.extend(xo_findings)
        _append_text(log_file, f'xo-discovery: added {len(xo_findings)} xo linter findings')
    
    # Run Python linter discovery for Python repos
    python_findings = discover_python_linter_findings(repo_path, log_file)
    if python_findings:
        findings.extend(python_findings)
        _append_text(log_file, f'python-discovery: added {len(python_findings)} ruff findings')
    
    return findings


def create_issues_for_findings(
    issues_data: Dict[str, Any],
    findings: List[Finding],
    confidence_threshold: float,
    max_issues_per_run: int,
) -> List[Dict[str, Any]]:
    existing = {str(x.get('finding_id')) for x in issues_data.get('issues', []) if x.get('finding_id')}
    created: List[Dict[str, Any]] = []

    for finding in findings:
        if len(created) >= max_issues_per_run:
            break
        if finding.confidence < confidence_threshold:
            continue
        if finding.finding_id in existing:
            continue

        issue_id = f"QA-{len(issues_data['issues']) + len(created) + 1:04d}"
        issue = {
            'issue_id': issue_id,
            'finding_id': finding.finding_id,
            'repo': finding.repo,
            'path': finding.path,
            'line': finding.line,
            'rule': finding.rule,
            'snippet': finding.snippet,
            'confidence': finding.confidence,
            'quick_win': finding.quick_win,
            'safe_to_autofix': finding.safe_to_autofix,
            'status': 'open',
            'created_at': now_iso(),
            'updated_at': now_iso(),
            'source': 'sandbox_local_runner_v2',
            'history': [{'at': now_iso(), 'event': 'open'}],
        }
        created.append(issue)

    issues_data['issues'].extend(created)
    return created


def choose_safe_autofix_items(findings: List[Finding], confidence_threshold: float) -> List[Finding]:
    return [
        f
        for f in findings
        if f.safe_to_autofix and f.confidence >= confidence_threshold
    ]


def route_findings_with_intent(
    findings: List[Finding],
    confidence_threshold: float,
    findings_file: Optional[Path] = None,
    worktree_path: Optional[Path] = None,
    log_file: Optional[Path] = None,
) -> Dict[str, List[Any]]:
    """Route findings into intentional execution lanes.

    Buckets:
    - autofix_safe: deterministic low-risk fixes
    - refactor_queue: structural refactor findings, with queue metadata when queued
    - human_review: non-autofix findings needing manual or later LLM handling
    - skipped: below confidence threshold
    """
    routed: Dict[str, List[Any]] = {
        'autofix_safe': [],
        'refactor_queue': [],
        'human_review': [],
        'skipped': [],
    }

    for finding in findings:
        if finding.confidence < confidence_threshold:
            routed['skipped'].append(finding)
            continue

        rc = classify_finding(finding)
        if rc == RefactorClass.SIMPLE_FIX and finding.safe_to_autofix:
            routed['autofix_safe'].append(finding)
            continue

        if rc == RefactorClass.REFACTOR_CLASS:
            refactor_work = RefactorWork(finding_id=finding.finding_id)
            finding.refactor_phase = refactor_work.phase.value
            queued_work_id: Optional[str] = None
            route_reason = 'planning'

            if worktree_path is not None:
                allowed, reason = can_auto_refactor(finding, worktree_path)
                if not allowed:
                    refactor_work.mark_aborted(reason)
                    finding.refactor_phase = refactor_work.phase.value
                    route_reason = reason
                    entry = enqueue_refactor_work(finding, refactor_work)
                    queued_work_id = entry.work_id

            if findings_file is not None:
                save_refactor_work(finding.finding_id, findings_file, refactor_work)

            if log_file is not None:
                _append_text(
                    log_file,
                    'route-findings: '
                    f'finding_id={finding.finding_id} rule={finding.rule} '
                    f'class={rc.value} phase={refactor_work.phase.value} '
                    f'queued_work_id={queued_work_id or ""} reason={route_reason}',
                )

            routed['refactor_queue'].append({
                'finding': finding,
                'refactor_work': refactor_work,
                'queued_work_id': queued_work_id,
                'reason': route_reason,
            })
            continue

        routed['human_review'].append(finding)

    return routed


def find_issue_for_finding(issues_data: Dict[str, Any], finding_id: str) -> Optional[Dict[str, Any]]:
    for issue in issues_data.get('issues', []):
        if str(issue.get('finding_id')) == str(finding_id):
            return issue
    return None


def append_issue_history(issue: Dict[str, Any], event: str, detail: Optional[str] = None) -> None:
    history = issue.setdefault('history', [])
    payload: Dict[str, Any] = {'at': now_iso(), 'event': event}
    if detail:
        payload['detail'] = detail
    history.append(payload)


def set_issue_status(issue: Dict[str, Any], status: str, detail: Optional[str] = None) -> None:
    issue['status'] = status
    issue['updated_at'] = now_iso()
    if detail:
        issue['status_detail'] = detail
    append_issue_history(issue, status, detail)


def count_failed_fix_attempts(issue: Dict[str, Any]) -> int:
    """Count the number of failed fix verification attempts from issue history.
    
    A failed attempt is any event of type:
    - fix_failed_verification
    - needs-human-* (any needs-human escalation)
    
    Count only failures since the most recent reopen (`open`) event so operators can
    intentionally reset an issue after changing remediation policy or validation rules.
    """
    count = 0
    history = issue.get('history', [])
    failed_events = {
        'fix_failed_verification',
        'needs-human-validation-failed',
        'needs-human-scope-limit-exceeded',
        'needs-human-commit-failed',
        'needs-human-push-failed',
        'needs-human-max-retries-exceeded',
    }
    last_open_index = 0
    for idx, entry in enumerate(history):
        if str(entry.get('event', '')).lower() == 'open':
            last_open_index = idx

    for entry in history[last_open_index + 1:]:
        event = str(entry.get('event', '')).lower()
        if event in failed_events or event.startswith('needs-human'):
            count += 1
    return count


def ensure_issue_for_finding(
    issues_data: Dict[str, Any],
    finding: Finding,
    confidence_threshold: float,
) -> Optional[Dict[str, Any]]:
    existing = find_issue_for_finding(issues_data, finding.finding_id)
    if existing:
        return existing
    if finding.confidence < confidence_threshold:
        return None

    issue_id = f"QA-{len(issues_data['issues']) + 1:04d}"
    issue = {
        'issue_id': issue_id,
        'finding_id': finding.finding_id,
        'repo': finding.repo,
        'path': finding.path,
        'line': finding.line,
        'rule': finding.rule,
        'snippet': finding.snippet,
        'confidence': finding.confidence,
        'quick_win': finding.quick_win,
        'safe_to_autofix': finding.safe_to_autofix,
        'status': 'open',
        'created_at': now_iso(),
        'updated_at': now_iso(),
        'source': 'sandbox_local_runner_v2',
        'history': [{'at': now_iso(), 'event': 'open'}],
    }
    issues_data['issues'].append(issue)
    return issue
