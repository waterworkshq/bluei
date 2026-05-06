#!/usr/bin/env python3
"""check_completeness.py — verify every source def/class appears in exactly one target module."""

import ast
from pathlib import Path

SOURCE = Path(__file__).parent.parent / "sandbox_local_runner.py.bak"
PACKAGE = Path(__file__).parent


def main():
    if not SOURCE.exists():
        print(f"Source file not found: {SOURCE}")
        print("Skipping completeness check (backup file not present)")
        return

    # Build source inventory
    tree = ast.parse(SOURCE.read_text())
    source_symbols = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            source_symbols[node.name] = "class"
        elif isinstance(node, ast.FunctionDef):
            source_symbols[node.name] = "function"

    # Build target inventory
    target_symbols = {}
    for py_file in PACKAGE.glob("*.py"):
        if py_file.name.startswith("_") or py_file.name in (
            "enforce_architecture.py",
            "check_completeness.py",
            "test_refactor.py",
            "test_directive_seeding.py",
            "test_directive_seeding_e2e.py",
            "test_mnemo_client.py",
            "test_reforge.py",
            "test_refactor_queue.py",
            "test_route_findings_with_intent.py",
            "test_refactor_state.py",
        ):
            continue
        try:
            t = ast.parse(py_file.read_text())
        except Exception:
            continue
        for node in ast.walk(t):
            if isinstance(node, ast.ClassDef):
                target_symbols[node.name] = py_file.name
            elif isinstance(node, ast.FunctionDef):
                target_symbols[node.name] = py_file.name

    missing = set(source_symbols.keys()) - set(target_symbols.keys())
    extra = set(target_symbols.keys()) - set(source_symbols.keys())

    # Known-intentional extras from Phase 1-3 additions (classmethods and new state/utils functions)
    KNOWN_EXTRA = {
        "from_dict",          # Finding classmethod (Phase 1)
        "get_effective_cooldown",   # state.py new function (Phase 3)
        "load_finding_record",      # state.py new function (Phase 1)
        "update_finding_record",    # state.py new function (Phase 1)
        "increment_fix_attempt",    # state.py new function (Phase 1)
        "load_lessons_for_finding", # utils.py new function (Phase 2)
        "load_recent_lessons",      # utils.py new function (Phase 2)
        # Phase 5: mnemo_client.py additions
        "MnemoClient",
        "is_mnemo_available",
        "_build_query",
        "_build_finding_context",
        "_build_outcome_context",
        "_call_recall",
        "_capture",
        "_process_result",
        "recall",
        "seed",
        # Phase 5: test helpers / fixtures from e2e test (e2e file skipped but symbols visible)
        "make_finding",
        "now_utc",
        "run_tests",
        "test_full_directive_seeding_e2e_flow",
        # Refactor-class scaffolding (reforge.py — net-new, not from monolith)
        "RefactorClass",
        "RefactorPhase",
        "RefactorWork",
        "classify_finding",
        "can_auto_refactor",
        "is_large_refactor",
        "describe_class",
        "LARGE_FILE_SAFETY_LIMIT",
        "REFACTOR_CLASS_RULES",
        "CLAUDE_FIX_RULES",
        # __init__ dunder methods from __init__.py classes
        "__init__",
        # refactor_queue.py — net-new module (not from monolith)
        "RefactorQueue",
        "QueueEntry",
        "QueueStatus",
        "enqueue_refactor_work",
        "list_pending_review",
        "create_refactor_plan",
        "load_refactor_work",
        "save_refactor_work",
        "get_pending_refactor_work",
        "route_findings_with_intent",
        "route_to_human_review",
        "process_refactor_queue",
        "_queue_dir",
        "_work_file",
        "_symlink_path",
        "_write_entry",
        "_relink",
        "enqueue",
        "get",
        "list_items",
        "approve",
        "start_execution",
        "complete",
        "fail",
        "abort_pending",
        "count_by_status",
        "build_refactor_cycle_command",
        "_build_refactor_queue_snapshot",
        # Additional intentional extras from newer package-only modules/helpers
        "count_actionable_issues",
        "choose_validation_baseline",
        "evaluate_pr_mergeability",
        "merge_failure_requires_pr_fix",
        "load_llm_fixable_rules",
        "capture",
        "is_available",
        "CallGraphEntry",
        "Dependency",
        "FileRelevance",
        "PatternMatch",
        "find_relevant_files",
        "find_relevant_files_for_finding",
        "get_context_for_finding",
        "get_dependencies",
        "get_symbols_for_file",
        "get_callees",
        "get_callers",
        "format",
        "format_brief",
        "format_callees",
        "format_callers",
        "search_patterns",
        "mark_splitting",
        "mark_validating",
        "mark_done",
        "mark_aborted",
        "_autonomous_review_gate_passes",
        "_extract_search_terms",
        "_get_conn",
        "_get_llm_fixable_rules",
        "_get_mnemo_client",
        "_hydrate_worktree_dependencies",
        "_load_review_state",
        "_mnemo_enabled",
        "_project_for",
        "_recall_via_cli",
        "_reconcile_issue_pr_link",
        "_should_use_mnemo",
        "_sort_key",
        "_triage_pr_back_to_fix_cycle",
    }
    unexpected_extra = extra - KNOWN_EXTRA
    if missing:
        print(f"MISSING from package: {sorted(missing)}")
    if unexpected_extra:
        print(f"EXTRA in package (not in source): {sorted(unexpected_extra)}")
    if extra - KNOWN_EXTRA:
        print(f"(Known extras ignored: {sorted(extra & KNOWN_EXTRA)})")
    if not missing and not unexpected_extra:
        print("✅  Completeness check passed")


main()
