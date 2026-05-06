#!/usr/bin/env python3
"""Run engine - wraps sandbox_local_runner.py."""

import json
import subprocess
import os
import fcntl
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import re

from .models import Repo, RepoStatus, Run, generate_id, now_iso
from .registry import RepoRegistry
from .state import StateManager
from .health import HealthEngine
from .config import ConfigManager
from .review import ReviewCycleEngine


@dataclass
class RunOptions:
    """Options for a run."""
    phase: str = 'orchestrated'
    dry_run: bool = True
    fix_engine: Optional[str] = None
    allow_review_push: bool = False


@dataclass
class RunResult:
    """Result of a run."""
    run: Run
    success: bool
    output: str
    error: Optional[str] = None


class RunEngine:
    """Executes QA runs by wrapping sandbox_local_runner.py."""
    
    def __init__(self, 
                 registry: RepoRegistry,
                 state: StateManager,
                 health: HealthEngine,
                 config_manager: ConfigManager):
        self.registry = registry
        self.state = state
        self.health = health
        self.config = config_manager
        self.runner_path = config_manager.workspace / 'core' / 'sandbox_local_runner.py'
    
    def _lock_path(self, repo_name: str, phase: str) -> Path:
        """Return the lock file path for a repo/phase pair.

        review-cycle and merge-cycle share a lock to prevent state file races.
        """
        lock_dir = self.config.workspace / 'locks'
        lock_dir.mkdir(parents=True, exist_ok=True)
        # review-cycle and merge-cycle share a lock to prevent concurrent
        # read/write on review_state.json / active_prs.json
        shared_phases = {'review-cycle', 'merge-cycle'}
        lock_phase = 'review-merge' if phase in shared_phases else phase
        return lock_dir / f'{repo_name}-{lock_phase}.lock'

    def _acquire_lock(self, repo_name: str, phase: str):
        """Acquire a non-blocking lock; return file handle or None if already locked."""
        lock_path = self._lock_path(repo_name, phase)
        handle = open(lock_path, 'a+', encoding='utf-8')
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(f'pid={os.getpid()} started_at={now_iso()}\n')
            handle.flush()
            return handle
        except BlockingIOError:
            handle.close()
            return None

    def _release_lock(self, handle) -> None:
        """Release a previously acquired file lock."""
        if not handle:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
    
    def _template_for_backend(self, repo: Repo, backend: str) -> Optional[str]:
        config = repo.config
        if backend == 'claude':
            return config.claude_template or (
                'claude --dangerously-skip-permissions --print '
                '"Read {prompt_file} and apply the minimal safe fix for finding {finding_id}. '
                'Run relevant tests/build checks, keep the diff small, and exit non-zero on failure."'
            )
        if backend == 'opencode':
            return config.opencode_template or (
                'opencode run "Read {prompt_file} and apply the minimal safe fix for finding {finding_id}. '
                'Run relevant tests/build checks, keep the diff small, and exit non-zero on failure."'
            )
        return None

    def _backend_available(self, backend: str) -> bool:
        if backend == 'deterministic':
            return True
        if backend == 'claude':
            return shutil.which('claude') is not None
        if backend == 'opencode':
            return shutil.which('opencode') is not None
        return False

    def _resolve_backend(self, repo: Repo, requested_backend: Optional[str]) -> Dict[str, Optional[str]]:
        config = repo.config
        preferred = requested_backend or config.fix_engine

        if preferred and preferred not in {'auto', 'claude', 'opencode', 'deterministic'}:
            preferred = config.fix_engine

        if preferred == 'deterministic':
            return {'logical_backend': 'deterministic', 'runner_engine': 'deterministic', 'template': None}

        candidates: List[str] = []
        if preferred and preferred != 'auto':
            candidates.append(preferred)
        for backend in config.fallback_engines or ['claude', 'opencode', 'deterministic']:
            if backend not in candidates:
                candidates.append(backend)
        if 'deterministic' not in candidates:
            candidates.append('deterministic')

        for backend in candidates:
            if backend == 'deterministic':
                return {'logical_backend': 'deterministic', 'runner_engine': 'deterministic', 'template': None}
            if self._backend_available(backend):
                return {
                    'logical_backend': backend,
                    'runner_engine': 'claude',
                    'template': self._template_for_backend(repo, backend),
                }

        return {'logical_backend': 'deterministic', 'runner_engine': 'deterministic', 'template': None}

    def _build_cli_args(self, repo: Repo, options: RunOptions, backend: Optional[Dict[str, Optional[str]]] = None) -> List[str]:
        """Build CLI arguments for sandbox_local_runner.py."""
        config = repo.config
        repo_name = config.name
        backend = backend or self._resolve_backend(repo, options.fix_engine)
        
        args = [
            'python3',
            str(self.runner_path),
            '--repo-path', config.path,
            '--state-file', str(self.state.get_state_file(repo_name)),
            '--log-file', str(self.config.workspace / 'logs' / repo_name / 'run.log'),
            '--findings-file', str(self.state.get_findings_file(repo_name)),
            '--issues-file', str(self.state.get_issues_file(repo_name)),
            '--worktree-root', str(self.config.repos_dir / repo_name / 'worktrees'),
            '--status-file', str(self.state._get_state_dir(repo_name) / 'status.json'),
            '--docs-index-file', str(self.state._get_state_dir(repo_name) / 'docs_index.json'),
            '--run-phase', options.phase,
            
            # Limits
            '--open-issues-cap', str(config.limits.get('open_issues_cap', 20)),
            '--open-prs-cap', str(config.limits.get('open_prs_cap', 5)),
            '--max-prs-per-run', str(config.limits.get('max_prs_per_run', 2)),
            '--max-issues-per-run', str(config.limits.get('max_issues_per_run', 10)),
            '--max-files-changed', str(config.limits.get('max_files_changed', 5)),
            '--max-loc-diff', str(config.limits.get('max_loc_diff', 200)),
            '--max-fix-attempts-per-issue', str(config.limits.get('max_fix_attempts', 3)),
            
            # Cooldowns
            '--finding-cooldown-seconds', str(config.cooldowns.get('finding_seconds', 14400)),
            '--merge-cooldown-minutes', str(config.cooldowns.get('merge_minutes', 30)),
            '--staleness-threshold-seconds', str(config.cooldowns.get('staleness_seconds', 7200)),
            
            # Fix engine
            '--fix-engine', str(backend['runner_engine']),
        ]
        
        # Dry run
        if options.dry_run:
            args.append('--dry-run')
        else:
            args.append('--no-dry-run')
        
        # Command template for LLM-based backends
        if backend['runner_engine'] == 'claude' and backend.get('template'):
            args.extend(['--claude-cmd-template', str(backend['template'])])
        
        # GitHub flags
        if config.github.get('live_actions', False):
            args.append('--live-github-actions')
        if config.github.get('auto_merge', False):
            args.append('--auto-merge-sandbox')
        if config.github.get('auto_rebase', {}).get('enabled', True):
            args.append('--auto-rebase-enabled')
            rebase_max = config.github['auto_rebase'].get('max_prs_per_sweep', 5)
            args.extend(['--rebase-max-prs', str(rebase_max)])

        # Baseline checks (per-repo validation commands)
        if config.baseline_checks:
            args.extend(['--baseline-checks', json.dumps(config.baseline_checks)])

        return args
    
    def _parse_output(self, output: str) -> Dict[str, int]:
        """Parse sandbox_local_runner.py output for metrics."""
        metrics = {
            'findings_detected': 0,
            'issues_created': 0,
            'fix_attempts': 0,
            'fixes_verified': 0,
            'fixes_failed': 0,
            'prs_created': 0,
            'merges_completed': 0,
        }
        
        # Pattern: findings=N issues=M prs=P
        patterns = {
            'findings_detected': [r'findings[=:\s]+(\d+)', r'(\d+)\s+findings'],
            'issues_created': [r'issues?[=:\s]+(\d+)', r'created[=:\s]+(\d+)'],
            'fix_attempts': [r'fix_attempts?[=:\s]+(\d+)', r'attempts?[=:\s]+(\d+)'],
            'fixes_verified': [r'verified[=:\s]+(\d+)', r'fixes_verified[=:\s]+(\d+)'],
            'fixes_failed': [r'failed[=:\s]+(\d+)', r'fixes_failed[=:\s]+(\d+)'],
            'prs_created': [r'prs?[=:\s]+(\d+)', r'created[=:\s]+(\d+)\s+pr'],
            'merges_completed': [r'merges?[=:\s]+(\d+)', r'merged[=:\s]+(\d+)'],
        }
        
        output_lower = output.lower()
        for key, pattern_list in patterns.items():
            for pattern in pattern_list:
                match = re.search(pattern, output_lower)
                if match:
                    metrics[key] = int(match.group(1))
                    break
        
        return metrics

    def _run_review_cycle(self, repo: Repo, run: Run, log_dir: Path, dry_run: bool, allow_review_push: bool = False) -> RunResult:
        engine = ReviewCycleEngine(repo, self.state)
        result = engine.run(dry_run=dry_run, allow_review_push=allow_review_push)
        output = (
            f"review_cycle active_prs={result.active_prs} blocked_prs={result.blocked_prs} "
            f"retry_eligible_prs={result.retry_eligible_prs} retry_planned_prs={result.retry_planned_prs} "
            f"retry_prepared_prs={result.retry_prepared_prs} retry_executed_prs={result.retry_executed_prs} "
            f"retry_failed_prs={result.retry_failed_prs} retry_exhausted_prs={result.retry_exhausted_prs} "
            f"merge_ready_prs={result.merge_ready_prs} paused_prs={result.paused_prs}"
        )
        (log_dir / f'{run.id}.log').write_text(output + '\n', encoding='utf-8')

        run.ended_at = now_iso()
        start_dt = datetime.fromisoformat(run.started_at.replace('Z', '+00:00'))
        end_dt = datetime.fromisoformat(run.ended_at.replace('Z', '+00:00'))
        run.duration_seconds = int((end_dt - start_dt).total_seconds())
        run.prs_created = result.active_prs
        run.status = 'completed'

        findings = self.state.load_findings(repo.config.name)
        health_score = self.health.calculate(findings)
        run.health_before = repo.current_health_score
        run.health_after = health_score.score
        run.health_delta = health_score.score - repo.current_health_score

        self.registry.update(repo.config.name, {
            'status': RepoStatus.READY.value,
            'last_run_at': now_iso(),
            'current_findings_count': len(findings),
            'current_health_score': health_score.score,
            'total_prs': max(repo.total_prs, result.active_prs),
        })
        self.health.save_health_snapshot(
            repo.config.name,
            health_score,
            len(findings),
            self.state._get_state_dir(repo.config.name)
        )
        return RunResult(run=run, success=True, output=output)
    
    def run(self, repo: Repo, options: RunOptions) -> RunResult:
        """Execute a run for a repo."""
        repo_name = repo.config.name
        run_id = generate_id('run')
        
        resolved_backend = self._resolve_backend(repo, options.fix_engine)

        # Create run record
        run = Run(
            id=run_id,
            repo_id=repo.config.id,
            phase=options.phase,
            started_at=now_iso(),
            dry_run=options.dry_run,
            fix_engine=str(resolved_backend['logical_backend']),
            status='running',
        )
        
        # Build CLI args for sandbox-backed phases only
        args = self._build_cli_args(repo, options, resolved_backend) if options.phase != 'review-cycle' else []
        
        # Ensure log directory exists
        log_dir = self.config.workspace / 'logs' / repo_name
        log_dir.mkdir(parents=True, exist_ok=True)
        
        lock_handle = self._acquire_lock(repo_name, options.phase)
        if not lock_handle:
            run.status = 'skipped'
            run.error = f'Another {options.phase} run is already active for {repo_name}'
            run.ended_at = now_iso()
            self.state.save_run(repo_name, run)
            return RunResult(run=run, success=False, output='', error=run.error)

        # Update repo status
        self.registry.update(repo_name, {
            'status': RepoStatus.RUNNING.value,
        })
        
        try:
            if options.phase == 'review-cycle':
                return self._run_review_cycle(repo, run, log_dir, options.dry_run, options.allow_review_push)

            # Execute runner
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=3600,  # 1 hour timeout
                cwd=str(self.config.workspace),
            )
            
            output = result.stdout + '\n' + result.stderr
            (log_dir / f'{run_id}.log').write_text(output, encoding='utf-8')
            metrics = self._parse_output(output)
            
            # Update run record
            run.ended_at = now_iso()
            
            # Calculate duration
            start_dt = datetime.fromisoformat(run.started_at.replace('Z', '+00:00'))
            end_dt = datetime.fromisoformat(run.ended_at.replace('Z', '+00:00'))
            run.duration_seconds = int((end_dt - start_dt).total_seconds())
            
            run.findings_detected = metrics['findings_detected']
            run.issues_created = metrics['issues_created']
            run.fix_attempts = metrics['fix_attempts']
            run.fixes_verified = metrics['fixes_verified']
            run.fixes_failed = metrics['fixes_failed']
            run.prs_created = metrics['prs_created']
            run.merges_completed = metrics['merges_completed']
            run.status = 'completed' if result.returncode == 0 else 'failed'
            run.error = result.stderr[:500] if result.returncode != 0 else None
            
            # Recalculate health
            findings = self.state.load_findings(repo_name)
            health_score = self.health.calculate(findings)
            
            run.health_before = repo.current_health_score
            run.health_after = health_score.score
            run.health_delta = health_score.score - repo.current_health_score
            
            # Update repo state
            self.registry.update(repo_name, {
                'status': RepoStatus.READY.value,
                'last_run_at': now_iso(),
                'current_findings_count': len(findings),
                'current_health_score': health_score.score,
                'total_fixes': repo.total_fixes + run.fixes_verified,
                'total_prs': repo.total_prs + run.prs_created,
                'total_merges': repo.total_merges + run.merges_completed,
            })
            
            # Save health snapshot
            self.health.save_health_snapshot(
                repo_name,
                health_score,
                len(findings),
                self.state._get_state_dir(repo_name)
            )
            
            return RunResult(
                run=run,
                success=result.returncode == 0,
                output=output,
            )
            
        except subprocess.TimeoutExpired:
            run.status = 'timeout'
            run.error = 'Run exceeded 1 hour timeout'
            run.ended_at = now_iso()
            return RunResult(run=run, success=False, output='', error=run.error)
            
        except Exception as e:
            run.status = 'error'
            run.error = str(e)
            run.ended_at = now_iso()
            return RunResult(run=run, success=False, output='', error=run.error)
            
        finally:
            self._release_lock(lock_handle)

            # Save run record
            self.state.save_run(repo_name, run)
            
            # Update repo status
            self.registry.update(repo_name, {
                'status': RepoStatus.READY.value,
            })
    
    def dry_run(self, repo: Repo, phase: str = 'issue-cycle') -> RunResult:
        """Execute a dry run (preview only)."""
        return self.run(repo, RunOptions(phase=phase, dry_run=True))
    
    def get_run_history(self, repo_name: str, limit: int = 10) -> List[Dict]:
        """Get run history for a repo."""
        return self.state.list_runs(repo_name, limit)
