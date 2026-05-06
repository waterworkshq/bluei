"""cli.py — main() entry point + status artifact."""
import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

from .models import now_iso, age_seconds, Finding, parse_iso
from .utils import sanitize_command_template, run_capture, run_no_capture, branch_suffix, append_lesson, assert_safe_repo, is_path_tracked
from .state import (
    load_state, save_state, load_issues, save_issues, _append_text,
    guard_open_issues, guard_open_prs, record_reconciliation_event,
    reconcile_open_workload, mark_finding_activity, filter_findings_by_cooldown,
    append_findings, load_finding_record, update_finding_record, increment_fix_attempt,
    count_actionable_issues, NON_ACTIONABLE_ISSUE_STATUSES,
)
from .gh import (
    get_origin_url, parse_github_repo, repo_is_sandbox,
    find_existing_github_issue, find_existing_github_pr,
    gh_issue_comment, gh_issue_close, gh_pr_comment,
    finding_from_issue_record, fetch_open_prs_for_merge,
    evaluate_pr_check_health, evaluate_pr_reviews, evaluate_pr_mergeability,
    merge_failure_requires_pr_fix, merge_pr,
    create_or_update_github_issue, create_or_update_github_pr,
    fetch_github_live_counts,
)
from .rebase_sweep import sweep_rebase
from .orchestrator import (
    build_active_cycle_command, build_issue_cycle_command, build_pr_cycle_command,
    build_merge_cycle_command, build_orchestrated_cycle_command, build_refactor_cycle_command, build_reconcile_only_command,
    build_docs_index_refresh_command, build_verification_only_command, discover_findings,
    create_issues_for_findings, choose_safe_autofix_items, route_findings_with_intent,
    find_issue_for_finding, ensure_issue_for_finding, append_issue_history, set_issue_status,
    count_failed_fix_attempts,
)
from .lifecycle import (
    verify_fix_closed, apply_autofix, git_commit_all, git_push_branch,
    run_named_checks, build_target_checks, apply_claude_fix, run_validation_gate,
    choose_validation_baseline,
    classify_review_feedback, review_loop_allowed, diff_stats,
    process_refactor_queue,
)
from .prompts import (
    render_test_coverage_prompt, render_type_safety_prompt,
    render_complexity_refactor_prompt, render_maxlines_refactor_prompt,
    render_claude_fix_prompt,
)
from .mnemo_client import is_mnemo_available
from .reforge import RefactorClass, classify_finding
from .git_utils import get_branch, refresh_docs_index, load_docs_index
from .constants import (
    DEFAULT_STATE, DEFAULT_LOG, DEFAULT_FINDINGS, DEFAULT_STATUS,
    DEFAULT_REPO, DETECTOR_CATALOG, WORKSPACE, AGENT_ROOT,
    DEFAULT_ISSUES, DEFAULT_WORKTREE_ROOT, DEFAULT_DOCS_INDEX,
    DEFAULT_LESSONS_LOG, BASELINE_VALIDATION_CHECKS, RULE_TARGET_CHECKS,
    CLAUDE_REQUIRED_RULES, BLOCKED_REPOS, DEFAULT_FIX_ENGINE,
    DEFAULT_CLAUDE_CMD_TEMPLATE, QA_FIX_PROMPT_FILENAME,
    DEFAULT_FINDING_COOLDOWN_SECONDS, DEFAULT_STALENESS_THRESHOLD_SECONDS,
    DEFAULT_BATCH_RULES_PATH, DEFAULT_BATCH_STATE,
    load_llm_fixable_rules,
)

logger = logging.getLogger(__name__)

# Loaded once at module level, cached by the loader
_LLM_FIXABLE_RULES: Optional[Dict[str, Dict[str, Any]]] = None


def _build_refactor_queue_snapshot() -> Dict[str, Any]:
    """Return lightweight refactor queue counts for status/reporting."""
    try:
        rq_mod = __import__(f"{__package__}.refactor_queue", fromlist=["RefactorQueue"])
        queue = rq_mod.RefactorQueue()
        counts = queue.count_by_status()
    except Exception:
        counts = {}

    snapshot = {
        'pending_review': int(counts.get('pending_review', 0)),
        'approved': int(counts.get('approved', 0)),
        'executing': int(counts.get('executing', 0)),
        'completed': int(counts.get('completed', 0)),
        'aborted': int(counts.get('aborted', 0)),
    }
    snapshot['total'] = sum(snapshot.values())
    return snapshot


def _triage_pr_back_to_fix_cycle(
    *,
    issue: Dict[str, Any],
    pr_number: int,
    pr_url: str,
    branch: str,
    reason: str,
    log_file: Path,
) -> None:
    issue_github = issue.setdefault('github', {})
    issue_github['pr_number'] = pr_number
    if pr_url:
        issue_github['pr_url'] = pr_url
    if branch:
        issue_github['branch'] = branch
    set_issue_status(issue, 'pr_merge_conflict', reason)
    _append_text(
        log_file,
        f'triage: pr=#{pr_number} returned to pr-cycle reason={reason}',
    )


def _load_review_state(review_state_file: Path) -> Dict[str, Any]:
    if not review_state_file.exists():
        return {}
    try:
        payload = json.loads(review_state_file.read_text())
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _autonomous_review_gate_passes(review_state: Dict[str, Any], pr_number: int) -> Tuple[bool, str]:
    prs = review_state.get('prs') if isinstance(review_state.get('prs'), dict) else {}
    pr_state = prs.get(str(pr_number)) if isinstance(prs, dict) else None
    if not isinstance(pr_state, dict):
        return False, 'review-state-missing'

    if str(pr_state.get('last_action') or '') != 'merge_ready':
        return False, 'review-state-not-merge-ready'

    snapshot = pr_state.get('last_snapshot') if isinstance(pr_state.get('last_snapshot'), dict) else {}
    if str(snapshot.get('merge_state_status') or '').upper() not in {'CLEAN', 'UNKNOWN', 'UNSTABLE'}:
        return False, 'review-state-not-cautiously-mergeable'
    if int(snapshot.get('actionable_comment_count') or 0) != 0:
        return False, 'review-state-has-actionable-comments'
    if list(snapshot.get('active_change_requesters') or []):
        return False, 'review-state-has-change-requesters'
    if not pr_state.get('last_review_comment_key'):
        return False, 'review-artifact-missing'

    return True, 'review-artifact-merge-ready'


def _get_llm_fixable_rules() -> Dict[str, Dict[str, Any]]:
    global _LLM_FIXABLE_RULES
    if _LLM_FIXABLE_RULES is None:
        _LLM_FIXABLE_RULES = load_llm_fixable_rules()
    return _LLM_FIXABLE_RULES


def _load_batch_rules_for_args(args: argparse.Namespace) -> List[Any]:
    """Load batch rules from args or fall back to built-in defaults.

    Returns a list of BatchRule objects suitable for group_findings_for_batch().
    """
    from .batch_pr import load_batch_rules as _load_yaml_rules
    from .models import BatchRule

    if not getattr(args, 'batch_pr_enabled', True):
        return []

    rules_path = getattr(args, 'batch_pr_rules', None)
    if rules_path is not None and rules_path.exists():
        return _load_yaml_rules(rules_path)

    # Fall back to built-in rules file
    if DEFAULT_BATCH_RULES_PATH.exists():
        return _load_yaml_rules(DEFAULT_BATCH_RULES_PATH)

    # Hardcoded fallback rules
    return [
        BatchRule(rule_pattern="ruff-c408", enabled=True, group_by="rule",
                  max_batch_size=20, max_files_per_batch=15, max_loc_per_batch=500,
                  isolation={"file_patterns": ["**/migrations/*.py"]}, priority=1),
        BatchRule(rule_pattern="ruff-b904", enabled=True, group_by="rule",
                  max_batch_size=15, max_files_per_batch=10, max_loc_per_batch=300,
                  isolation={"file_patterns": ["**/middleware*.py"]}, priority=2),
        BatchRule(rule_pattern="ruff-b007", enabled=True, group_by="rule",
                  max_batch_size=10, priority=3),
        BatchRule(rule_pattern="ruff-s311", enabled=True, group_by="file",
                  max_batch_size=10, max_files_per_batch=5, priority=4),
    ]


def _hydrate_worktree_dependencies(repo_path: Path, worktree_path: Path, log_file: Path) -> None:
    """Best-effort link shared dependency folders into a fresh git worktree.

    JS/TS worktrees typically do not carry `node_modules` because it is untracked, but
    validation commands still expect package-local binaries to exist. Reuse the primary
    repo's dependency directory when available.
    """
    for dirname in ('node_modules',):
        source = repo_path / dirname
        target = worktree_path / dirname
        if not source.exists() or target.exists():
            continue
        try:
            os.symlink(source, target, target_is_directory=True)
            _append_text(log_file, f'worktree-deps: linked {dirname} from repo into worktree')
        except Exception as exc:
            _append_text(log_file, f'worktree-deps: failed to link {dirname}: {exc}')


def _reconcile_issue_pr_link(
    *,
    issue: Dict[str, Any],
    repo_slug: str,
    repo_path: Path,
    log_file: Path,
) -> bool:
    """Return True when the issue is still backed by an open live PR and should skip queueing.

    If the linked PR was closed or disappeared, clear the stale PR linkage and reopen the
    issue queue state so the finding can be attempted again.
    """
    issue_github = issue.get('github', {}) if isinstance(issue.get('github'), dict) else {}
    if not (issue_github.get('pr_number') or issue_github.get('pr_url')):
        return False

    finding_id = str(issue.get('finding_id') or '')
    if not finding_id:
        return True

    existing_pr = find_existing_github_pr(repo_slug, finding_id, cwd=repo_path)
    if existing_pr and str(existing_pr.get('state') or '').upper() == 'OPEN':
        issue_github['pr_number'] = int(existing_pr['number'])
        issue_github['pr_url'] = str(existing_pr.get('url') or issue_github.get('pr_url') or '')
        issue_github['branch'] = str(existing_pr.get('headRefName') or issue_github.get('branch') or '')
        issue['github'] = issue_github
        return issue.get('status') != 'pr_merge_conflict'

    stale_pr_number = issue_github.pop('pr_number', None)
    stale_pr_url = issue_github.pop('pr_url', None)
    issue_github.pop('branch', None)
    issue['github'] = issue_github

    stale_ref = stale_pr_url or stale_pr_number or 'unknown'
    if issue.get('status') in {'pr_opened', 'pr_merge_conflict'}:
        set_issue_status(issue, 'open', f'linked PR no longer open; returned to queue ({stale_ref})')
    else:
        append_issue_history(issue, 'pr_link_cleared', f'linked PR no longer open ({stale_ref})')
    _append_text(log_file, f'pr-link-reset: issue={issue.get("issue_id") or issue.get("id")} stale_pr={stale_ref}')
    return False


def update_status_artifact(
    status_file: Path,
    state: Dict[str, Any],
    issues_file: Path,
    findings_file: Path,
    args: argparse.Namespace,
    run_mode: str,
    reconcile_event: Dict[str, Any],
    previous_last_run_at: Optional[str] = None,
    run_metrics: Optional[Dict[str, Any]] = None,
) -> None:
    if status_file.exists():
        try:
            status = json.loads(status_file.read_text())
            if not isinstance(status, dict):
                status = {}
        except Exception:
            status = {}
    else:
        status = {}

    generated_at = now_iso()
    last_run_before_update = status.get('last_run_at') or previous_last_run_at
    previous_age_seconds = age_seconds(last_run_before_update)
    threshold_seconds = int(args.staleness_threshold_seconds)

    findings_entries = 0
    if findings_file.exists():
        with findings_file.open('r', encoding='utf-8') as f:
            findings_entries = sum(1 for line in f if line.strip())

    issues_data = load_issues(issues_file)
    issues_list = issues_data.get('issues', [])
    status_counter = Counter(i.get('status', 'unknown') for i in issues_list)
    actionable_issues = sum(1 for i in issues_list if i.get('status', 'open') not in NON_ACTIONABLE_ISSUE_STATUSES)
    raw_open_issues = sum(1 for i in issues_list if i.get('status', 'open') == 'open')
    refactor_queue = _build_refactor_queue_snapshot()
    status['generated_at'] = generated_at
    status['last_run_at'] = generated_at
    status['run_mode'] = run_mode
    status['fix_configuration'] = {
        'fix_engine': args.fix_engine,
        'claude_cmd_template': sanitize_command_template(args.claude_cmd_template),
    }
    # Read live PR count from active_prs.json (source of truth) instead of
    # stale state cache — same pattern as the open_issues fix.
    active_prs_file = status_file.parent / 'active_prs.json'
    live_open_prs = 0
    if active_prs_file.exists():
        try:
            active_prs_data = json.loads(active_prs_file.read_text())
            live_open_prs = len(active_prs_data.get('prs', {}))
        except Exception:
            live_open_prs = int(state.get('open_prs', 0))
    else:
        live_open_prs = int(state.get('open_prs', 0))

    status['current_counts'] = {
        'open_issues': raw_open_issues,
        'actionable_issues': actionable_issues,
        'open_prs': live_open_prs,
        'created_records_total': len(state.get('created', [])),
        'issue_records_total': len(issues_list),
        'findings_entries': findings_entries,
        'refactor_queue_total': refactor_queue['total'],
        'by_status': dict(status_counter),
    }
    status['refactor_queue'] = refactor_queue
    status['last_reconciliation'] = reconcile_event
    status['staleness'] = {
        'threshold_seconds': threshold_seconds,
        'age_seconds': 0,
        'is_stale': False,
        'stale_after': (datetime.now(timezone.utc) + timedelta(seconds=threshold_seconds)).isoformat(),
        'previous_last_run_at': last_run_before_update,
        'previous_age_seconds': previous_age_seconds,
        'was_stale_before_run': previous_age_seconds is not None and previous_age_seconds > threshold_seconds,
    }
    status['manual_one_cycle_command'] = build_active_cycle_command(args)
    status['issue_cycle_command'] = build_issue_cycle_command(args)
    status['pr_cycle_command'] = build_pr_cycle_command(args)
    status['merge_cycle_command'] = build_merge_cycle_command(args)
    status['orchestrated_cycle_command'] = build_orchestrated_cycle_command(args)
    status['refactor_cycle_command'] = build_refactor_cycle_command(args)
    status['reconcile_only_command'] = build_reconcile_only_command(args)
    status['verification_only_command'] = build_verification_only_command(args)
    status['docs_index_refresh_command'] = build_docs_index_refresh_command(args)
    status['detector_catalog'] = DETECTOR_CATALOG
    latest_run_metrics = dict(run_metrics or {
        'findings_detected': 0,
        'findings_written': 0,
        'issues_created': 0,
        'fix_attempts': 0,
        'prs_created': 0,
        'fixes_verified': 0,
        'fixes_failed_verification': 0,
        'unresolved_open': 0,
        'findings_suppressed_by_cooldown': 0,
        'issues_escalated_max_retries': 0,
        'merge_attempts': 0,
        'merges_succeeded': 0,
        'merges_failed': 0,
        'merged_pr_urls': [],
        'blocked_events': 0,
        'blocked_reasons': [],
    })
    latest_run_metrics['refactor_queue_total'] = refactor_queue['total']
    latest_run_metrics['refactor_queue_pending_review'] = refactor_queue['pending_review']
    latest_run_metrics['refactor_queue_approved'] = refactor_queue['approved']
    latest_run_metrics['refactor_queue_executing'] = refactor_queue['executing']
    latest_run_metrics['refactor_queue_completed'] = refactor_queue['completed']
    latest_run_metrics['refactor_queue_aborted'] = refactor_queue['aborted']
    status['latest_run_metrics'] = latest_run_metrics

    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(status, indent=2, sort_keys=True) + '\n')


def main() -> int:
    p = argparse.ArgumentParser(description='SAFE local sandbox QA workflow runner (v2 hardening)')
    p.add_argument('--repo-path', default=str(DEFAULT_REPO))
    p.add_argument('--state-file', default=str(DEFAULT_STATE))
    p.add_argument('--log-file', default=str(DEFAULT_LOG))
    p.add_argument('--findings-file', default=str(DEFAULT_FINDINGS))
    p.add_argument('--issues-file', default=str(DEFAULT_ISSUES))
    p.add_argument('--worktree-root', default=str(DEFAULT_WORKTREE_ROOT))
    p.add_argument('--status-file', default=str(DEFAULT_STATUS))
    p.add_argument('--docs-index-file', default=str(DEFAULT_DOCS_INDEX))
    p.add_argument('--reconcile-only', action='store_true', default=False)
    p.add_argument(
        '--run-phase',
        choices=['issue-cycle', 'pr-cycle', 'merge-cycle', 'refactor-cycle', 'orchestrated', 'verify-only', 'detect-only', 'e2e', 'docs-index'],
        default='orchestrated',
    )

    # Safety defaults
    p.add_argument('--dry-run', dest='dry_run', action='store_true', default=True)
    p.add_argument('--no-dry-run', dest='dry_run', action='store_false')
    p.add_argument('--live-github-actions', action='store_true', default=False)
    p.add_argument('--max-prs-per-run', type=int, default=2)
    p.add_argument('--allow-main-commit', action='store_true', default=False)
    p.add_argument('--force-push', action='store_true', default=False)

    # Discovery/creation policy
    p.add_argument('--max-issues-per-run', type=int, default=10)
    p.add_argument('--refresh-docs-index', action='store_true', default=False)
    p.add_argument('--issue-confidence-threshold', type=float, default=0.7)
    p.add_argument('--open-issues-cap', type=int, default=20)
    p.add_argument('--open-prs-cap', type=int, default=5)
    p.add_argument('--simulate-open-issues', type=int)
    p.add_argument('--simulate-open-prs', type=int)
    p.add_argument('--finding-cooldown-seconds', type=int, default=DEFAULT_FINDING_COOLDOWN_SECONDS)
    p.add_argument('--migrate-context', action='store_true', default=False,
                   help='Reclassify findings with context rules and migrate issue state')
    p.add_argument('--staleness-threshold-seconds', type=int, default=DEFAULT_STALENESS_THRESHOLD_SECONDS)
    p.add_argument('--auto-merge-sandbox', action='store_true', default=False)
    p.add_argument('--merge-cooldown-minutes', type=int, default=30)
    p.add_argument('--auto-rebase-enabled', action='store_true', default=False,
                   help='Enable post-merge rebase sweep across sibling PRs')
    p.add_argument('--rebase-max-prs', type=int, default=5,
                   help='Max sibling PRs to rebase in one sweep')
    p.add_argument('--max-queue-items', type=int, default=None,
                    help='Maximum number of refactor queue items to process per run (default: all approved)')
    p.add_argument('--auto-approve', action='store_true', default=False,
                    help='Auto-approve pending_review items before processing (use with refactor-cycle)')
    p.add_argument('--max-fix-attempts-per-issue', type=int, default=3,
                    help='Maximum autofix verification attempts per issue before escalating to human (default: 3)')
    p.add_argument('--fix-engine', choices=['deterministic', 'claude'], default=DEFAULT_FIX_ENGINE)
    p.add_argument('--claude-cmd-template', default=DEFAULT_CLAUDE_CMD_TEMPLATE)

    # Fix scope policy
    p.add_argument('--max-files-changed', type=int, default=5)
    p.add_argument('--max-loc-diff', type=int, default=200)

    # Validation policy
    p.add_argument('--allow-unchanged-baseline-failures', dest='allow_unchanged_baseline_failures', action='store_true', default=True)
    p.add_argument('--no-allow-unchanged-baseline-failures', dest='allow_unchanged_baseline_failures', action='store_false')

    # Baseline checks (per-repo validation commands, JSON list of lists)
    p.add_argument('--baseline-checks', default='[]',
                   help='JSON list of baseline check command lists, e.g. \'[["npm","test"],["npm","run","build"]]\'')

    # Review loop scaffolding
    p.add_argument('--pr-author', default='qa-bot')
    p.add_argument('--bot-author', default='qa-bot')
    p.add_argument('--pr-tags', default='')
    p.add_argument('--explicit-tag', default='qa-autofix-ok')
    p.add_argument('--review-feedback', default='')
    p.add_argument('--log-lesson', dest='log_lesson', default='', help='Manual lesson entry to append to LESSONS_LOG.md')
    p.add_argument('--lessons-file', default=str(DEFAULT_LESSONS_LOG))

    # Batch PR engine (Phase 1)
    p.add_argument('--batch-pr-enabled', action='store_true', default=True,
                   help='Enable batch PR grouping for related findings')
    p.add_argument('--no-batch-pr', dest='batch_pr_enabled', action='store_false',
                   help='Disable batch PR grouping')
    p.add_argument('--batch-pr-rules', type=Path, default=None,
                   help='Path to batch_rules.yaml (default: built-in rules)')
    p.add_argument('--batch-state-file', default=str(DEFAULT_BATCH_STATE),
                   help='Path to batch state JSONL file (default: state/batches.jsonl)')
    p.add_argument('--no-batch-pr-split-on-failure', dest='batch_pr_split_on_failure',
                   action='store_false', default=True,
                   help='Do not split batches on fix failures')

    args = p.parse_args()

    # Ensure a basic handler is configured so logger.info(...) prints to stderr
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format='%(message)s')

    # Parse --baseline-checks JSON into named-dict format for run_named_checks / prompt rendering
    # Format: [["npm","test"],["npm","run","build"]] → {'baseline-0': ['npm','test'], 'baseline-1': ['npm','run','build']}
    _parsed_baseline_checks: Dict[str, List[str]] = {}
    try:
        raw: List[List[str]] = json.loads(args.baseline_checks)
        for idx, cmd in enumerate(raw):
            if cmd:
                _parsed_baseline_checks[f'baseline-{idx}'] = cmd
    except (json.JSONDecodeError, TypeError):
        _parsed_baseline_checks = {}
    # Fall back to hardcoded qa-sandbox checks if nothing provided
    PER_REPO_BASELINE_CHECKS: Dict[str, List[str]] = (
        _parsed_baseline_checks if _parsed_baseline_checks else dict(BASELINE_VALIDATION_CHECKS)
    )

    if args.run_phase == 'detect-only':
        args.run_phase = 'issue-cycle'
    elif args.run_phase == 'e2e':
        args.run_phase = 'orchestrated'

    repo_path = Path(args.repo_path)

    # Phase 4: log mnemo availability
    mnemo_ok = is_mnemo_available(repo_path)
    if mnemo_ok:
        logger.info("mnemo available — recall and seeding enabled for this repo")
    else:
        logger.info("mnemo unavailable — falling back to local reranker")

    state_file = Path(args.state_file)
    log_file = Path(args.log_file)
    findings_file = Path(args.findings_file)
    issues_file = Path(args.issues_file)
    worktree_root = Path(args.worktree_root)
    status_file = Path(args.status_file)
    docs_index_file = Path(args.docs_index_file)
    lessons_file = Path(args.lessons_file)
    review_state_file = issues_file.with_name('review_state.json')

    # Handle manual lesson entry
    if args.log_lesson:
        append_lesson(
            lessons_file=lessons_file,
            cycle_type='manual',
            what_broke=args.log_lesson if 'broke' in args.log_lesson.lower() else '',
            what_changed=args.log_lesson if 'changed' in args.log_lesson.lower() or 'change' in args.log_lesson.lower() else '',
            what_worked=args.log_lesson if 'worked' in args.log_lesson.lower() else '',
        )
        # If only logging a lesson, exit
        if args.reconcile_only or args.run_phase == 'docs-index':
            print(f'[DONE] Logged manual lesson to {lessons_file}')
            return 0

    try:
        assert_safe_repo(repo_path)
    except Exception as e:
        print(f'[ABORT] {e}')
        _append_text(log_file, f'abort: {e}')
        return 2

    if args.fix_engine == 'claude':
        if repo_path.name in BLOCKED_REPOS:
            print(f'[ABORT] claude fix mode is blocked for repo: {repo_path.name}')
            _append_text(log_file, f'abort: claude fix mode blocked for repo={repo_path.name}')
            return 2
        if sys.executable is None or True:
            pass  # Keep going; the actual check happens at fix time

    if args.refresh_docs_index or args.run_phase == 'docs-index':
        docs_entries = refresh_docs_index(repo_path, docs_index_file, log_file)
        if args.run_phase == 'docs-index':
            print(f'[DONE] DOCS-INDEX entries={len(docs_entries)} file={docs_index_file}')
            return 0

    # Refactor-cycle: process approved refactor-queue items
    if args.run_phase == 'refactor-cycle':
        _append_text(log_file, f'refactor-cycle: start worktree={worktree_root} dry_run={args.dry_run}')
        result = process_refactor_queue(
            worktree_path=worktree_root,
            repo_path=repo_path,
            dry_run=args.dry_run,
            max_items=args.max_queue_items,
            auto_approve=args.auto_approve,
        )
        processed = result.get('processed', [])
        approved = result.get('approved', [])
        pending = result.get('pending', [])
        failed = result.get('failed', [])
        print(
            f'[DONE] refactor-cycle processed={len(processed)} '
            f'auto_approved={len(approved)} pending={len(pending)} failed={len(failed)}'
        )
        _append_text(
            log_file,
            f'refactor-cycle: done processed={processed} approved={approved} '
            f'pending={pending} failed={failed}',
        )
        return 0

    if args.force_push:
        print('[ABORT] force push is disabled in safety mode')
        _append_text(log_file, 'abort: force push disabled')
        return 2

    if args.max_prs_per_run < 1 or args.max_prs_per_run > 2:
        print('[ABORT] max-prs-per-run is hard-locked to 1-2 for sandbox safety')
        _append_text(log_file, f'abort: max-prs-per-run must be 1 or 2, got {args.max_prs_per_run}')
        return 2

    if args.open_issues_cap < 10 or args.open_issues_cap > 50:
        print('[ABORT] open-issues-cap must stay within 10-50 for sandbox safety')
        _append_text(log_file, f'abort: invalid open-issues-cap={args.open_issues_cap} (allowed 10-50)')
        return 2

    if args.open_prs_cap < 1 or args.open_prs_cap > 10:
        print('[ABORT] open-prs-cap must stay within 1-10 for sandbox safety')
        _append_text(log_file, f'abort: invalid open-prs-cap={args.open_prs_cap} (allowed 1-10)')
        return 2

    if args.merge_cooldown_minutes < 0:
        print('[ABORT] merge-cooldown-minutes must be >= 0')
        _append_text(log_file, 'abort: invalid merge-cooldown-minutes')
        return 2

    if args.finding_cooldown_seconds < 0:
        print('[ABORT] finding-cooldown-seconds must be >= 0')
        _append_text(log_file, 'abort: invalid finding-cooldown-seconds')
        return 2

    if args.staleness_threshold_seconds < 1:
        print('[ABORT] staleness-threshold-seconds must be positive')
        _append_text(log_file, 'abort: invalid staleness-threshold-seconds')
        return 2

    if args.max_fix_attempts_per_issue < 1:
        print('[ABORT] max-fix-attempts-per-issue must be >= 1')
        _append_text(log_file, 'abort: invalid max-fix-attempts-per-issue')
        return 2

    origin_url = get_origin_url(repo_path)
    gh_owner, gh_name = parse_github_repo(origin_url)
    gh_repo_slug = f'{gh_owner}/{gh_name}' if gh_owner and gh_name else ''
    if args.live_github_actions and not gh_repo_slug:
        print('[ABORT] live mode requires a GitHub origin remote')
        _append_text(log_file, 'abort: live mode requested on non-GitHub repo')
        return 2

    if args.auto_merge_sandbox and not repo_is_sandbox(gh_repo_slug):
        print('[ABORT] --auto-merge-sandbox is restricted to qa-sandbox-repo only')
        _append_text(log_file, f'abort: auto-merge not allowed for repo={gh_repo_slug or "unknown"}')
        return 2

    if not args.reconcile_only and args.run_phase != 'merge-cycle':
        # Review loop policy scaffolding
        pr_tags = [x.strip() for x in args.pr_tags.split(',') if x.strip()]
        review_ok, review_reason = review_loop_allowed(args.pr_author, pr_tags, args.bot_author, args.explicit_tag)
        _append_text(log_file, review_reason)
        if not review_ok:
            print(f'[NEEDS-HUMAN] {review_reason}')
            return 4

        feedback_class = classify_review_feedback(args.review_feedback)
        if feedback_class == 'needs-human':
            _append_text(log_file, 'review-feedback classified as conceptual -> needs-human')
            print('[NEEDS-HUMAN] conceptual review feedback requires human intervention')
            return 4

    state = load_state(state_file)
    previous_last_run_at = state.get('last_run_at')
    open_issues, open_prs, reconcile_event = reconcile_open_workload(
        repo_path=repo_path,
        state=state,
        log_file=log_file,
        simulate_open_issues=args.simulate_open_issues,
        simulate_open_prs=args.simulate_open_prs,
    )
    state['last_run_at'] = now_iso()

    if args.reconcile_only:
        save_state(state_file, state)
        update_status_artifact(
            status_file=status_file,
            state=state,
            issues_file=issues_file,
            findings_file=findings_file,
            args=args,
            run_mode='RECONCILE-ONLY',
            reconcile_event=reconcile_event,
            previous_last_run_at=previous_last_run_at,
            run_metrics={
                'findings_detected': 0,
                'findings_written': 0,
                'issues_created': 0,
                'fix_attempts': 0,
                'prs_created': 0,
                'fixes_verified': 0,
                'fixes_failed_verification': 0,
                'unresolved_open': int(state.get('open_issues', 0)),
                'findings_suppressed_by_cooldown': 0,
                'issues_escalated_max_retries': 0,
                'merge_attempts': 0,
                'merges_succeeded': 0,
                'merges_failed': 0,
                'merged_pr_urls': [],
                'blocked_events': 0,
                'blocked_reasons': [],
            },
        )
        print(
            f"[DONE] RECONCILE-ONLY source={reconcile_event['reason']} "
            f"open_issues={open_issues} open_prs={open_prs}"
        )
        _append_text(log_file, f'done: mode=RECONCILE-ONLY open_issues={open_issues} open_prs={open_prs}')
        return 0

    blocked_reasons: List[str] = []
    fix_attempts = 0
    fixes_verified = 0
    fixes_failed_verification = 0
    issues_escalated_max_retries = 0
    created_prs = 0
    merge_attempts = 0
    merges_succeeded = 0
    merges_failed = 0
    merged_pr_urls: List[str] = []

    findings: List[Finding] = []
    written_findings = 0
    eligible_findings: List[Finding] = []
    suppressed_findings: List[Finding] = []
    refactor_routed_items: List[Dict[str, Any]] = []

    issues_data = load_issues(issues_file)
    created_issues: List[Dict[str, Any]] = []

    run_issue_cycle = args.run_phase in ('issue-cycle', 'orchestrated')
    run_pr_cycle = args.run_phase in ('pr-cycle', 'orchestrated')
    run_merge_cycle = args.run_phase in ('merge-cycle', 'orchestrated')

    if (args.run_phase in ('verify-only',) or run_issue_cycle) and not docs_index_file.exists():
        refresh_docs_index(repo_path, docs_index_file, log_file)

    if args.run_phase in ('verify-only',) or run_issue_cycle:
        # Early cap guard — skip discovery entirely if issue cap is already saturated.
        # Saves the cost of scanning thousands of findings for zero output.
        if run_issue_cycle:
            pre_discovery_actionable = count_actionable_issues(issues_data)
            cap_ok, cap_reason = guard_open_issues(pre_discovery_actionable, args.open_issues_cap)
            _append_text(log_file, f'pre-discovery-cap-check: {cap_reason}')
            if not cap_ok:
                blocked_reasons.append(cap_reason)
        if args.run_phase in ('verify-only',) or (run_issue_cycle and cap_ok):
            findings = discover_findings(repo_path, log_file=log_file, docs_index_file=docs_index_file)
        else:
            findings = []
            eligible_findings = []
        written_findings = append_findings(findings_file, findings)
        _append_text(log_file, f'discovery: findings_detected={len(findings)} findings_written={written_findings}')
        eligible_findings, suppressed_findings = filter_findings_by_cooldown(
            findings=findings,
            state=state,
            cooldown_seconds=args.finding_cooldown_seconds,
            log_file=log_file,
        )
        if suppressed_findings:
            _append_text(log_file, f'cooldown-summary: suppressed_findings={len(suppressed_findings)}')

        # Filter to tracked paths only when live GitHub actions are enabled AND
        # we are in the PR cycle (where we must be able to modify the file).
        # During the ISSUE CYCLE, untracked/missing-file findings are still valid
        # issue subjects — e.g. "test-gap-missing-file" for a tests/ file that
        # doesn't yet exist has an inherently untracked path, but is worth reporting.
        if args.live_github_actions and run_pr_cycle:
            tracked_findings: List[Finding] = []
            untracked_count = 0
            for finding in eligible_findings:
                if is_path_tracked(repo_path, finding.path):
                    tracked_findings.append(finding)
                else:
                    untracked_count += 1
                    _append_text(log_file, f'discovery-skip: untracked path for live queue path={finding.path} rule={finding.rule}')
            eligible_findings = tracked_findings
            if untracked_count:
                _append_text(log_file, f'discovery-skip-summary: untracked_findings={untracked_count}')

        refactor_findings: List[Finding] = []
        remaining_findings: List[Finding] = []
        for finding in eligible_findings:
            if classify_finding(finding) == RefactorClass.REFACTOR_CLASS:
                refactor_findings.append(finding)
            else:
                remaining_findings.append(finding)
        if refactor_findings:
            routed = route_findings_with_intent(
                refactor_findings,
                confidence_threshold=args.issue_confidence_threshold,
                findings_file=findings_file,
                worktree_path=repo_path,
                log_file=log_file,
            )
            refactor_routed_items = list(routed.get('refactor_queue', []))
            _append_text(
                log_file,
                f'refactor-routing-summary: routed={len(refactor_routed_items)} skipped={len(routed.get("skipped", []))}',
            )
        eligible_findings = remaining_findings

    # --- Contextual Fix Migration ---
    if args.migrate_context:
        from .migrate_context import reclassify_findings, dry_run_report
        findings_path = Path(args.findings_file)
        if args.dry_run:
            report = dry_run_report(findings_path)
            _append_text(log_file, 'migrate-context (dry run):\n' + report)
            print(report)
        else:
            changes = reclassify_findings(findings_path)
            _append_text(log_file, f'migrate-context: {len(changes)} findings reclassified')
            for fid, info in changes.items():
                _append_text(log_file, f'  {fid}: {info["old_class"]} -> {info["new_class"]} (rule={info["rule"]})')
            print(f'Reclassified {len(changes)} findings.')

    if run_issue_cycle:
        # Count only actionable issues (excluding blocked/escalated/resolved)
        # This prevents non-actionable issues from stalling the pipeline
        actionable_issue_count = count_actionable_issues(issues_data)
        _append_text(log_file, f'actionable-issues: raw_open={open_issues} actionable={actionable_issue_count}')

        for routed_item in refactor_routed_items:
            finding = routed_item['finding']
            existing_issue = find_issue_for_finding(issues_data, finding.finding_id)
            issue = ensure_issue_for_finding(
                issues_data=issues_data,
                finding=finding,
                confidence_threshold=args.issue_confidence_threshold,
            )
            if issue is None:
                continue
            if existing_issue is None:
                created_issues.append(issue)
                open_issues += 1
            refactor_meta = issue.setdefault('refactor', {})
            refactor_meta['phase'] = routed_item['refactor_work'].phase.value
            if routed_item.get('queued_work_id'):
                refactor_meta['queue_work_id'] = routed_item['queued_work_id']
            refactor_meta['review_reason'] = routed_item.get('reason', 'planning')
            set_issue_status(issue, 'needs-human-refactor-review', routed_item.get('reason', 'planning'))

        actionable_issue_count = count_actionable_issues(issues_data)

        for _ in range(args.max_issues_per_run):
            ok, reason = guard_open_issues(actionable_issue_count, args.open_issues_cap)
            _append_text(log_file, reason)
            if not ok:
                blocked_reasons.append(reason)
                break

            batch = create_issues_for_findings(
                issues_data=issues_data,
                findings=eligible_findings,
                confidence_threshold=args.issue_confidence_threshold,
                max_issues_per_run=1,
            )
            if not batch:
                break

            for issue in batch:
                if args.live_github_actions:
                    issue_finding = finding_from_issue_record(issue)
                    if issue_finding is not None:
                        gh_issue = create_or_update_github_issue(
                            repo_slug=gh_repo_slug,
                            finding=issue_finding,
                            dry_run=args.dry_run,
                            log_file=log_file,
                            cwd=repo_path,
                        )
                        issue_github = issue.setdefault('github', {})
                        if gh_issue.get('number') is not None:
                            issue_github['issue_number'] = gh_issue.get('number')
                        if gh_issue.get('url'):
                            issue_github['issue_url'] = gh_issue.get('url')

                created_issues.append(issue)
                open_issues += 1

    if args.run_phase == 'verify-only':
        active_keys = {(f.rule, f.path) for f in findings}
        for issue in issues_data.get('issues', []):
            key = (str(issue.get('rule', '')), str(issue.get('path', '')))
            if key in active_keys:
                set_issue_status(issue, 'fix_failed_verification', 'detector still firing in verification-only cycle')
                fixes_failed_verification += 1
            else:
                set_issue_status(issue, 'resolved_verified', 'detector no longer firing in verification-only cycle')
                fixes_verified += 1
        save_issues(issues_file, issues_data)

        unresolved_open = len(
            [x for x in issues_data.get('issues', []) if x.get('status') not in ('resolved_verified', 'resolved_merged')]
        )
        state['open_issues'] = open_issues
        state['open_prs'] = open_prs
        state['last_run_at'] = now_iso()
        save_state(state_file, state)
        update_status_artifact(
            status_file=status_file,
            state=state,
            issues_file=issues_file,
            findings_file=findings_file,
            args=args,
            run_mode='VERIFY-ONLY',
            reconcile_event=reconcile_event,
            previous_last_run_at=previous_last_run_at,
            run_metrics={
                'findings_detected': len(findings),
                'findings_written': written_findings,
                'issues_created': 0,
                'fix_attempts': 0,
                'prs_created': 0,
                'fixes_verified': fixes_verified,
                'fixes_failed_verification': fixes_failed_verification,
                'unresolved_open': unresolved_open,
                'findings_suppressed_by_cooldown': len(suppressed_findings),
                'issues_escalated_max_retries': 0,
                'merge_attempts': 0,
                'merges_succeeded': 0,
                'merges_failed': 0,
                'merged_pr_urls': [],
                'blocked_events': len(blocked_reasons),
                'blocked_reasons': blocked_reasons,
            },
        )
        print(
            f'[DONE] VERIFY-ONLY findings={len(findings)} fixes_verified={fixes_verified} '
            f'fixes_failed_verification={fixes_failed_verification}'
        )
        return 0

    if run_pr_cycle:
        queue_candidates: List[Tuple[Dict[str, Any], Finding]] = []
        for issue in issues_data.get('issues', []):
            if issue.get('status') in ('resolved_merged',):
                continue
            issue_github = issue.get('github', {}) if isinstance(issue.get('github'), dict) else {}
            if issue_github.get('pr_number') or issue_github.get('pr_url'):
                if not args.live_github_actions:
                    continue
                if _reconcile_issue_pr_link(
                    issue=issue,
                    repo_slug=gh_repo_slug,
                    repo_path=repo_path,
                    log_file=log_file,
                ):
                    continue
            finding = finding_from_issue_record(issue)
            if finding is None:
                continue
            if issue.get('status') in NON_ACTIONABLE_ISSUE_STATUSES:
                continue

            if args.live_github_actions and not is_path_tracked(repo_path, finding.path):
                set_issue_status(issue, 'blocked_untracked_path', f'path not tracked in git HEAD: {finding.path}')
                continue
            finding_class = classify_finding(finding)
            if finding_class == RefactorClass.REFACTOR_CLASS:
                routed = route_findings_with_intent(
                    [finding],
                    confidence_threshold=args.issue_confidence_threshold,
                    findings_file=findings_file,
                    worktree_path=repo_path,
                    log_file=log_file,
                )
                refactor_item = (routed.get('refactor_queue') or [{}])[0]
                refactor_meta = issue.setdefault('refactor', {})
                if refactor_item.get('queued_work_id'):
                    refactor_meta['queue_work_id'] = refactor_item['queued_work_id']
                if refactor_item.get('refactor_work') is not None:
                    refactor_meta['phase'] = refactor_item['refactor_work'].phase.value
                refactor_meta['review_reason'] = refactor_item.get('reason', 'planning')
                if issue.get('status') != 'needs-human-refactor-review':
                    set_issue_status(
                        issue,
                        'needs-human-refactor-review',
                        refactor_item.get('reason', 'planning'),
                    )
                _append_text(
                    log_file,
                    f'pr-cycle: routed structural refactor issue={issue.get("issue_id")} finding_id={finding.finding_id} to refactor review lane',
                )
                continue

            if not finding.safe_to_autofix:
                llm_rules = _get_llm_fixable_rules()
                if finding.rule in llm_rules:
                    # Rule is LLM-fixable — route to fix engine, don't skip
                    pass
                elif classify_finding(finding) == RefactorClass.CONTEXTUAL_FIX:
                    # Contextual fix engine can handle this — route to fix engine
                    pass
                else:
                    # Truly not fixable — mark for human triage
                    if issue.get('status') != 'needs-human-not-fixable':
                        set_issue_status(
                            issue,
                            'needs-human-not-fixable',
                            f'rule {finding.rule} is not autofixable and not LLM-fixable'
                        )
                        _append_text(
                            log_file,
                            f'skip: issue={issue.get("issue_id")} rule={finding.rule} '
                            f'not autofixable and not in LLM_FIXABLE_RULES'
                        )
                    continue
            if finding.confidence < args.issue_confidence_threshold:
                continue

            # P0 Fix #1: Check if issue has exceeded max fix attempts
            failed_attempts = count_failed_fix_attempts(issue)
            if failed_attempts >= args.max_fix_attempts_per_issue:
                if issue.get('status') != 'needs-human-max-retries-exceeded':
                    set_issue_status(
                        issue,
                        'needs-human-max-retries-exceeded',
                        f'exceeded max fix attempts ({failed_attempts}/{args.max_fix_attempts_per_issue})'
                    )
                    issues_escalated_max_retries += 1
                    _append_text(
                        log_file,
                        f'escalation: issue={issue.get("issue_id")} finding_id={finding.finding_id} '
                        f'exceeded max_fix_attempts_per_issue ({failed_attempts}/{args.max_fix_attempts_per_issue}) '
                        f'-> marking as needs-human-max-retries-exceeded'
                    )
                continue

            queue_candidates.append((issue, finding))

        if not queue_candidates:
            _append_text(log_file, 'pr-cycle: no eligible issue-queue items for autofix')

        baseline_results: Dict[str, Dict[str, Any]] = {}
        if queue_candidates:
            baseline_results = run_named_checks(
                repo_path=repo_path,
                checks=PER_REPO_BASELINE_CHECKS,
                log_file=log_file,
                phase='baseline-main',
            )
            baseline_failures = [name for name, result in baseline_results.items() if int(result.get('rc', 1)) != 0]
            if baseline_failures:
                _append_text(log_file, f'baseline-main: failing_checks={",".join(baseline_failures)}')
            else:
                _append_text(log_file, 'baseline-main: all checks passing')

        current_branch = get_branch(repo_path)
        if current_branch == 'main' and not args.allow_main_commit:
            _append_text(log_file, 'safety: main branch direct commit blocked; using isolated worktree branch')

        # Build the iteration list: batch mode groups findings, non-batch iterates directly
        if getattr(args, 'batch_pr_enabled', False) and queue_candidates:
            from .batch_pr import group_findings_for_batch, process_batch as _process_batch
            from .state import save_batch_record as _save_batch_record

            _batch_rules = _load_batch_rules_for_args(args)
            _batch_groups = group_findings_for_batch(queue_candidates, _batch_rules)
            _append_text(
                log_file,
                f'batch-cycle: {len(_batch_groups)} batch groups from {len(queue_candidates)} candidates',
            )

            # Pre-process multi-finding batches, collect solo items for single-finding path
            _solo_items = []  # list of (issue, finding)
            for _bg in _batch_groups:
                if _bg.is_solo:
                    _solo_items.append((_bg.issues[0], _bg.findings[0]))
                else:
                    # Process multi-finding batch inline
                    if created_prs >= args.max_prs_per_run:
                        break
                    ok, reason = guard_open_prs(open_prs, args.open_prs_cap)
                    _append_text(log_file, reason)
                    if not ok:
                        blocked_reasons.append(reason)
                        break
                    _success, _detail = _process_batch(
                        batch=_bg, repo_path=repo_path, args=args, log_file=log_file,
                    )
                    if _success:
                        created_prs += 1
                        open_prs += 1
                        _bsf = Path(getattr(args, 'batch_state_file', str(DEFAULT_BATCH_STATE)))
                        _save_batch_record(_bsf, _bg.to_record())
                        _append_text(log_file, f'batch-cycle: {_bg.batch_id} -> {_detail}')
                    else:
                        _append_text(log_file, f'batch-cycle: {_bg.batch_id} failed: {_detail}')

            # Solo items use the same single-finding path as non-batch mode
            _iteration_items = _solo_items
        else:
            _iteration_items = queue_candidates

        # ── Single-finding loop (shared by batch-solo and non-batch paths) ──
        for idx, (issue, finding) in enumerate(_iteration_items, start=1):
            if created_prs >= args.max_prs_per_run:
                break

            ok, reason = guard_open_prs(open_prs, args.open_prs_cap)
            _append_text(log_file, reason)
            if not ok:
                blocked_reasons.append(reason)
                break

            issue_github = issue.setdefault('github', {})
            issue_number: Optional[int] = issue_github.get('issue_number')
            issue_url: str = str(issue_github.get('issue_url') or '')

            if args.live_github_actions and issue_number is None:
                gh_issue = create_or_update_github_issue(
                    repo_slug=gh_repo_slug,
                    finding=finding,
                    dry_run=args.dry_run,
                    log_file=log_file,
                    cwd=repo_path,
                )
                issue_number = gh_issue.get('number') if gh_issue.get('number') is not None else issue_number
                issue_url = str(gh_issue.get('url') or issue_url)
                if issue_number is not None:
                    issue_github['issue_number'] = issue_number
                if issue_url:
                    issue_github['issue_url'] = issue_url

            existing_pr_for_repair: Optional[Dict[str, Any]] = None
            if args.live_github_actions:
                existing_pr = find_existing_github_pr(gh_repo_slug, finding.finding_id, cwd=repo_path)
                if existing_pr and str(existing_pr.get('state') or '').upper() == 'OPEN':
                    pr_number = int(existing_pr['number'])
                    pr_url = str(existing_pr.get('url') or '')
                    issue_github['pr_number'] = pr_number
                    issue_github['pr_url'] = pr_url
                    issue_github['branch'] = str(existing_pr.get('headRefName') or issue_github.get('branch') or '')
                    if issue.get('status') == 'pr_merge_conflict':
                        existing_pr_for_repair = existing_pr
                        _append_text(
                            log_file,
                            f'pr-cycle: resuming existing PR #{pr_number} for merge-conflict repair',
                        )
                    else:
                        if issue_number is not None and not args.dry_run:
                            gh_issue_comment(
                                gh_repo_slug,
                                issue_number,
                                f'Existing PR already open for this finding: {pr_url}',
                                cwd=repo_path,
                            )
                        set_issue_status(issue, 'pr_opened', 'existing live PR already present for finding')
                        mark_finding_activity(state=state, finding_ids=[finding.finding_id], action='pr-open-existing')
                        continue
                elif existing_pr:
                    _append_text(
                        log_file,
                        f'pr-cycle: ignoring closed linked PR #{existing_pr.get("number")} for finding={finding.finding_id}',
                    )

            ts = datetime.now().strftime('%Y%m%d%H%M%S')
            finding_suffix = finding.finding_id[:8]
            if args.live_github_actions:
                worktree_branch = str(
                    (existing_pr_for_repair or {}).get('headRefName')
                    or issue_github.get('branch')
                    or f"qa/live-{branch_suffix(finding.rule)}-{finding_suffix}"
                )
            else:
                worktree_branch = f'qa/sandbox-v2-{ts}-{idx}'
            # Use an absolute, finding-specific path so stale interrupted runs are less likely to collide.
            worktree_path = worktree_root.resolve() / f'qa-sandbox-v2-{ts}-{idx}-{finding_suffix}'

            # Best-effort cleanup of stale worktree metadata/path before creating the next isolated worktree.
            run_no_capture(['git', 'worktree', 'prune'], cwd=repo_path)
            if worktree_path.exists():
                run_no_capture(['rm', '-rf', str(worktree_path)], cwd=repo_path)

            add_rc, add_out = run_capture(
                ['git', 'worktree', 'add', '-B', worktree_branch, str(worktree_path)],
                cwd=repo_path,
            )
            if add_rc != 0:
                blocked_reasons.append('failed-to-create-worktree')
                _append_text(log_file, f'error: failed to create worktree output={(add_out or "<empty>")[:300]}')
                break

            _hydrate_worktree_dependencies(repo_path=repo_path, worktree_path=worktree_path, log_file=log_file)

            run_status = 'unknown'
            try:
                fix_attempts += 1
                set_issue_status(issue, 'fix_attempted', 'starting sandbox autofix attempt')

                worktree_baseline_results = run_named_checks(
                    repo_path=worktree_path,
                    checks=PER_REPO_BASELINE_CHECKS,
                    log_file=log_file,
                    phase='worktree-baseline',
                )

                target_checks = build_target_checks(finding)

                # Determine fix strategy
                llm_rules = _get_llm_fixable_rules()
                is_llm_fixable = (
                    not finding.safe_to_autofix and
                    finding.rule in llm_rules
                )
                use_claude_engine = (
                    args.fix_engine == 'claude' or
                    finding.rule in CLAUDE_REQUIRED_RULES or
                    is_llm_fixable
                )
                # Store prompt hint for LLM-fixable rules
                extra_prompt = llm_rules.get(finding.rule, {}).get('prompt_hint') if is_llm_fixable else None

                if use_claude_engine:
                    rc, claude_output, prompt_file = apply_claude_fix(
                        worktree_path=worktree_path,
                        finding=finding,
                        baseline_checks=BASELINE_VALIDATION_CHECKS,
                        target_checks=target_checks,
                        claude_cmd_template=args.claude_cmd_template,
                        max_files_changed=args.max_files_changed,
                        max_loc_diff=args.max_loc_diff,
                        log_file=log_file,
                        findings_file=findings_file,    # NEW
                        lessons_file=lessons_file,      # NEW
                        repo_path=repo_path,            # NEW (Phase 0 mnemo integration)
                        extra_prompt=extra_prompt,
                    )
                    if rc != 0:
                        run_status = 'fix-failed-verification:claude-command-failed'
                        set_issue_status(issue, 'fix_failed_verification', run_status)
                        fixes_failed_verification += 1
                        if args.live_github_actions and issue_number is not None and not args.dry_run:
                            gh_issue_comment(
                                gh_repo_slug,
                                issue_number,
                                (
                                    'Claude autofix command failed '
                                    f'(rc={rc}) for {finding.rule} in {finding.path}:{finding.line}. '
                                    f'Prompt: {prompt_file}. Output: {(claude_output or "<empty>")[:300]}'
                                ),
                                cwd=repo_path,
                            )
                        current_entry = state.get('finding_activity', {}).get(finding.finding_id, {})
                        current_failures = current_entry.get('failure_count', 0)
                        mark_finding_activity(
                            state=state,
                            finding_ids=[finding.finding_id],
                            action='fix-failed-verification',
                            failure_count=current_failures + 1,
                            last_error=f'claude rc={rc}',
                        )
                        continue
                else:
                    applied = apply_autofix(worktree_path, finding, log_file)
                    if not applied:
                        # Try contextual fix engine before giving up
                        from .context_fix import apply_contextual_fix
                        _append_text(log_file, f'contextual-fix: attempting for rule={finding.rule} path={finding.path}')
                        applied = apply_contextual_fix(
                            repo_path=repo_path,
                            finding=finding,
                            log_file=log_file,
                            worktree_path=worktree_path,
                        )
                    if not applied:
                        run_status = 'fix-noop'
                        set_issue_status(issue, 'fix_failed_verification', 'autofix could not modify target pattern')
                        fixes_failed_verification += 1
                        # Record failure tracking for deterministic fixes too (Phase 2)
                        if finding.finding_id:
                            increment_fix_attempt(
                                finding.finding_id,
                                findings_file,
                                f'autofix no-op for rule={finding.rule}',
                            )
                        if args.live_github_actions and issue_number is not None and not args.dry_run:
                            gh_issue_comment(
                                gh_repo_slug,
                                issue_number,
                                f'Autofix could not update pattern for {finding.rule} in {finding.path}:{finding.line}.',
                                cwd=repo_path,
                            )
                        current_entry = state.get('finding_activity', {}).get(finding.finding_id, {})
                        current_failures = current_entry.get('failure_count', 0)
                        mark_finding_activity(
                            state=state,
                            finding_ids=[finding.finding_id],
                            action='fix-failed-verification',
                            failure_count=current_failures + 1,
                            last_error=f'autofix no-op rule={finding.rule}',
                        )
                        continue

                files_changed, loc_diff = diff_stats(worktree_path)
                _append_text(log_file, f'fix-scope-stats: files_changed={files_changed} loc_diff={loc_diff}')

                if files_changed == 0 and loc_diff == 0:
                    verified_without_changes = verify_fix_closed(
                        worktree_path,
                        finding,
                        log_file,
                        docs_index_file=docs_index_file,
                    )
                    if verified_without_changes:
                        set_issue_status(issue, 'resolved_verified', 'finding already closed on branch; no code change needed')
                        fixes_verified += 1
                        if args.live_github_actions and issue_number is not None and not args.dry_run:
                            gh_issue_comment(
                                gh_repo_slug,
                                issue_number,
                                'Finding no longer reproduces on the current branch. Closing without a new PR.',
                                cwd=repo_path,
                            )
                            gh_issue_close(
                                gh_repo_slug,
                                issue_number,
                                'Resolved by existing branch state; no additional change required.',
                                cwd=repo_path,
                            )
                        mark_finding_activity(
                            state=state,
                            finding_ids=[finding.finding_id],
                            action='resolved-noop-verified',
                        )
                        continue

                if files_changed > args.max_files_changed or loc_diff > args.max_loc_diff:
                    run_status = 'needs-human-scope-limit-exceeded'
                    blocked_reasons.append(run_status)
                    set_issue_status(issue, 'fix_failed_verification', run_status)
                    fixes_failed_verification += 1
                    if args.live_github_actions and issue_number is not None and not args.dry_run:
                        gh_issue_comment(
                            gh_repo_slug,
                            issue_number,
                            f'Fix exceeded scope limits (files={files_changed}, loc={loc_diff}); needs human follow-up.',
                            cwd=repo_path,
                        )
                    break

                post_fix_results = run_named_checks(
                    repo_path=worktree_path,
                    checks=PER_REPO_BASELINE_CHECKS,
                    log_file=log_file,
                    phase='post-fix',
                )
                target_results = run_named_checks(
                    repo_path=worktree_path,
                    checks=target_checks,
                    log_file=log_file,
                    phase='target-check',
                ) if target_checks else {}

                checks_ok, validation_reason = run_validation_gate(
                    baseline_results=choose_validation_baseline(
                        repo_baseline_results=baseline_results,
                        worktree_baseline_results=worktree_baseline_results,
                        log_file=log_file,
                    ),
                    post_fix_results=post_fix_results,
                    target_results=target_results,
                    allow_unchanged_baseline_failures=args.allow_unchanged_baseline_failures,
                    log_file=log_file,
                )
                if not checks_ok:
                    run_status = f'needs-human-validation-failed:{validation_reason}'
                    blocked_reasons.append(run_status)
                    set_issue_status(issue, 'fix_failed_verification', run_status)
                    fixes_failed_verification += 1
                    if args.live_github_actions and issue_number is not None and not args.dry_run:
                        gh_issue_comment(
                            gh_repo_slug,
                            issue_number,
                            f'Validation gate failed after autofix ({validation_reason}); keeping issue open for manual intervention.',
                            cwd=repo_path,
                        )
                    break

                verified = verify_fix_closed(worktree_path, finding, log_file, docs_index_file=docs_index_file)
                if not verified:
                    run_status = 'fix-failed-verification'
                    set_issue_status(issue, 'fix_failed_verification', 'detector still firing after fix + validation')
                    fixes_failed_verification += 1
                    if args.live_github_actions and issue_number is not None and not args.dry_run:
                        gh_issue_comment(
                            gh_repo_slug,
                            issue_number,
                            'Post-fix verification failed: detector still firing.',
                            cwd=repo_path,
                        )
                    current_entry = state.get('finding_activity', {}).get(finding.finding_id, {})
                    current_failures = current_entry.get('failure_count', 0)
                    mark_finding_activity(
                        state=state,
                        finding_ids=[finding.finding_id],
                        action='fix-failed-verification',
                        failure_count=current_failures + 1,
                        last_error='detector still firing after fix',
                    )
                    continue

                set_issue_status(issue, 'resolved_verified', 'detector no longer firing after fix + validation')
                fixes_verified += 1

                pr_number: Optional[int] = None
                pr_url = ''
                if args.live_github_actions:
                    commit_message = f"fix(sandbox): {finding.rule} [{finding.finding_id[:8]}]"
                    commit_result = git_commit_all(worktree_path, commit_message, log_file=log_file, dry_run=args.dry_run)
                    if commit_result == 'no_changes':
                        run_status = 'resolved-verified-noop'
                        set_issue_status(issue, 'resolved_verified', 'detector no longer firing and no repo diff remained to commit')
                        mark_finding_activity(
                            state=state,
                            finding_ids=[finding.finding_id],
                            action='resolved-verified-noop',
                            failure_count=0,
                            last_error=None,
                        )
                        if args.live_github_actions and issue_number is not None and not args.dry_run:
                            gh_issue_comment(
                                gh_repo_slug,
                                issue_number,
                                'Post-fix verification passed and the effective fix was already present, so no new commit/PR was needed.',
                                cwd=repo_path,
                            )
                        continue
                    if commit_result != 'committed':
                        run_status = 'needs-human-commit-failed'
                        blocked_reasons.append(run_status)
                        set_issue_status(issue, 'fix_failed_verification', run_status)
                        fixes_failed_verification += 1
                        break

                    pushed = git_push_branch(worktree_path, worktree_branch, log_file=log_file, dry_run=args.dry_run)
                    if not pushed:
                        run_status = 'needs-human-push-failed'
                        blocked_reasons.append(run_status)
                        set_issue_status(issue, 'fix_failed_verification', run_status)
                        fixes_failed_verification += 1
                        break

                    pr_result = create_or_update_github_pr(
                        repo_slug=gh_repo_slug,
                        finding=finding,
                        branch=worktree_branch,
                        issue_number=issue_number,
                        dry_run=args.dry_run,
                        log_file=log_file,
                        cwd=worktree_path,
                    )
                    pr_number = pr_result.get('number') if pr_result.get('number') is not None else None
                    pr_url = str(pr_result.get('url') or '')
                    if pr_number is not None:
                        issue_github['pr_number'] = pr_number
                    if pr_url:
                        issue_github['pr_url'] = pr_url
                    issue_github['branch'] = worktree_branch

                    if issue_number is not None and not args.dry_run:
                        gh_issue_comment(
                            gh_repo_slug,
                            issue_number,
                            f'Post-fix verification passed. PR: {pr_url or "(pending URL)"}',
                            cwd=repo_path,
                        )
                    if pr_number is not None and not args.dry_run:
                        gh_pr_comment(
                            gh_repo_slug,
                            pr_number,
                            f'Automated verification passed for finding {finding.finding_id}.',
                            cwd=repo_path,
                        )
                else:
                    pr_url = ''

                if pr_number is not None or not args.live_github_actions:
                    set_issue_status(issue, 'pr_opened', 'autofix PR created from issue queue')

                entry = {
                    'type': 'pr',
                    'repo': str(repo_path),
                    'branch': worktree_branch,
                    'dry_run': args.dry_run,
                    'live_github_actions': bool(args.live_github_actions),
                    'created_at': now_iso(),
                    'linked_issue_ids': [issue.get('id') or issue['issue_id']],
                    'linked_finding_ids': [finding.finding_id],
                    'github_issue_url': issue_url,
                    'github_issue_number': issue_number,
                    'github_pr_url': pr_url,
                    'github_pr_number': pr_number,
                    'note': 'live GitHub PR workflow complete after fix+verify' if args.live_github_actions else 'simulated local PR creation after e2e fix+verification gate',
                }
                state.setdefault('created', []).append(entry)
                mark_finding_activity(
                    state=state,
                    finding_ids=[finding.finding_id],
                    action='pr-opened',
                    failure_count=0,
                    last_error=None,
                )
                open_prs += 1
                created_prs += 1
                run_status = 'pr-live-created' if args.live_github_actions else 'pr-simulated-resolved-verified'

            finally:
                run_no_capture(['git', 'worktree', 'remove', '--force', str(worktree_path)], cwd=repo_path)
                run_no_capture(['git', 'worktree', 'prune'], cwd=repo_path)
                if not args.live_github_actions:
                    run_no_capture(['git', 'branch', '-D', worktree_branch], cwd=repo_path)
                _append_text(log_file, f'cleanup: branch={worktree_branch} status={run_status}')

    if run_merge_cycle:
        if not args.auto_merge_sandbox:
            reason = 'merge-cycle-skip: auto-merge flag not enabled'
            _append_text(log_file, reason)
            blocked_reasons.append(reason)
        elif not gh_repo_slug or not repo_is_sandbox(gh_repo_slug):
            reason = f'merge-cycle-block: repo not sandbox ({gh_repo_slug or "unknown"})'
            _append_text(log_file, reason)
            blocked_reasons.append(reason)
        else:
            open_pr_list = fetch_open_prs_for_merge(gh_repo_slug, cwd=repo_path)
            now = datetime.now(timezone.utc)
            cooldown_seconds = args.merge_cooldown_minutes * 60
            for pr in open_pr_list:
                pr_number = int(pr.get('number'))
                pr_url = str(pr.get('url') or '')
                created_at = parse_iso(pr.get('createdAt'))
                age = int((now - created_at).total_seconds()) if created_at else 0

                if bool(pr.get('isDraft')):
                    _append_text(log_file, f'merge-skip: pr=#{pr_number} draft=true')
                    continue

                if age < cooldown_seconds:
                    _append_text(
                        log_file,
                        f'merge-skip: pr=#{pr_number} cooldown age_seconds={age} required={cooldown_seconds}',
                    )
                    continue

                check_health = evaluate_pr_check_health(gh_repo_slug, pr_number, cwd=repo_path)
                if not check_health.get('eligible', False):
                    merges_failed += 1
                    reason = str(check_health.get('reason') or 'checks-not-eligible')
                    detail = f'merge-block: pr=#{pr_number} reason={reason}'
                    blocked_reasons.append(detail)
                    _append_text(log_file, detail)
                    continue

                review_status = evaluate_pr_reviews(gh_repo_slug, pr_number, cwd=repo_path)
                if not review_status.get('eligible', False):
                    review_state = _load_review_state(review_state_file)
                    autonomous_ok, autonomous_reason = _autonomous_review_gate_passes(review_state, pr_number)
                    if autonomous_ok:
                        _append_text(
                            log_file,
                            f'merge-autonomous-gate-pass: pr=#{pr_number} reason={autonomous_reason}',
                        )
                    else:
                        merges_failed += 1
                        reason = str(review_status.get('reason') or 'review-not-approved')
                        detail = f'merge-block: pr=#{pr_number} reason={reason} autonomous_gate={autonomous_reason}'
                        blocked_reasons.append(detail)
                        _append_text(log_file, detail)
                        continue

                mergeability = evaluate_pr_mergeability(gh_repo_slug, pr_number, cwd=repo_path)
                if not mergeability.get('eligible', False):
                    reason = str(mergeability.get('reason') or 'merge-state-not-eligible')
                    merge_state_status = str(mergeability.get('merge_state_status') or '').upper()
                    if merge_state_status == 'UNKNOWN' or reason == 'merge-state-unknown':
                        mergeability = {
                            **mergeability,
                            'eligible': True,
                            'requires_pr_fix': False,
                            'merge_state_status': 'UNKNOWN',
                            'reason': 'merge-state-unknown-proceed-cautiously',
                        }
                        _append_text(
                            log_file,
                            f'merge-caution: pr=#{pr_number} normalized legacy unknown merge-state to cautious pass',
                        )
                    else:
                        branch = str(pr.get('headRefName') or '')
                        if mergeability.get('requires_pr_fix', False):
                            for issue in issues_data.get('issues', []):
                                issue_github = issue.get('github', {}) if isinstance(issue.get('github'), dict) else {}
                                if int(issue_github.get('pr_number') or 0) == pr_number:
                                    _triage_pr_back_to_fix_cycle(
                                        issue=issue,
                                        pr_number=pr_number,
                                        pr_url=pr_url,
                                        branch=branch,
                                        reason=reason,
                                        log_file=log_file,
                                    )
                            _append_text(log_file, f'merge-triaged: pr=#{pr_number} reason={reason}')
                            continue

                        merges_failed += 1
                        detail = f'merge-block: pr=#{pr_number} reason={reason}'
                        blocked_reasons.append(detail)
                        _append_text(log_file, detail)
                        continue

                merge_attempts += 1
                if not check_health.get('has_checks', False):
                    _append_text(log_file, f'merge-caution: pr=#{pr_number} no checks found; proceeding')

                merged, merge_reason = merge_pr(gh_repo_slug, pr_number, dry_run=args.dry_run, cwd=repo_path)
                if merged:
                    merges_succeeded += 1
                    if pr_url:
                        merged_pr_urls.append(pr_url)
                    open_prs = max(0, open_prs - 1)
                    _append_text(log_file, f'merge-success: pr=#{pr_number} reason={merge_reason}')

                    for issue in issues_data.get('issues', []):
                        issue_github = issue.get('github', {}) if isinstance(issue.get('github'), dict) else {}
                        if int(issue_github.get('pr_number') or 0) == pr_number:
                            set_issue_status(issue, 'resolved_merged', f'PR merged: {pr_url or pr_number}')

                    # Post-merge rebase sweep
                    if args.auto_rebase_enabled:
                        _append_text(log_file, 'auto-rebase: starting sibling sweep')
                        try:
                            base_branch = str(pr.get('baseRefName', 'main'))
                            rebase_result = sweep_rebase(
                                repo_path=repo_path,
                                gh_repo_slug=gh_repo_slug,
                                merged_pr_number=pr_number,
                                base_branch=base_branch,
                                log_file=log_file,
                                dry_run=args.dry_run,
                                max_prs=args.rebase_max_prs,
                            )
                            for r in rebase_result.get('rebased', []):
                                _append_text(log_file, f'rebase-ok: pr=#{r["pr_number"]}')
                            for c in rebase_result.get('conflicted', []):
                                _append_text(log_file, f'rebase-conflict: pr=#{c["pr_number"]} files={c["files"]}')
                                blocked_reasons.append(
                                    f'rebase-conflict: pr=#{c["pr_number"]} files={", ".join(c["files"])}'
                                )
                            for s in rebase_result.get('skipped', []):
                                _append_text(log_file, f'rebase-skip: pr=#{s["pr_number"]} reason={s["reason"]}')
                        except Exception as exc:
                            _append_text(log_file, f'auto-rebase-error: {exc}')
                    break
                else:
                    branch = str(pr.get('headRefName') or '')
                    if merge_failure_requires_pr_fix(merge_reason):
                        for issue in issues_data.get('issues', []):
                            issue_github = issue.get('github', {}) if isinstance(issue.get('github'), dict) else {}
                            if int(issue_github.get('pr_number') or 0) == pr_number:
                                _triage_pr_back_to_fix_cycle(
                                    issue=issue,
                                    pr_number=pr_number,
                                    pr_url=pr_url,
                                    branch=branch,
                                    reason=merge_reason,
                                    log_file=log_file,
                                )
                        _append_text(log_file, f'merge-triaged: pr=#{pr_number} reason={merge_reason}')
                        continue

                    merges_failed += 1
                    detail = f'merge-failed: pr=#{pr_number} reason={merge_reason}'
                    blocked_reasons.append(detail)
                    _append_text(log_file, detail)

            if args.live_github_actions and not args.dry_run:
                open_issues, open_prs, reconcile_event = reconcile_open_workload(
                    repo_path=repo_path,
                    state=state,
                    log_file=log_file,
                    simulate_open_issues=args.simulate_open_issues,
                    simulate_open_prs=args.simulate_open_prs,
                )

    save_issues(issues_file, issues_data)
    unresolved_open = len(
        [x for x in issues_data.get('issues', []) if x.get('status') not in ('resolved_verified', 'resolved_merged')]
    )

    state['open_issues'] = open_issues
    state['open_prs'] = open_prs
    state['last_run_at'] = now_iso()
    save_state(state_file, state)

    if args.live_github_actions:
        mode = 'DRY-RUN-LIVE' if args.dry_run else 'ACTIVE-LIVE'
    else:
        mode = 'DRY-RUN' if args.dry_run else 'ACTIVE-SIM'
    run_mode = f'{mode}-{args.run_phase.upper()}'
    update_status_artifact(
        status_file=status_file,
        state=state,
        issues_file=issues_file,
        findings_file=findings_file,
        args=args,
        run_mode=run_mode,
        reconcile_event=reconcile_event,
        previous_last_run_at=previous_last_run_at,
        run_metrics={
            'findings_detected': len(findings),
            'findings_written': written_findings,
            'issues_created': len(created_issues),
            'fix_attempts': fix_attempts,
            'prs_created': created_prs,
            'fixes_verified': fixes_verified,
            'fixes_failed_verification': fixes_failed_verification,
            'unresolved_open': unresolved_open,
            'findings_suppressed_by_cooldown': len(suppressed_findings),
            'issues_escalated_max_retries': issues_escalated_max_retries,
            'merge_attempts': merge_attempts,
            'merges_succeeded': merges_succeeded,
            'merges_failed': merges_failed,
            'merged_pr_urls': merged_pr_urls,
            'blocked_events': len(blocked_reasons),
            'blocked_reasons': blocked_reasons,
        },
    )

    print(
        f'[DONE] {run_mode} findings={len(findings)} issues_created={len(created_issues)} '
        f'fix_attempts={fix_attempts} fixes_verified={fixes_verified} '
        f'fixes_failed_verification={fixes_failed_verification} prs_created={created_prs} '
        f'issues_escalated_max_retries={issues_escalated_max_retries} '
        f'merges={merges_succeeded}/{merge_attempts}'
    )
    _append_text(
        log_file,
        f'done: mode={run_mode} findings={len(findings)} issues={len(created_issues)} '
        f'fix_attempts={fix_attempts} fixes_verified={fixes_verified} '
        f'fixes_failed_verification={fixes_failed_verification} prs={created_prs} '
        f'issues_escalated_max_retries={issues_escalated_max_retries} '
        f'merges={merges_succeeded}/{merge_attempts}',
    )

    # Auto-log lesson at end of active cycles (not reconcile-only or verify-only)
    if args.run_phase in ('issue-cycle', 'pr-cycle', 'merge-cycle', 'orchestrated') and not args.reconcile_only:
        broke_parts: List[str] = []
        changed_parts: List[str] = []
        worked_parts: List[str] = []

        if fixes_failed_verification > 0:
            broke_parts.append(f'{fixes_failed_verification} fixes failed verification')
        if blocked_reasons:
            broke_parts.append(f'{len(blocked_reasons)} blocked events')

        if fixes_verified > 0:
            changed_parts.append(f'{fixes_verified} fixes verified')
        if created_prs > 0:
            changed_parts.append(f'{created_prs} PRs created')

        if len(created_issues) > 0:
            worked_parts.append(f'{len(created_issues)} issues flagged')
        if merges_succeeded > 0:
            worked_parts.append(f'{merges_succeeded} merges succeeded')

        append_lesson(
            lessons_file=lessons_file,
            cycle_type=args.run_phase,
            what_broke='; '.join(broke_parts) if broke_parts else '',
            what_changed='; '.join(changed_parts) if changed_parts else '',
            what_worked='; '.join(worked_parts) if worked_parts else '',
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
