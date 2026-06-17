#!/usr/bin/env python3
"""State management for QA Agent — app layer.

Shared findings/issues/state/runs/baselines state surfaces stay here.
Review-specific state surfaces (active PRs, review runs, review findings,
feedback events, learned rules, publish state, monitored safety, etc.)
were extracted to bluei/review/state.py:ReviewStateManager during the
H5 review→app decoupling (2026-06-18).

For backward compatibility, this module:
  1. Lazily re-exports the review-state DEFAULT_* constants (so external
     callers reading these symbols keep working).
  2. Forwards review-specific StateManager methods to a lazily-constructed
     ReviewStateManager instance. New code should use ReviewStateManager
     directly.

Note: We cannot import bluei.review.state at module load because
bluei/review/__init__.py eagerly imports review modules that depend on
bluei.app.state (cycle). The DEFAULT_* constants and ReviewStateManager
are therefore resolved lazily via module __getattr__ and the
self.review_state property respectively.
"""

import logging
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from bluei.review.state import ReviewStateManager  # noqa: F401

_logger = logging.getLogger(__name__)

from bluei.engine import issue_lifecycle
from bluei.engine import state as engine_state
from bluei.engine.models import Finding
from bluei.engine.state_io import (
    atomic_json_write,
    rotate_jsonl_if_needed,
    EVENT_LOG_MAX_BYTES,
    EVENT_LOG_MAX_LINES,
)
from bluei.engine.jsonl import append_jsonl, read_jsonl
from .models import Run, now_iso

# ——— Backward-compat aliases ———
# The canonical implementations now live in bluei/engine/state_io.py
# (extracted to fix engine→app layering violations, rec-08). These aliases
# preserve the old private names so existing callers — including
# bluei/campaigns/state.py, bluei/app/emergent_rules.py, tests, and any
# external consumers — keep working without code churn.
_atomic_json_write = atomic_json_write
_rotate_jsonl_if_needed = rotate_jsonl_if_needed
_EVENT_LOG_MAX_BYTES = EVENT_LOG_MAX_BYTES
_EVENT_LOG_MAX_LINES = EVENT_LOG_MAX_LINES


# Lazily-resolved review-state names. We cannot import these at module
# load time because bluei.review.__init__ eagerly loads modules that
# depend on bluei.app.state. Use module-level __getattr__ (PEP 562).
_LAZY_REVIEW_NAMES = {
    "ReviewStateManager",
    "DEFAULT_ACTIVE_PRS_STATE",
    "DEFAULT_REVIEW_STATE",
    "DEFAULT_REVIEW_RUN",
    "DEFAULT_REVIEW_FINDINGS_MANIFEST",
    "DEFAULT_LEARNED_RULES",
    "DEFAULT_REVIEW_PUBLISH_STATE",
    "DEFAULT_MONITORED_SAFETY_STATE",
    "DEFAULT_FEEDBACK_EVENT",
}


def __getattr__(name: str):
    """Lazy module-level attribute access for review-state re-exports.

    Resolves names listed in _LAZY_REVIEW_NAMES by importing them from
    bluei.review.state on first access. This breaks the circular import
    that would otherwise occur if we imported them at module load time.
    """
    if name in _LAZY_REVIEW_NAMES:
        from bluei.review import state as _review_state_module

        return getattr(_review_state_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class StateManager:
    """Manages persistent state for the agent.

    App-specific surfaces (findings, issues, runner state, runs, baselines,
    fix_patterns, campaigns, emergent_rules file path) are implemented
    directly here.

    Review-specific surfaces are forwarded to a lazily-constructed
    ReviewStateManager (see bluei/review/state.py). New code should
    construct ReviewStateManager directly when working with review state.
    """

    def __init__(self, repos_dir: Path):
        self.repos_dir = Path(repos_dir)
        # Lazily-built review delegate; shares the same repos_dir.
        self._review_state: Optional["ReviewStateManager"] = None

    @property
    def review_state(self) -> "ReviewStateManager":
        """Lazily construct the review-state delegate."""
        if self._review_state is None:
            # Imported lazily to avoid circular import at module load.
            from bluei.review.state import ReviewStateManager

            self._review_state = ReviewStateManager(self.repos_dir)
        return self._review_state

    def _get_repo_dir(self, repo_name: str) -> Path:
        return self.repos_dir / repo_name

    def _get_state_dir(self, repo_name: str) -> Path:
        return self._get_repo_dir(repo_name) / "state"

    # === Generic versioned JSON load/save ===
    # Used by both app-level state surfaces and (historically) review surfaces.
    # Kept here for app-side consumers (e.g., campaigns state) and tests that
    # exercise the versioned-load contract directly.

    def _load_versioned_json(
        self,
        path: Path,
        defaults: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Load a JSON file with versioned defaults merging."""
        if not path.exists():
            import copy

            return copy.deepcopy(defaults)
        with open(path) as f:
            data = json.load(f)
        for k, v in defaults.items():
            import copy

            data.setdefault(k, copy.deepcopy(v))
        return data

    def _save_versioned_json(
        self,
        path: Path,
        data: Dict[str, Any],
        defaults: Dict[str, Any],
    ) -> None:
        """Save a JSON file atomically with versioned defaults merging."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(defaults)
        payload.update(data or {})
        payload["updated_at"] = now_iso()
        _atomic_json_write(path, payload)

    # === Findings ===

    def get_findings_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / "findings.jsonl"

    def load_findings(self, repo_name: str) -> List[Finding]:
        """Load all findings for a repo.

        Uses bluei.engine.jsonl.read_jsonl for the read primitive; wraps each
        record in Finding.from_dict(). Malformed lines are silently skipped
        (skip_errors=True default).
        """
        findings_file = self.get_findings_file(repo_name)
        records = read_jsonl(findings_file)
        return [Finding.from_dict(r) for r in records]

    def append_findings(self, repo_name: str, findings: List[Finding]) -> int:
        """Append findings to repo's findings file.

        Delegates to engine.state.append_findings so discovered_at auto-set
        and sort_keys=True deterministic output both happen consistently.
        Returns the number of new findings actually written (dedup by
        finding_id, like the engine version).
        """
        return engine_state.append_findings(self.get_findings_file(repo_name), findings)

    def clear_findings(self, repo_name: str) -> None:
        """Clear findings file."""
        findings_file = self.get_findings_file(repo_name)
        if findings_file.exists():
            findings_file.unlink()

    # === Issues ===

    def get_issues_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / "issues.json"

    def load_issues(self, repo_name: str) -> Dict[str, Any]:
        """Load issues for a repo.

        Delegates to engine.issue_lifecycle.load_issues so shape validation
        (dict with list 'issues' key) and malformed-data handling are owned
        in one place.
        """
        return issue_lifecycle.load_issues(self.get_issues_file(repo_name))

    def save_issues(self, repo_name: str, issues: Dict[str, Any]) -> None:
        """Save issues for a repo.

        Delegates to engine.issue_lifecycle.save_issues so atomic write and
        schema ownership both live in the engine layer.
        """
        issue_lifecycle.save_issues(self.get_issues_file(repo_name), issues)

    # === State ===

    def get_state_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / "state.json"

    def load_state(self, repo_name: str) -> Dict[str, Any]:
        """Load runner state for a repo.

        Delegates to engine.state.load_state so the schema (5 keys including
        reconciliation_events) is owned in one place. Path is resolved via
        self.get_state_file(); the engine function handles file I/O and
        setdefault for missing keys.
        """
        return engine_state.load_state(self.get_state_file(repo_name))

    def save_state(self, repo_name: str, state: Dict[str, Any]) -> None:
        """Save runner state for a repo.

        Delegates to engine.state.save_state so atomic write semantics and
        schema ownership both live in the engine layer. Parent directory
        creation stays on StateManager (path-resolution concern).
        """
        state_file = self.get_state_file(repo_name)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        engine_state.save_state(state_file, state)

    # === Review care (delegated to ReviewStateManager) ===
    # These forward to self.review_state for backward compatibility with
    # tests and any external callers. New code should construct
    # bluei.review.state.ReviewStateManager directly.

    def get_active_prs_file(self, repo_name: str) -> Path:
        return self.review_state.get_active_prs_file(repo_name)

    def load_active_prs(self, repo_name: str) -> Dict[str, Any]:
        return self.review_state.load_active_prs(repo_name)

    def save_active_prs(self, repo_name: str, data: Dict[str, Any]) -> None:
        self.review_state.save_active_prs(repo_name, data)

    def get_review_state_file(self, repo_name: str) -> Path:
        return self.review_state.get_review_state_file(repo_name)

    def load_review_state(self, repo_name: str) -> Dict[str, Any]:
        return self.review_state.load_review_state(repo_name)

    def save_review_state(self, repo_name: str, data: Dict[str, Any]) -> None:
        self.review_state.save_review_state(repo_name, data)

    def get_review_events_file(self, repo_name: str) -> Path:
        return self.review_state.get_review_events_file(repo_name)

    def append_review_event(self, repo_name: str, event: Dict[str, Any]) -> None:
        self.review_state.append_review_event(repo_name, event)

    def get_review_locks_dir(self, repo_name: str) -> Path:
        return self.review_state.get_review_locks_dir(repo_name)

    def get_review_prompts_dir(self, repo_name: str) -> Path:
        return self.review_state.get_review_prompts_dir(repo_name)

    # === Phase B2: Autonomous-review state surfaces (delegated) ===

    def get_review_runs_dir(self, repo_name: str) -> Path:
        return self.review_state.get_review_runs_dir(repo_name)

    def save_review_run(self, repo_name: str, run_data: Dict[str, Any]) -> Path:
        return self.review_state.save_review_run(repo_name, run_data)

    def load_review_run(self, repo_name: str, run_id: str) -> Optional[Dict[str, Any]]:
        return self.review_state.load_review_run(repo_name, run_id)

    def list_review_runs(self, repo_name: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self.review_state.list_review_runs(repo_name, limit)

    def save_typed_review_run(self, repo_name: str, run: Any) -> Path:
        return self.review_state.save_typed_review_run(repo_name, run)

    def load_typed_review_run(self, repo_name: str, run_id: str) -> Optional[Any]:
        return self.review_state.load_typed_review_run(repo_name, run_id)

    def list_typed_review_runs(self, repo_name: str, limit: int = 20) -> List[Any]:
        return self.review_state.list_typed_review_runs(repo_name, limit)

    def get_review_findings_file(self, repo_name: str) -> Path:
        return self.review_state.get_review_findings_file(repo_name)

    def get_review_finding_file(self, repo_name: str, finding_id: str) -> Path:
        return self.review_state.get_review_finding_file(repo_name, finding_id)

    def append_review_findings(
        self, repo_name: str, findings: List[Dict[str, Any]]
    ) -> int:
        return self.review_state.append_review_findings(repo_name, findings)

    def load_review_findings(self, repo_name: str) -> List[Dict[str, Any]]:
        return self.review_state.load_review_findings(repo_name)

    def save_review_finding(
        self, repo_name: str, finding_id: str, data: Dict[str, Any]
    ) -> Path:
        return self.review_state.save_review_finding(repo_name, finding_id, data)

    def load_review_finding(
        self, repo_name: str, finding_id: str
    ) -> Optional[Dict[str, Any]]:
        return self.review_state.load_review_finding(repo_name, finding_id)

    def get_feedback_events_file(self, repo_name: str) -> Path:
        return self.review_state.get_feedback_events_file(repo_name)

    def append_feedback_event(self, repo_name: str, event: Dict[str, Any]) -> None:
        self.review_state.append_feedback_event(repo_name, event)

    def load_feedback_events(self, repo_name: str) -> List[Dict[str, Any]]:
        return self.review_state.load_feedback_events(repo_name)

    def append_typed_feedback_event(self, repo_name: str, event: Any) -> None:
        self.review_state.append_typed_feedback_event(repo_name, event)

    def load_typed_feedback_events(self, repo_name: str) -> List[Any]:
        return self.review_state.load_typed_feedback_events(repo_name)

    def get_learned_rules_file(self, repo_name: str) -> Path:
        return self.review_state.get_learned_rules_file(repo_name)

    def load_learned_rules(self, repo_name: str) -> Dict[str, Any]:
        return self.review_state.load_learned_rules(repo_name)

    def save_learned_rules(self, repo_name: str, data: Dict[str, Any]) -> None:
        self.review_state.save_learned_rules(repo_name, data)

    def get_review_publish_state_file(self, repo_name: str) -> Path:
        return self.review_state.get_review_publish_state_file(repo_name)

    def load_review_publish_state(self, repo_name: str) -> Dict[str, Any]:
        return self.review_state.load_review_publish_state(repo_name)

    def save_review_publish_state(self, repo_name: str, data: Dict[str, Any]) -> None:
        self.review_state.save_review_publish_state(repo_name, data)

    def get_monitored_safety_state_file(self, repo_name: str) -> Path:
        return self.review_state.get_monitored_safety_state_file(repo_name)

    def load_monitored_safety_state(self, repo_name: str) -> Dict[str, Any]:
        return self.review_state.load_monitored_safety_state(repo_name)

    def save_monitored_safety_state(self, repo_name: str, data: Dict[str, Any]) -> None:
        self.review_state.save_monitored_safety_state(repo_name, data)

    # --- fix_patterns.jsonl --- (app-owned; shared with review via this method)

    def get_fix_patterns_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / "fix_patterns.jsonl"

    def get_campaigns_dir(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / "campaigns"

    def get_emergent_rules_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / "emergent_rules.json"

    # === Runs ===

    def get_runs_dir(self, repo_name: str) -> Path:
        runs_dir = self._get_repo_dir(repo_name) / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        return runs_dir

    def save_run(self, repo_name: str, run: Run) -> Path:
        """Save a run record."""
        runs_dir = self.get_runs_dir(repo_name)
        run_file = runs_dir / f"{run.id}.json"
        _atomic_json_write(run_file, run.to_dict())
        return run_file

    def load_run(self, repo_name: str, run_id: str) -> Optional[Run]:
        """Load a specific run."""
        runs_dir = self.get_runs_dir(repo_name)
        run_file = runs_dir / f"{run_id}.json"

        if not run_file.exists():
            return None

        with open(run_file) as f:
            data = json.load(f)

        return Run(**data)

    def list_runs(self, repo_name: str, limit: int = 10) -> List[Dict]:
        """List recent runs."""
        runs_dir = self.get_runs_dir(repo_name)
        if not runs_dir.exists():
            return []

        runs = []
        for run_file in sorted(runs_dir.glob("*.json"), reverse=True)[:limit]:
            with open(run_file) as f:
                runs.append(json.load(f))

        return runs

    # === Baselines ===

    def get_baselines_dir(self, repo_name: str) -> Path:
        baselines_dir = self._get_repo_dir(repo_name) / "baselines"
        baselines_dir.mkdir(parents=True, exist_ok=True)
        return baselines_dir

    def save_baseline(self, repo_name: str, baseline: Dict[str, Any]) -> Path:
        """Save a baseline."""
        baselines_dir = self.get_baselines_dir(repo_name)
        baseline_file = baselines_dir / f"{baseline['id']}.json"
        _atomic_json_write(baseline_file, baseline)
        return baseline_file

    def load_baseline(self, repo_name: str, baseline_id: str) -> Optional[Dict]:
        """Load a baseline."""
        baselines_dir = self.get_baselines_dir(repo_name)
        baseline_file = baselines_dir / f"{baseline_id}.json"

        if not baseline_file.exists():
            return None

        with open(baseline_file) as f:
            return json.load(f)

    def list_baselines(self, repo_name: str) -> List[str]:
        """List baseline IDs."""
        baselines_dir = self.get_baselines_dir(repo_name)
        if not baselines_dir.exists():
            return []

        return [f.stem for f in baselines_dir.glob("*.json")]
