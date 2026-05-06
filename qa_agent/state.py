#!/usr/bin/env python3
"""State management for QA Agent."""

import copy
import fcntl
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import Finding, Run, generate_id, now_iso


def _atomic_json_write(path: Path, data: Any) -> None:
    """Write JSON data atomically: temp file + rename, same filesystem.

    This avoids leaving a partial/corrupted file if the process is
    interrupted mid-write. The rename is atomic on POSIX systems.

    Uses fcntl.flock(LOCK_EX) to prevent concurrent cycle collisions
    (both cycles writing .tmp + os.replace simultaneously).
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    lock_path = path.with_suffix(path.suffix + '.lock')
    with open(lock_path, 'w') as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


# ——— JSONL rotation ———
# review_events.jsonl and feedback_events.jsonl accumulate indefinitely.
# Rotate by archiving the current file when it exceeds the threshold.
_EVENT_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_EVENT_LOG_MAX_LINES = 10_000  # whichever comes first


def _rotate_jsonl_if_needed(path: Path) -> None:
    """Rotate a JSONL file if it exceeds size or line thresholds."""
    if not path.exists():
        return
    if path.stat().st_size < _EVENT_LOG_MAX_BYTES:
        # Check line count only if size threshold not reached
        try:
            with open(path) as f:
                line_count = sum(1 for _ in f)
            if line_count < _EVENT_LOG_MAX_LINES:
                return
        except Exception:
            return
    # Rotate: rename current file to .bak, start fresh
    bak_path = path.with_suffix(path.suffix + '.bak')
    try:
        os.replace(str(path), str(bak_path))
    except Exception:
        pass
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(tmp, path)


DEFAULT_ACTIVE_PRS_STATE = {
    'version': 1,
    'updated_at': None,
    'prs': {},
}

DEFAULT_REVIEW_STATE = {
    'version': 1,
    'updated_at': None,
    'prs': {},
}

# --- Phase B2: Autonomous-review state surface defaults ---

DEFAULT_REVIEW_RUN = {
    'version': 1,
    'run_id': '',
    'repo': '',
    'pr_number': None,
    'started_at': None,
    'ended_at': None,
    'status': 'pending',   # pending | running | completed | failed
    'findings_count': 0,
    'publish_status': 'none',  # none | published | failed | skipped
    'error': None,
}

DEFAULT_REVIEW_FINDINGS_MANIFEST = {
    'version': 1,
    'updated_at': None,
    'total_findings': 0,
}

DEFAULT_LEARNED_RULES = {
    'version': 1,
    'updated_at': None,
    'rules': [],          # list of learned rule objects
    'active_count': 0,
    'tentative_count': 0,
}

DEFAULT_REVIEW_PUBLISH_STATE = {
    'version': 1,
    'updated_at': None,
    'findings': {},       # { finding_id: publish_entry }
    'runs': {},           # { run_id: run_publish_entry }
}

# Phase G7: Monitored-rollout safety state default
DEFAULT_MONITORED_SAFETY_STATE = {
    'version': 1,
    'updated_at': None,
    'circuit_open': False,
    'failure_count': 0,
    'cooldown_until': None,
    'last_failure_at': None,
    'last_failure_reason': '',
    'auto_rollback_active': False,
    'auto_rollback_reason': '',
    'auto_rollback_triggered_at': None,
}

DEFAULT_FEEDBACK_EVENT = {
    'version': 1,
    'timestamp': None,
    'source': None,        # e.g. 'github_review_comment', 'github_review_thread'
    'pr_number': None,
    'finding_id': None,
    'signal': None,       # 'positive' | 'negative' | 'conflict' | 'request_change' | 'approve' | 'comment'
    'normalized': False,
    'payload': {},
}


class StateManager:
    """Manages persistent state for the agent."""
    
    def __init__(self, repos_dir: Path):
        self.repos_dir = Path(repos_dir)
    
    def _get_repo_dir(self, repo_name: str) -> Path:
        return self.repos_dir / repo_name
    
    def _get_state_dir(self, repo_name: str) -> Path:
        return self._get_repo_dir(repo_name) / 'state'
    
    # === Findings ===
    
    def get_findings_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / 'findings.jsonl'
    
    def load_findings(self, repo_name: str) -> List[Finding]:
        """Load all findings for a repo."""
        findings_file = self.get_findings_file(repo_name)
        if not findings_file.exists():
            return []
        
        findings = []
        with open(findings_file) as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    findings.append(Finding.from_dict(data))
        
        return findings
    
    def append_findings(self, repo_name: str, findings: List[Finding]) -> int:
        """Append findings to repo's findings file."""
        findings_file = self.get_findings_file(repo_name)
        findings_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Load existing IDs to avoid duplicates
        existing_ids = set()
        if findings_file.exists():
            with open(findings_file) as f:
                for line in f:
                    if line.strip():
                        data = json.loads(line)
                        existing_ids.add(data.get('finding_id'))
        
        # Append new findings
        written = 0
        with open(findings_file, 'a') as f:
            for finding in findings:
                if finding.finding_id not in existing_ids:
                    f.write(json.dumps(finding.to_dict()) + '\n')
                    written += 1
        
        return written
    
    def clear_findings(self, repo_name: str) -> None:
        """Clear findings file."""
        findings_file = self.get_findings_file(repo_name)
        if findings_file.exists():
            findings_file.unlink()
    
    # === Issues ===
    
    def get_issues_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / 'issues.json'
    
    def load_issues(self, repo_name: str) -> Dict[str, Any]:
        """Load issues for a repo."""
        issues_file = self.get_issues_file(repo_name)
        if not issues_file.exists():
            return {'issues': []}
        
        with open(issues_file) as f:
            return json.load(f)
    
    def save_issues(self, repo_name: str, issues: Dict[str, Any]) -> None:
        """Save issues for a repo."""
        issues_file = self.get_issues_file(repo_name)
        issues_file.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(issues_file, issues)
    
    # === State ===
    
    def get_state_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / 'state.json'
    
    def load_state(self, repo_name: str) -> Dict[str, Any]:
        """Load runner state for a repo."""
        state_file = self.get_state_file(repo_name)
        if not state_file.exists():
            return {
                'open_issues': 0,
                'open_prs': 0,
                'created': [],
                'finding_activity': {},
            }
        
        with open(state_file) as f:
            return json.load(f)
    
    def save_state(self, repo_name: str, state: Dict[str, Any]) -> None:
        """Save runner state for a repo."""
        state_file = self.get_state_file(repo_name)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(state_file, state)

    # === Review care ===

    def get_active_prs_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / 'active_prs.json'

    def load_active_prs(self, repo_name: str) -> Dict[str, Any]:
        path = self.get_active_prs_file(repo_name)
        if not path.exists():
            return copy.deepcopy(DEFAULT_ACTIVE_PRS_STATE)
        with open(path) as f:
            data = json.load(f)
        data.setdefault('version', 1)
        data.setdefault('updated_at', None)
        data.setdefault('prs', {})
        return data

    def save_active_prs(self, repo_name: str, data: Dict[str, Any]) -> None:
        path = self.get_active_prs_file(repo_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(DEFAULT_ACTIVE_PRS_STATE)
        payload.update(data or {})
        payload['updated_at'] = now_iso()
        _atomic_json_write(path, payload)

    def get_review_state_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / 'review_state.json'

    def load_review_state(self, repo_name: str) -> Dict[str, Any]:
        path = self.get_review_state_file(repo_name)
        if not path.exists():
            return copy.deepcopy(DEFAULT_REVIEW_STATE)
        with open(path) as f:
            data = json.load(f)
        data.setdefault('version', 1)
        data.setdefault('updated_at', None)
        data.setdefault('prs', {})
        return data

    def save_review_state(self, repo_name: str, data: Dict[str, Any]) -> None:
        path = self.get_review_state_file(repo_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(DEFAULT_REVIEW_STATE)
        payload.update(data or {})
        payload['updated_at'] = now_iso()
        _atomic_json_write(path, payload)

    def get_review_events_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / 'review_events.jsonl'

    def append_review_event(self, repo_name: str, event: Dict[str, Any]) -> None:
        path = self.get_review_events_file(repo_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'timestamp': now_iso(), **(event or {})}
        with open(path, 'a') as f:
            f.write(json.dumps(payload) + '\n')
        _rotate_jsonl_if_needed(path)

    def get_review_locks_dir(self, repo_name: str) -> Path:
        path = self._get_state_dir(repo_name) / 'review_locks'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_review_prompts_dir(self, repo_name: str) -> Path:
        path = self._get_state_dir(repo_name) / 'review_prompts'
        path.mkdir(parents=True, exist_ok=True)
        return path

    # === Phase B2: Autonomous-review state surfaces ===

    # --- review_runs/ (per-run JSON files) ---

    def get_review_runs_dir(self, repo_name: str) -> Path:
        path = self._get_state_dir(repo_name) / 'review_runs'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_review_run(self, repo_name: str, run_data: Dict[str, Any]) -> Path:
        """Save an autonomous-review run record."""
        run_id = run_data.get('run_id') or run_data.get('id')
        if not run_id:
            raise ValueError("review run data must contain 'run_id' or 'id'")
        path = self.get_review_runs_dir(repo_name) / f"{run_id}.json"
        payload = dict(DEFAULT_REVIEW_RUN)
        payload.update(run_data or {})
        if not payload.get('run_id'):
            payload['run_id'] = run_id
        payload['updated_at'] = now_iso()
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
        for p in runs_dir.glob('*.json'):
            with open(p) as f:
                data = json.load(f)
            for k, v in DEFAULT_REVIEW_RUN.items():
                data.setdefault(k, v)
            mtime = p.stat().st_mtime
            # Use started_at as primary key (iso string sorts correctly chronologically)
            # Tiebreak on mtime (run2's file has strictly later mtime than run1's)
            run_files.append((data.get('started_at') or '', -mtime, data))
        # Sort: primary key desc (newest first), secondary key desc (later mtime first)
        run_files.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [item[2] for item in run_files[:limit]]

    # --- review_findings.jsonl + review_findings/<finding_id>.json ---

    def get_review_findings_file(self, repo_name: str) -> Path:
        """Path to the review findings JSONL manifest/index."""
        return self._get_state_dir(repo_name) / 'review_findings.jsonl'

    def get_review_finding_file(self, repo_name: str, finding_id: str) -> Path:
        """Path to an individual review finding JSON file."""
        d = self._get_state_dir(repo_name) / 'review_findings'
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{finding_id}.json"

    def append_review_findings(self, repo_name: str, findings: List[Dict[str, Any]]) -> int:
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
                        existing_ids.add(json.loads(line).get('finding_id'))

        written = 0
        with open(findings_file, 'a') as f:
            for finding in findings:
                fid = finding.get('finding_id')
                if fid and fid not in existing_ids:
                    f.write(json.dumps(finding) + '\n')
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

    def save_review_finding(self, repo_name: str, finding_id: str, data: Dict[str, Any]) -> Path:
        """Save an individual review finding as a standalone JSON file."""
        path = self.get_review_finding_file(repo_name, finding_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(data)
        payload.setdefault('finding_id', finding_id)
        payload.setdefault('version', 1)
        payload.setdefault('saved_at', now_iso())
        _atomic_json_write(path, payload)
        return path

    def load_review_finding(self, repo_name: str, finding_id: str) -> Optional[Dict[str, Any]]:
        """Load a specific review finding by ID."""
        path = self.get_review_finding_file(repo_name, finding_id)
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    # --- feedback_events.jsonl ---

    def get_feedback_events_file(self, repo_name: str) -> Path:
        """Path to the feedback events JSONL log."""
        return self._get_state_dir(repo_name) / 'feedback_events.jsonl'

    def append_feedback_event(self, repo_name: str, event: Dict[str, Any]) -> None:
        """Append a feedback event to the JSONL log."""
        path = self.get_feedback_events_file(repo_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(DEFAULT_FEEDBACK_EVENT)
        payload.update(event or {})
        if not payload.get('timestamp'):
            payload['timestamp'] = now_iso()
        payload['version'] = 1
        with open(path, 'a') as f:
            f.write(json.dumps(payload) + '\n')
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

    # --- learned_rules.json ---

    def get_learned_rules_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / 'learned_rules.json'

    def load_learned_rules(self, repo_name: str) -> Dict[str, Any]:
        """Load learned rules; returns versioned default if file absent."""
        path = self.get_learned_rules_file(repo_name)
        if not path.exists():
            return copy.deepcopy(DEFAULT_LEARNED_RULES)
        with open(path) as f:
            data = json.load(f)
        for k, v in DEFAULT_LEARNED_RULES.items():
            data.setdefault(k, copy.deepcopy(v))
        return data

    def save_learned_rules(self, repo_name: str, data: Dict[str, Any]) -> None:
        """Save learned rules atomically."""
        path = self.get_learned_rules_file(repo_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(DEFAULT_LEARNED_RULES)
        payload.update(data or {})
        payload['updated_at'] = now_iso()
        _atomic_json_write(path, payload)

    # --- review_publish_state.json ---

    def get_review_publish_state_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / 'review_publish_state.json'

    def load_review_publish_state(self, repo_name: str) -> Dict[str, Any]:
        """Load publish state; returns versioned default if file absent."""
        path = self.get_review_publish_state_file(repo_name)
        if not path.exists():
            return copy.deepcopy(DEFAULT_REVIEW_PUBLISH_STATE)
        with open(path) as f:
            data = json.load(f)
        for k, v in DEFAULT_REVIEW_PUBLISH_STATE.items():
            data.setdefault(k, copy.deepcopy(v))
        return data

    def save_review_publish_state(self, repo_name: str, data: Dict[str, Any]) -> None:
        """Save publish state atomically."""
        path = self.get_review_publish_state_file(repo_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(DEFAULT_REVIEW_PUBLISH_STATE)
        payload.update(data or {})
        payload['updated_at'] = now_iso()
        _atomic_json_write(path, payload)

    # --- monitored_safety_state.json (Phase G7) ---

    def get_monitored_safety_state_file(self, repo_name: str) -> Path:
        return self._get_state_dir(repo_name) / 'monitored_safety_state.json'

    def load_monitored_safety_state(self, repo_name: str) -> Dict[str, Any]:
        """Load monitored safety state; returns versioned default if file absent."""
        path = self.get_monitored_safety_state_file(repo_name)
        if not path.exists():
            return copy.deepcopy(DEFAULT_MONITORED_SAFETY_STATE)
        with open(path) as f:
            data = json.load(f)
        for k, v in DEFAULT_MONITORED_SAFETY_STATE.items():
            data.setdefault(k, copy.deepcopy(v))
        return data

    def save_monitored_safety_state(self, repo_name: str, data: Dict[str, Any]) -> None:
        """Save monitored safety state atomically."""
        path = self.get_monitored_safety_state_file(repo_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(DEFAULT_MONITORED_SAFETY_STATE)
        payload.update(data or {})
        payload['updated_at'] = now_iso()
        _atomic_json_write(path, payload)

    # === Runs ===
    
    def get_runs_dir(self, repo_name: str) -> Path:
        runs_dir = self._get_repo_dir(repo_name) / 'runs'
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
        for run_file in sorted(runs_dir.glob('*.json'), reverse=True)[:limit]:
            with open(run_file) as f:
                runs.append(json.load(f))
        
        return runs
    
    # === Baselines ===
    
    def get_baselines_dir(self, repo_name: str) -> Path:
        baselines_dir = self._get_repo_dir(repo_name) / 'baselines'
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
        
        return [f.stem for f in baselines_dir.glob('*.json')]
