#!/usr/bin/env python3
"""State management for QA Agent."""

import logging
import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

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
from .models import Run, now_iso, ReviewRun, FeedbackEvent

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


DEFAULT_ACTIVE_PRS_STATE = {
    "version": 1,
    "updated_at": None,
    "prs": {},
}

DEFAULT_REVIEW_STATE = {
    "version": 1,
    "updated_at": None,
    "prs": {},
}

# --- Phase B2: Autonomous-review state surface defaults ---

DEFAULT_REVIEW_RUN = {
    "version": 1,
    "run_id": "",
    "repo": "",
    "pr_number": None,
    "started_at": None,
    "ended_at": None,
    "status": "pending",  # pending | running | completed | failed
    "findings_count": 0,
    "publish_status": "none",  # none | published | failed | skipped
    "error": None,
}

DEFAULT_REVIEW_FINDINGS_MANIFEST = {
    "version": 1,
    "updated_at": None,
    "total_findings": 0,
}

DEFAULT_LEARNED_RULES = {
    "version": 1,
    "updated_at": None,
    "rules": [],  # list of learned rule objects
    "active_count": 0,
    "tentative_count": 0,
}

DEFAULT_REVIEW_PUBLISH_STATE = {
    "version": 1,
    "updated_at": None,
    "findings": {},  # { finding_id: publish_entry }
    "runs": {},  # { run_id: run_publish_entry }
}

# Phase G7: Monitored-rollout safety state default
DEFAULT_MONITORED_SAFETY_STATE = {
    "version": 1,
    "updated_at": None,
    "circuit_open": False,
    "failure_count": 0,
    "cooldown_until": None,
    "last_failure_at": None,
    "last_failure_reason": "",
    "auto_rollback_active": False,
    "auto_rollback_reason": "",
    "auto_rollback_triggered_at": None,
}

DEFAULT_FEEDBACK_EVENT = {
    "version": 1,
    "timestamp": None,
    "source": None,  # e.g. 'github_review_comment', 'github_review_thread'
    "pr_number": None,
    "finding_id": None,
    "signal": None,  # 'positive' | 'negative' | 'conflict' | 'request_change' | 'approve' | 'comment'
    "normalized": False,
    "payload": {},
}


class StateManager:
    """Manages persistent state for the agent."""

    def __init__(self, repos_dir: Path):
        self.repos_dir = Path(repos_dir)

    def _get_repo_dir(self, repo_name: str) -> Path:
        return self.repos_dir / repo_name

    def _get_state_dir(self, repo_name: str) -> Path:
        return self._get_repo_dir(repo_name) / "state"

    # === Generic versioned JSON load/save ===

    def _load_versioned_json(
        self,
        path: Path,
        defaults: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Load a JSON file with versioned defaults merging."""
        if not path.exists():
            return copy.deepcopy(defaults)
        with open(path) as f:
            data = json.load(f)
        for k, v in defaults.items():
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

    # === Review care ===

    def get_active_prs_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / "active_prs.json"

    def load_active_prs(self, repo_name: str) -> Dict[str, Any]:
        return self._load_versioned_json(
            self.get_active_prs_file(repo_name), DEFAULT_ACTIVE_PRS_STATE
        )

    def save_active_prs(self, repo_name: str, data: Dict[str, Any]) -> None:
        self._save_versioned_json(
            self.get_active_prs_file(repo_name), data, DEFAULT_ACTIVE_PRS_STATE
        )

    def get_review_state_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / "review_state.json"

    def load_review_state(self, repo_name: str) -> Dict[str, Any]:
        return self._load_versioned_json(
            self.get_review_state_file(repo_name), DEFAULT_REVIEW_STATE
        )

    def save_review_state(self, repo_name: str, data: Dict[str, Any]) -> None:
        self._save_versioned_json(
            self.get_review_state_file(repo_name), data, DEFAULT_REVIEW_STATE
        )

    def get_review_events_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / "review_events.jsonl"

    def append_review_event(self, repo_name: str, event: Dict[str, Any]) -> None:
        path = self.get_review_events_file(repo_name)
        payload = {"timestamp": now_iso(), **(event or {})}
        append_jsonl(path, payload)
        _rotate_jsonl_if_needed(path)

    def get_review_locks_dir(self, repo_name: str) -> Path:
        path = self._get_state_dir(repo_name) / "review_locks"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_review_prompts_dir(self, repo_name: str) -> Path:
        path = self._get_state_dir(repo_name) / "review_prompts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # === Phase B2: Autonomous-review state surfaces ===

    # --- review_runs/ (per-run JSON files) ---

    def get_review_runs_dir(self, repo_name: str) -> Path:
        path = self._get_state_dir(repo_name) / "review_runs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_review_run(self, repo_name: str, run_data: Dict[str, Any]) -> Path:
        """Save an autonomous-review run record."""
        run_id = run_data.get("run_id") or run_data.get("id")
        if not run_id:
            raise ValueError("review run data must contain 'run_id' or 'id'")
        path = self.get_review_runs_dir(repo_name) / f"{run_id}.json"
        payload = dict(DEFAULT_REVIEW_RUN)
        payload.update(run_data or {})
        if not payload.get("run_id"):
            payload["run_id"] = run_id
        payload["updated_at"] = now_iso()
        _atomic_json_write(path, payload)
        return path

    def load_review_run(self, repo_name: str, run_id: str) -> Optional[Dict[str, Any]]:
        """Load a specific autonomous-review run."""
        path = self.get_review_runs_dir(repo_name) / f"{run_id}.json"
        if not path.exists():
            return None
        with open(path) as f:
            data = json.load(f)
        # ensure versioned defaults
        for k, v in DEFAULT_REVIEW_RUN.items():
            data.setdefault(k, v)
        return data

    def list_review_runs(self, repo_name: str, limit: int = 20) -> List[Dict[str, Any]]:
        """List recent autonomous-review runs, newest first.

        Sorts by ``started_at`` descending (most recent first), with file
        modification time as a tiebreaker for runs with identical timestamps.
        This ensures deterministic ordering regardless of UUID ordering in
        run_id filenames.
        """
        runs_dir = self.get_review_runs_dir(repo_name)
        if not runs_dir.exists():
            return []
        # Load all run data first, then sort by started_at desc, then mtime desc
        run_files: List[tuple] = []
        for p in runs_dir.glob("*.json"):
            with open(p) as f:
                data = json.load(f)
            for k, v in DEFAULT_REVIEW_RUN.items():
                data.setdefault(k, v)
            mtime = p.stat().st_mtime
            # Use started_at as primary key (iso string sorts correctly chronologically)
            # Tiebreak on mtime (run2's file has strictly later mtime than run1's)
            run_files.append((data.get("started_at") or "", -mtime, data))
        # Sort: primary key desc (newest first), secondary key desc (later mtime first)
        run_files.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [item[2] for item in run_files[:limit]]

    def save_typed_review_run(self, repo_name: str, run: ReviewRun) -> Path:
        """Save a ReviewRun dataclass as a review run record."""
        return self.save_review_run(repo_name, run.to_dict())

    def load_typed_review_run(self, repo_name: str, run_id: str) -> Optional[ReviewRun]:
        """Load a review run as a ReviewRun dataclass."""
        data = self.load_review_run(repo_name, run_id)
        if data is None:
            return None
        return ReviewRun.from_dict(data)

    def list_typed_review_runs(
        self, repo_name: str, limit: int = 20
    ) -> List[ReviewRun]:
        """List recent review runs as ReviewRun dataclasses."""
        return [ReviewRun.from_dict(d) for d in self.list_review_runs(repo_name, limit)]

    # --- review_findings.jsonl + review_findings/<finding_id>.json ---

    def get_review_findings_file(self, repo_name: str) -> Path:
        """Path to the review findings JSONL manifest/index."""
        return self._get_state_dir(repo_name) / "review_findings.jsonl"

    def get_review_finding_file(self, repo_name: str, finding_id: str) -> Path:
        """Path to an individual review finding JSON file."""
        d = self._get_state_dir(repo_name) / "review_findings"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{finding_id}.json"

    def append_review_findings(
        self, repo_name: str, findings: List[Dict[str, Any]]
    ) -> int:
        """Append review findings to the JSONL index; deduplicates by finding_id.

        Returns the number of newly written records.
        """
        findings_file = self.get_review_findings_file(repo_name)
        findings_file.parent.mkdir(parents=True, exist_ok=True)

        existing_ids = set()
        if findings_file.exists():
            with open(findings_file) as f:
                for line in f:
                    if line.strip():
                        existing_ids.add(json.loads(line).get("finding_id"))

        written = 0
        for finding in findings:
            fid = finding.get("finding_id")
            if fid and fid not in existing_ids:
                append_jsonl(findings_file, finding)
                written += 1
        return written

    def load_review_findings(self, repo_name: str) -> List[Dict[str, Any]]:
        """Load all review findings from the JSONL index."""
        findings_file = self.get_review_findings_file(repo_name)
        if not findings_file.exists():
            return []
        findings = []
        with open(findings_file) as f:
            for line in f:
                if line.strip():
                    findings.append(json.loads(line))
        return findings

    def save_review_finding(
        self, repo_name: str, finding_id: str, data: Dict[str, Any]
    ) -> Path:
        """Save an individual review finding as a standalone JSON file."""
        path = self.get_review_finding_file(repo_name, finding_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(data)
        payload.setdefault("finding_id", finding_id)
        payload.setdefault("version", 1)
        payload.setdefault("saved_at", now_iso())
        _atomic_json_write(path, payload)
        return path

    def load_review_finding(
        self, repo_name: str, finding_id: str
    ) -> Optional[Dict[str, Any]]:
        """Load a specific review finding by ID."""
        path = self.get_review_finding_file(repo_name, finding_id)
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    # --- feedback_events.jsonl ---

    def get_feedback_events_file(self, repo_name: str) -> Path:
        """Path to the feedback events JSONL log."""
        return self._get_state_dir(repo_name) / "feedback_events.jsonl"

    def append_feedback_event(self, repo_name: str, event: Dict[str, Any]) -> None:
        """Append a feedback event to the JSONL log."""
        path = self.get_feedback_events_file(repo_name)
        payload = dict(DEFAULT_FEEDBACK_EVENT)
        payload.update(event or {})
        if not payload.get("timestamp"):
            payload["timestamp"] = now_iso()
        payload["version"] = 1
        append_jsonl(path, payload)
        _rotate_jsonl_if_needed(path)

    def load_feedback_events(self, repo_name: str) -> List[Dict[str, Any]]:
        """Load all feedback events from the JSONL log."""
        path = self.get_feedback_events_file(repo_name)
        if not path.exists():
            return []
        events = []
        with open(path) as f:
            for line in f:
                if line.strip():
                    events.append(json.loads(line))
        return events

    def append_typed_feedback_event(self, repo_name: str, event: FeedbackEvent) -> None:
        """Append a FeedbackEvent dataclass to the JSONL log."""
        self.append_feedback_event(repo_name, event.to_dict())

    def load_typed_feedback_events(self, repo_name: str) -> List[FeedbackEvent]:
        """Load all feedback events as FeedbackEvent dataclasses."""
        events = []
        for data in self.load_feedback_events(repo_name):
            try:
                events.append(FeedbackEvent.from_dict(data))
            except (ValueError, TypeError):
                _logger.debug("Skipping malformed feedback event")
        return events

    # --- learned_rules.json ---

    def get_learned_rules_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / "learned_rules.json"

    def load_learned_rules(self, repo_name: str) -> Dict[str, Any]:
        """Load learned rules; returns versioned default if file absent."""
        return self._load_versioned_json(
            self.get_learned_rules_file(repo_name), DEFAULT_LEARNED_RULES
        )

    def save_learned_rules(self, repo_name: str, data: Dict[str, Any]) -> None:
        """Save learned rules atomically."""
        self._save_versioned_json(
            self.get_learned_rules_file(repo_name), data, DEFAULT_LEARNED_RULES
        )

    # --- review_publish_state.json ---

    def get_review_publish_state_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / "review_publish_state.json"

    def load_review_publish_state(self, repo_name: str) -> Dict[str, Any]:
        """Load publish state; returns versioned default if file absent."""
        return self._load_versioned_json(
            self.get_review_publish_state_file(repo_name), DEFAULT_REVIEW_PUBLISH_STATE
        )

    def save_review_publish_state(self, repo_name: str, data: Dict[str, Any]) -> None:
        """Save publish state atomically."""
        self._save_versioned_json(
            self.get_review_publish_state_file(repo_name),
            data,
            DEFAULT_REVIEW_PUBLISH_STATE,
        )

    # --- monitored_safety_state.json (Phase G7) ---

    def get_monitored_safety_state_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / "monitored_safety_state.json"

    def load_monitored_safety_state(self, repo_name: str) -> Dict[str, Any]:
        """Load monitored safety state; returns versioned default if file absent."""
        return self._load_versioned_json(
            self.get_monitored_safety_state_file(repo_name),
            DEFAULT_MONITORED_SAFETY_STATE,
        )

    def save_monitored_safety_state(self, repo_name: str, data: Dict[str, Any]) -> None:
        """Save monitored safety state atomically."""
        self._save_versioned_json(
            self.get_monitored_safety_state_file(repo_name),
            data,
            DEFAULT_MONITORED_SAFETY_STATE,
        )

    # --- fix_patterns.jsonl ---

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
