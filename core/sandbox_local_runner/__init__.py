"""sandbox_local_runner — local sandbox QA workflow runner package."""

# Re-export public API only. No wildcard imports.
from .constants import (
    DETECTOR_CATALOG,
    BASELINE_VALIDATION_CHECKS,
    RULE_TARGET_CHECKS,
    CLAUDE_REQUIRED_RULES,
    BLOCKED_REPOS,
    MAX_LINES_REFACTOR_LIMIT,
    MAX_LINES_REFACTOR_TARGET,
    DEFAULT_FINDING_COOLDOWN_SECONDS,
    DEFAULT_STALENESS_THRESHOLD_SECONDS,
    MAX_RECONCILIATION_EVENTS,
    QA_FIX_PROMPT_FILENAME,
    WORKSPACE,
    AGENT_ROOT,
    RUNNER_PATH,
    DEFAULT_REPO,
    DEFAULT_STATE,
    DEFAULT_LOG,
    DEFAULT_FINDINGS,
    DEFAULT_ISSUES,
    DEFAULT_WORKTREE_ROOT,
    DEFAULT_STATUS,
    DEFAULT_DOCS_INDEX,
    DEFAULT_LESSONS_LOG,
    DEFAULT_FIX_ENGINE,
    DEFAULT_CLAUDE_CMD_TEMPLATE,
    load_llm_fixable_rules,
)
from .models import Finding, now_iso, parse_iso, age_seconds, stable_finding_id
from .utils import (
    run_capture, run_no_capture, is_path_tracked,
    sanitize_command_template, command_list_to_shell, append_lesson,
    load_lessons_for_finding,     # NEW
    load_recent_lessons,          # NEW
    assert_safe_repo, branch_suffix,
)
from .state import (
    load_state, save_state,
    load_findings_seen, append_findings,
    load_finding_record,          # NEW
    update_finding_record,        # NEW
    increment_fix_attempt,        # NEW
    load_refactor_work,
    save_refactor_work,
    get_pending_refactor_work,
    get_effective_cooldown,      # NEW (Phase 3)
    load_issues, save_issues,
    guard_open_issues, guard_open_prs,
    record_reconciliation_event, reconcile_open_workload,
    mark_finding_activity, filter_findings_by_cooldown,
)
from .gh import (
    get_origin_url, parse_github_repo, finding_dedupe_marker,
    parse_issue_number_from_url, parse_pr_number_from_url,
    find_existing_github_issue, find_existing_github_pr,
    gh_issue_comment, gh_issue_close, gh_pr_comment,
    finding_from_issue_record, repo_is_sandbox,
    fetch_open_prs_for_merge, evaluate_pr_check_health,
    evaluate_pr_reviews, merge_pr,
    create_or_update_github_issue, create_or_update_github_pr,
    fetch_github_live_counts,
)
from .linters import (
    run_xo_linter_in_container,
    discover_xo_linter_findings,
    discover_python_linter_findings,
    discover_typescript_type_findings,
    discover_test_coverage_findings,
)
from .git_utils import get_branch, refresh_docs_index, load_docs_index
from .prompts import (
    render_test_coverage_prompt,
    render_type_safety_prompt,
    render_complexity_refactor_prompt,
    render_maxlines_refactor_prompt,
    render_claude_fix_prompt,
)
from .orchestrator import (
    build_active_cycle_command,
    build_issue_cycle_command,
    build_pr_cycle_command,
    build_merge_cycle_command,
    build_orchestrated_cycle_command,
    build_refactor_cycle_command,
    build_reconcile_only_command,
    build_docs_index_refresh_command,
    build_verification_only_command,
    discover_findings,
    create_issues_for_findings,
    choose_safe_autofix_items,
    route_findings_with_intent,
    find_issue_for_finding,
    append_issue_history,
    set_issue_status,
    count_failed_fix_attempts,
    ensure_issue_for_finding,
)
from .lifecycle import (
    verify_fix_closed,
    apply_autofix,
    apply_claude_fix,
    route_to_human_review,
    process_refactor_queue,
    git_commit_all,
    git_push_branch,
    diff_stats,
    run_named_checks,
    build_target_checks,
    run_validation_gate,
    classify_review_feedback,
    review_loop_allowed,
)
from .reforge import (
    RefactorClass,
    RefactorPhase,
    RefactorWork,
    classify_finding,
    can_auto_refactor,
    describe_class,
    is_large_refactor,
    REFACTOR_CLASS_RULES,
    CLAUDE_FIX_RULES,
    LARGE_FILE_SAFETY_LIMIT,
)
from .refactor_queue import (
    RefactorQueue,
    QueueEntry,
    QueueStatus,
    enqueue_refactor_work,
    list_pending_review,
    create_refactor_plan,
)
from .cli import update_status_artifact, main
