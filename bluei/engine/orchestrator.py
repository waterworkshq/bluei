"""Cycle orchestration facade.

Historically this module held the command builders, AST discovery
pipeline, finding router, and issue lifecycle code. Those have been
extracted into focused modules:

- ``bluei.engine.command_builder`` — CLI command builders for each cycle phase
- ``bluei.engine.discovery`` — AST scans, string detectors, linters, plugins
- ``bluei.engine.finding_router`` — finding routing into execution lanes
- ``bluei.engine.issue_lifecycle`` — issue creation, status, history, escalation

This file re-exports the public symbols so existing imports of the form
``from bluei.engine.orchestrator import X`` continue to work.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Command builders — re-exported from command_builder.py
# ---------------------------------------------------------------------------

from bluei.engine.command_builder import (  # noqa: F401
    build_active_cycle_command,
    build_docs_index_refresh_command,
    build_issue_cycle_command,
    build_merge_cycle_command,
    build_orchestrated_cycle_command,
    build_pr_cycle_command,
    build_reconcile_only_command,
    build_refactor_cycle_command,
    build_verification_only_command,
)

# ---------------------------------------------------------------------------
# Discovery pipeline — re-exported from discovery.py
# ---------------------------------------------------------------------------

from bluei.engine.discovery import (  # noqa: F401
    _AST_SKIP_DIRS,
    _TS_SKIP_DIRS,
    LANGUAGE_SCAN_CONFIGS,
    LanguageScanConfig,
    _ast_scan_go_files,
    _ast_scan_python_files,
    _ast_scan_rust_files,
    _ast_scan_ts_js_files,
    _go_getter,
    _python_getter,
    _rust_getter,
    _ts_getter,
    ast_scan_pipeline,
    discover_findings,
)

# ---------------------------------------------------------------------------
# Finding router — re-exported from finding_router.py
# ---------------------------------------------------------------------------

from bluei.engine.finding_router import (  # noqa: F401
    choose_safe_autofix_items,
    route_findings_with_intent,
)

# ``classify_finding`` lives in ``reforge`` but legacy callers (e.g.
# ``bluei.engine.commands.pr_cycle``) still import it via orchestrator.
from bluei.engine.reforge import classify_finding  # noqa: F401

# ---------------------------------------------------------------------------
# Issue lifecycle — re-exported from issue_lifecycle.py
# (Kept here for backward compatibility with monkeypatch targets in tests.)
# ---------------------------------------------------------------------------

from bluei.engine.issue_lifecycle import (  # noqa: F401
    check_consecutive_fix_failures,
    check_finding_escalation_before_fix,
    count_failed_fix_attempts,
    create_issues_for_findings,
    ensure_issue_for_finding,
    find_issue_for_finding,
    set_issue_status,
)


def append_issue_history(
    issue: Dict[str, Any], event: str, detail: Optional[str] = None
) -> None:
    """Re-export from issue_lifecycle — kept for monkeypatch compat."""
    from bluei.engine.issue_lifecycle import append_issue_history as _impl

    _impl(issue, event, detail)
