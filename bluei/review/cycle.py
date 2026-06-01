#!/usr/bin/env python3
"""Review cycle engine for QA Agent.

Manages observation (read-only) and autonomous (fix + PR) review modes.
Provides GitHub PR snapshot fetching, review-comment classification, worktree
remediation, finding normalization/deduplication, and a guarded live-publish
pipeline with circuit-breaker safety and learned-rule suppression.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

from bluei.review.models import (
    FindingSource,
    FindingActionability,
    FindingSeverity,
    PublishStatus,
    MonitoredSafetyState,
    normalize_finding_path,
    normalize_finding_header,
    make_finding_fingerprint,
    make_review_finding_id,
)
from bluei.app.models import (
    Repo,
    RepoConfig,
    ReviewMode,
    LiveRolloutMode,
    now_iso,
    generate_id,
)
from bluei.app.state import StateManager
from bluei.review.types import (
    ReviewCycleResult,
    PublishFilterResult,
    CandidateValidationError,
)
from bluei.review.provider import GitHubReviewProvider, GRAPHQL_QUERY
from bluei.review.chunking import (
    ChunkManifest,
    build_chunk_manifest,
    order_files_for_chunking,
)
from bluei.review.normalization import (
    normalize_candidate,
    assign_finding_identity,
    dedupe_findings,
)
from bluei.review.eligibility import (
    RemediationEligibility,
    is_remediation_eligible,
    _DEFAULT_MIN_CONFIDENCE,
    _DEFAULT_MIN_ACTIONABILITY,
)
from bluei.review.rules import (
    _classify_pattern_risk,
    _get_learned_rules_state,
    _save_learned_rules_state,
    _build_learned_rules_payload,
    _make_learned_rule_id,
    _check_rule_conflicts,
    _should_activate_tentative_rule,
    _propose_learned_rule_from_finding,
    _increment_rule_evidence,
    _activate_tentative_rule,
    _suppress_finding_with_rule,
    _process_learned_rules_for_run,
)
from bluei.review.feedback import (
    normalize_feedback,
    record_feedback,
    inject_feedback_for_autonomous_review,
    _flush_injected_feedback,
    _classify_text_sentiment,
    _normalize_reaction_signal,
)
from bluei.app.pattern_confidence import adjust_from_feedback
from bluei.review.publisher import (
    _build_pass_filter_result,
    ReconciliationResult,
    reconcile_publish_state,
    build_publish_entry,
    compute_run_publish_status,
    build_run_publish_entry,
    COMMENT_MAX_LINES,
    build_review_summary_comment,
)

from bluei.review.safety import SafetyMixin
from bluei.review.worktree import WorktreeMixin
from bluei.review.generation import GenerationMixin
from bluei.review.observation import ObservationMixin
from bluei.review.autonomous import AutonomousMixin


class ReviewCycleEngine(
    SafetyMixin,
    WorktreeMixin,
    GenerationMixin,
    ObservationMixin,
    AutonomousMixin,
):
    """Review cycle engine with observation + remediation planning groundwork."""

    def __init__(self, repo: Repo, state: StateManager):
        self.repo = repo
        self.state = state
        self.provider = GitHubReviewProvider(repo, state)

    def _prompt_path(self, pr_number: int) -> Path:
        return (
            self.state.get_review_prompts_dir(self.repo.config.name)
            / f"pr-{pr_number}.md"
        )

    def _review_worktree_root(self) -> Path:
        path = self.state._get_repo_dir(self.repo.config.name) / "worktrees"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _review_worktree_path(self, pr_number: int) -> Path:
        return self._review_worktree_root() / f"review-pr-{pr_number}"

    def _review_lock_path(self, pr_number: int) -> Path:
        return (
            self.state.get_review_locks_dir(self.repo.config.name)
            / f"pr-{pr_number}.lock"
        )

    def _acquire_pr_lock(self, pr_number: int):
        """Acquire an exclusive file lock for a PR; returns file handle or None."""
        path = self._review_lock_path(pr_number)
        handle = open(path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(f"locked_at={now_iso()} pr={pr_number}\n")
            handle.flush()
            return handle
        except BlockingIOError:
            handle.close()
            return None

    def _release_pr_lock(self, handle) -> None:
        if not handle:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _run_repo_cmd(
        self, cmd: List[str], cwd: Optional[Path] = None, check: bool = True
    ) -> str:
        """Run a subprocess in the repo directory, optionally raising on failure."""
        result = subprocess.run(
            cmd,
            cwd=str(cwd or self.provider.repo_path),
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                (result.stderr or result.stdout or "").strip()
                or f"command failed: {cmd}"
            )
        return (result.stdout or "").strip()

    def _build_review_cycle_comment(
        self,
        pr_number: int,
        snapshot: Dict[str, Any],
        status: str,
        merge_readiness: Dict[str, Any],
        execution_result: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Render the markdown review-cycle comment for a PR."""
        actionable_comments = snapshot.get("actionable_comments") or []
        informational_comments = snapshot.get("informational_comments") or []
        active_change_requesters = snapshot.get("active_change_requesters") or []
        merge_reason = str(merge_readiness.get("reason") or "")
        merge_state = str(merge_readiness.get("state") or "unknown")
        review_decision = str(snapshot.get("review_decision") or "UNKNOWN")
        merge_state_status = str(snapshot.get("merge_state_status") or "UNKNOWN")
        publication_key = f"{snapshot.get('fingerprint', 'no-fingerprint')}:{status}"

        def _render_comment_lines(items: List[Dict[str, Any]], title: str) -> List[str]:
            if not items:
                return [f"- **{title}:** none"]
            lines = [f"- **{title}:** {len(items)}"]
            for item in items[:5]:
                author = item.get("author") or "unknown"
                body = " ".join(str(item.get("body") or "").split())
                if len(body) > 180:
                    body = body[:177] + "..."
                lines.append(f"  - @{author}: {body}")
            if len(items) > 5:
                lines.append(f"  - ...and {len(items) - 5} more")
            return lines

        lines = [
            f"<!-- bluei-review-cycle: pr={pr_number} key={publication_key} -->",
            f"## bluei Review, PR #{pr_number}",
            "",
            f"- **Review status:** `{status}`",
            f"- **GitHub review decision:** `{review_decision}`",
            f"- **Merge state:** `{merge_state_status}`",
            f"- **QA merge readiness:** `{merge_state}`",
            f"- **Assessment:** {merge_reason}",
            f"- **Active change requesters:** {', '.join('@' + user for user in active_change_requesters) if active_change_requesters else 'none'}",
            f"- **Actionable review comments:** {len(actionable_comments)}",
            f"- **Informational review comments:** {len(informational_comments)}",
        ]
        if execution_result:
            lines.extend(
                [
                    f"- **Execution result:** `{execution_result.get('status') or 'unknown'}`",
                    f"- **Validation ok:** {bool((execution_result.get('validation') or {}).get('ok'))}",
                    f"- **Changed files:** {len(execution_result.get('changed_files') or [])}",
                ]
            )
        lines.extend(["", "### Review details", ""])
        lines.extend(_render_comment_lines(actionable_comments, "Actionable findings"))
        lines.append("")
        lines.extend(
            _render_comment_lines(informational_comments, "Informational findings")
        )
        return "\n".join(lines).strip()

    def _publish_review_cycle_comment(
        self,
        pr_number: int,
        summary_text: str,
        publication_key: str,
        existing_review: Dict[str, Any],
    ) -> Optional[str]:
        """Post a review-cycle comment to GitHub, skipping if unchanged.

        Args:
            pr_number: PR number to comment on.
            summary_text: Markdown comment body.
            publication_key: Fingerprint-based key for deduplication.
            existing_review: Prior review record with last comment metadata.

        Returns:
            Comment URL on success, existing URL if skipped, or None on failure.
        """
        if not self.repo.config.github.get("live_actions", False):
            return None
        if existing_review.get(
            "last_review_comment_key"
        ) == publication_key and existing_review.get("last_review_comment_url"):
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "pr_number": pr_number,
                    "event": "review_comment_skipped_unchanged",
                    "provider": "github",
                    "details": {
                        "publication_key": publication_key,
                        "comment_url": existing_review.get("last_review_comment_url"),
                    },
                },
            )
            return str(existing_review.get("last_review_comment_url"))

        result = subprocess.run(
            [
                "gh",
                "pr",
                "comment",
                str(pr_number),
                "--repo",
                self.provider.repo_slug,
                "--body",
                summary_text,
            ],
            cwd=str(self.provider.repo_path),
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip() or (
                f"gh-pr-comment-failed (code {result.returncode})"
            )
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "pr_number": pr_number,
                    "event": "review_comment_failed",
                    "provider": "github",
                    "details": {
                        "publication_key": publication_key,
                        "error": error,
                    },
                },
            )
            return None

        comment_url = (result.stdout or "").strip() or None
        self.state.append_review_event(
            self.repo.config.name,
            {
                "pr_number": pr_number,
                "event": "review_comment_published",
                "provider": "github",
                "details": {
                    "publication_key": publication_key,
                    "comment_url": comment_url,
                },
            },
        )
        return comment_url

    def _resolve_backend(self) -> str:
        """Select the first available fix backend from config preferences."""
        preferred = self.repo.config.fix_engine
        candidates: List[str] = []
        if preferred and preferred != "auto":
            candidates.append(preferred)
        for backend in self.repo.config.fallback_engines or [
            "claude",
            "opencode",
            "deterministic",
        ]:
            if backend not in candidates:
                candidates.append(backend)
        if "deterministic" not in candidates:
            candidates.append("deterministic")
        for backend in candidates:
            if backend == "deterministic":
                return backend
            if shutil.which(backend) is not None:
                return backend
        return "deterministic"

    def _render_backend_command(self, prompt_file: Path) -> str:
        """Render the shell command for the resolved backend and prompt file.

        Does NOT check binary availability — that's the caller's responsibility
        at execution time.  If a user has explicitly configured a template for a
        backend, we honour it even when the binary isn't currently on PATH.
        """
        backend = self.repo.config.fix_engine
        if backend and backend != "auto":
            if backend == "claude":
                template = self.repo.config.review_claude_template or (
                    "claude --dangerously-skip-permissions --print "
                    '"Read {prompt_file} and address the PR review feedback with the minimal safe change. '
                    'Run relevant checks, keep the diff small, and exit non-zero on failure."'
                )
                return template.format(prompt_file=str(prompt_file))
            if backend == "opencode":
                template = self.repo.config.review_opencode_template or (
                    'opencode run "Read {prompt_file} and address the PR review feedback with the minimal safe change. '
                    'Run relevant checks, keep the diff small, and exit non-zero on failure."'
                )
                return template.format(prompt_file=str(prompt_file))
        # Fall back to availability-based resolution
        backend = self._resolve_backend()
        if backend == "claude":
            template = self.repo.config.review_claude_template or (
                "claude --dangerously-skip-permissions --print "
                '"Read {prompt_file} and address the PR review feedback with the minimal safe change. '
                'Run relevant checks, keep the diff small, and exit non-zero on failure."'
            )
            return template.format(prompt_file=str(prompt_file))
        if backend == "opencode":
            template = self.repo.config.review_opencode_template or (
                'opencode run "Read {prompt_file} and address the PR review feedback with the minimal safe change. '
                'Run relevant checks, keep the diff small, and exit non-zero on failure."'
            )
            return template.format(prompt_file=str(prompt_file))
        return (
            f'python3 -c "print("deterministic backend placeholder for {prompt_file}")"'
        )

    def _mnemo_available(self) -> bool:
        repo_path = Path(self.repo.config.path)
        return (repo_path / ".mnemo" / "db" / "memory.db").exists()

    def _mnemo_query(self, query: str, limit: int = 5) -> str:
        """Best-effort Mnemo Engram query; returns text block or empty string. Never raises."""
        if not self._mnemo_available():
            return ""
        query = (query or "").strip()
        if not query:
            return ""
        try:
            result = subprocess.run(
                ["mnemo", "engram", "query", query, "--limit", str(limit)],
                cwd=str(self.provider.repo_path),
                text=True,
                capture_output=True,
                check=False,
                timeout=15,
            )
            if result.returncode != 0:
                return ""
            return (result.stdout or "").strip()[:2000]
        except Exception:
            return ""

    def _build_mnemo_review_context(
        self,
        *,
        snapshot: Optional[Dict[str, Any]] = None,
        pr_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Build a compact Mnemo context block for review-cycle prompts from snapshot and PR data."""
        if not self._mnemo_available():
            return ""

        queries: List[str] = []
        if snapshot:
            comments = [
                (item.get("body") or "").strip()
                for item in (snapshot.get("actionable_comments") or [])[:3]
                if (item.get("body") or "").strip()
            ]
            branch = (snapshot.get("branch") or "").strip()
            if branch:
                queries.append(branch)
            queries.extend(comments)

        if pr_context:
            branch = (pr_context.get("branch") or "").strip()
            if branch:
                queries.append(branch)
            for path in (pr_context.get("changed_files") or [])[:5]:
                value = str(path).strip()
                if value:
                    queries.append(value)

        seen: set[str] = set()
        blocks: List[str] = []
        for query in queries:
            key = query.lower()
            if key in seen:
                continue
            seen.add(key)
            result = self._mnemo_query(query)
            if result:
                blocks.append(f"### Mnemo query: `{query}`\n{result}")
            if len(blocks) >= 2:
                break

        if not blocks:
            return ""
        return "## Mnemo context\n\n" + "\n\n".join(blocks) + "\n"

    def _apply_mnemo_candidate_signals(
        self,
        findings: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Apply small confidence boosts when Mnemo corroborates a candidate finding."""
        if not findings or not self._mnemo_available():
            return findings

        updated: List[Dict[str, Any]] = []
        for finding in findings:
            path = str(finding.get("path") or "").strip()
            header = str(finding.get("header") or "").strip()
            snippet = str(finding.get("snippet") or "").strip()
            queries: List[str] = []
            if path:
                queries.append(path)
                stem = Path(path).stem.strip()
                if stem and stem.lower() != path.lower():
                    queries.append(stem)
            if header:
                queries.append(header)
            if snippet:
                queries.append(snippet[:80])

            support_hits = 0
            support_queries: List[str] = []
            seen: set[str] = set()
            for query in queries:
                key = query.lower()
                if key in seen:
                    continue
                seen.add(key)
                result = self._mnemo_query(query, limit=3)
                if result:
                    support_hits += 1
                    support_queries.append(query)
                if support_hits >= 2:
                    break

            boosted = dict(finding)
            base_confidence = float(boosted.get("confidence", 0.0))
            confidence_boost = min(0.15, support_hits * 0.07)
            boosted["mnemo_support_hits"] = support_hits
            boosted["mnemo_support_queries"] = support_queries
            boosted["confidence"] = min(
                1.0, round(base_confidence + confidence_boost, 4)
            )
            updated.append(boosted)

        return sorted(
            updated,
            key=lambda item: (
                float(item.get("confidence", 0.0)),
                int(item.get("mnemo_support_hits", 0)),
            ),
            reverse=True,
        )

    def _guess_validation_commands(self) -> List[List[str]]:
        """Infer test/lint/build commands from package.json or project layout."""
        commands: List[List[str]] = []
        package_json = self.provider.repo_path / "package.json"
        if (
            self.repo.config.language in {"typescript", "javascript"}
            and package_json.exists()
        ):
            try:
                pkg = json.loads(package_json.read_text())
                scripts = pkg.get("scripts", {})
                if "test" in scripts:
                    commands.append(["npm", "test"])
                if "lint" in scripts:
                    commands.append(["npm", "run", "lint"])
                if "build" in scripts:
                    commands.append(["npm", "run", "build"])
                if "typecheck" in scripts:
                    commands.append(["npm", "run", "typecheck"])
            except Exception:
                pass
        elif self.repo.config.language == "python":
            if (self.provider.repo_path / "tests").exists():
                commands.append(["pytest", "-q"])
        return commands


    def _get_review_mode(self) -> str:
        """Return the configured review mode, falling back to observation."""
        raw = self.repo.config.review_care.get("mode", ReviewMode.OBSERVATION.value)
        valid = {
            ReviewMode.OBSERVATION.value,
            ReviewMode.AUTONOMOUS_REVIEW.value,
            ReviewMode.REMEDIATION.value,
        }
        return raw if raw in valid else ReviewMode.OBSERVATION.value

    def run(
        self, dry_run: bool = True, allow_review_push: bool = False
    ) -> ReviewCycleResult:
        """Dispatch to the appropriate review cycle based on config mode.

        Args:
            dry_run: If True, observe only without side effects.
            allow_review_push: If True, allow committing and pushing fixes.

        Returns:
            ReviewCycleResult with PR and finding counters.
        """
        mode = self._get_review_mode()
        mnemo_avail = self._mnemo_available()
        _logger.info("review-cycle mode=%s mnemo_available=%s", mode, mnemo_avail)
        if mode == ReviewMode.AUTONOMOUS_REVIEW.value:
            return self._run_autonomous_review_cycle(dry_run, allow_review_push)
        elif mode == ReviewMode.REMEDIATION.value:
            return self._run_remediation_cycle(dry_run, allow_review_push)
        else:
            # Observation mode (including missing/invalid — backward-compatible)
            return self._run_observation_cycle(dry_run, allow_review_push)
