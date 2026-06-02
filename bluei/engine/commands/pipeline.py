"""Pipeline phase handlers — thin re-export layer.

All functions have been extracted into individual phase modules.
This module re-exports them for backward compatibility with cli.py imports.
"""

from bluei.engine.commands.reconcile import run_reconcile_only
from bluei.engine.commands.verify_only import run_verify_only
from bluei.engine.commands.discover import run_discover_phase, DiscoverResult
from bluei.engine.commands.issue_cycle import (
    run_issue_creation_phase,
    IssueCreationResult,
)
from bluei.engine.commands.finalize import run_finalize_phase
from bluei.engine.commands.merge_cycle import run_merge_cycle_phase, MergeCycleResult
from bluei.engine.commands.pr_cycle import run_pr_cycle_phase
from bluei.engine.gh import (
    repo_is_sandbox,
    fetch_open_prs_for_merge,
    evaluate_pr_check_health,
    evaluate_pr_mergeability,
    evaluate_pr_reviews,
    merge_failure_requires_pr_fix,
    merge_pr,
)
from bluei.engine.rebase_sweep import sweep_rebase
from bluei.engine.state import reconcile_open_workload
from bluei.engine.commands.helpers import (
    _autonomous_review_gate_passes,
    _load_review_state,
    _triage_pr_back_to_fix_cycle,
)

__all__ = [
    "run_reconcile_only",
    "run_verify_only",
    "run_discover_phase",
    "DiscoverResult",
    "run_issue_creation_phase",
    "IssueCreationResult",
    "run_finalize_phase",
    "run_merge_cycle_phase",
    "MergeCycleResult",
    "run_pr_cycle_phase",
    "repo_is_sandbox",
    "fetch_open_prs_for_merge",
    "evaluate_pr_check_health",
    "evaluate_pr_mergeability",
    "evaluate_pr_reviews",
    "merge_failure_requires_pr_fix",
    "merge_pr",
    "sweep_rebase",
    "reconcile_open_workload",
    "_autonomous_review_gate_passes",
    "_load_review_state",
    "_triage_pr_back_to_fix_cycle",
]
