"""RunContext — shared pipeline state container for cli.py decomposition.

Packs the local variables from main() into a single mutable dataclass
that pipeline phases receive and mutate by reference.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from bluei.engine.cost_tracker import CostTracker
from bluei.engine.model_governor import SelectionFn, identity_selection
from bluei.engine.models import Finding
from bluei.engine.pattern_store import FixPatternStore

if TYPE_CHECKING:  # pragma: no cover — forward ref only, avoids import cycle
    from bluei.common.models import RepoConfig


@dataclass
class RunContext:
    args: Any  # argparse.Namespace

    # Paths
    repo_path: Path = field(default_factory=Path)
    state_file: Path = field(default_factory=Path)
    log_file: Path = field(default_factory=Path)
    findings_file: Path = field(default_factory=Path)
    issues_file: Path = field(default_factory=Path)
    worktree_root: Path = field(default_factory=Path)
    status_file: Path = field(default_factory=Path)
    docs_index_file: Path = field(default_factory=Path)
    lessons_file: Path = field(default_factory=Path)
    review_state_file: Path = field(default_factory=Path)

    # GitHub
    gh_repo_slug: str = ""
    origin_url: str = ""

    # State
    state: Dict[str, Any] = field(default_factory=dict)
    issues_data: Dict[str, Any] = field(default_factory=dict)
    open_issues: int = 0
    open_prs: int = 0
    reconcile_event: Dict[str, Any] = field(default_factory=dict)
    previous_last_run_at: Optional[str] = None

    # Findings
    findings: List[Finding] = field(default_factory=list)
    written_findings: int = 0
    eligible_findings: List[Finding] = field(default_factory=list)
    suppressed_findings: List[Finding] = field(default_factory=list)
    refactor_routed_items: List[Dict[str, Any]] = field(default_factory=list)

    # Counters
    fix_attempts: int = 0
    fixes_verified: int = 0
    fixes_failed_verification: int = 0
    issues_escalated_max_retries: int = 0
    created_issues: List[Dict[str, Any]] = field(default_factory=list)
    created_prs: int = 0
    claude_invocations: int = 0
    opencode_invocations: int = 0
    deterministic_invocations: int = 0
    merge_attempts: int = 0
    merges_succeeded: int = 0
    merges_failed: int = 0
    merged_pr_urls: List[str] = field(default_factory=list)
    blocked_reasons: List[str] = field(default_factory=list)

    # Cost tracking
    cost_tracker: Optional[CostTracker] = None
    cost_log_path: Optional[Path] = None
    run_id: str = ""

    # Governance State (ADR-0008) — read-time projection of the
    # approval_records.jsonl trail; populated at cycle start. Empty until the
    # first ApprovalRecord is written (substrate no-op).
    governance_state: Dict[str, str] = field(default_factory=dict)

    # Learning mode (ADR-0013) — global tri-state kill switch resolved once at
    # cycle start. ``active`` | ``audit_only`` | ``paused``. Phases 6 (Dry
    # Replay) and 7 (SPRT) consult this to short-circuit their effects.
    learning_mode: str = "active"

    # Flywheel Ledger accumulator (populated by cascade + standalone replay; read by finalize)
    ledger_records: List[Dict[str, Any]] = field(default_factory=list)

    # Pattern store
    pattern_store: Optional[FixPatternStore] = None

    # Model Governor (ADR-0022) — selection function injected so beta.1 can swap
    # identity -> select_tier via config (policy flip, not code edit). Default
    # is identity_selection (returns tier-2; no behavior change in alpha.6).
    selection_fn: SelectionFn = field(default_factory=lambda: identity_selection)
    governor_ledger_path: Optional[Path] = (
        None  # mirrors cost_log_path; resolved at cycle start
    )

    # Repo config (resolved once at cycle start) — used by the taste channel
    # to read RepoConfig.framework. Forward-ref string avoids an import cycle.
    repo_config: Optional["RepoConfig"] = None

    # Baseline checks (per-repo, resolved once)
    PER_REPO_BASELINE_CHECKS: Dict[str, List[str]] = field(default_factory=dict)

    # Cycle flags
    run_issue_cycle: bool = False
    run_pr_cycle: bool = False
    run_merge_cycle: bool = False
