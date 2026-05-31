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


class ReviewCycleEngine:
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

    def _prepare_worktree(
        self, snapshot: Dict[str, Any], dry_run: bool
    ) -> Dict[str, Any]:
        """Create or reuse a git worktree checked out at the PR branch.

        Args:
            snapshot: PR snapshot with pr_number and branch.
            dry_run: If True, return metadata without creating the worktree.

        Returns:
            Dict with worktree_path, local_branch, prepared, and dry_run.
        """
        pr_number = int(snapshot["pr_number"])
        branch = str(snapshot.get("branch") or "")
        path = self._review_worktree_path(pr_number)
        local_branch = f"qa-review-pr-{pr_number}"
        pr_ref = f"refs/remotes/origin/pr/{pr_number}/head"
        if dry_run:
            return {
                "worktree_path": str(path),
                "local_branch": local_branch,
                "prepared": False,
                "dry_run": True,
            }
        if path.exists():
            # Validate existing worktree — verify it's on the correct branch.
            # If branch is wrong or worktree is stale, force-recreate.
            worktree_branch = self._get_worktree_branch(path)
            if worktree_branch != local_branch:
                _logger.warning(
                    f"worktree-stale: path={path} expected_branch={local_branch} "
                    f"actual_branch={worktree_branch} — force-recreating"
                )
                self._run_repo_cmd(
                    ["git", "worktree", "remove", "--force", str(path)],
                    cwd=self.provider.repo_path,
                )

        if not path.exists():
            fetched = False
            try:
                self._run_repo_cmd(
                    [
                        "git",
                        "fetch",
                        "origin",
                        f"pull/{pr_number}/head:{pr_ref}",
                    ],
                    cwd=self.provider.repo_path,
                )
                fetched = True
            except Exception:
                pass

            start_point = pr_ref
            if not fetched:
                self._run_repo_cmd(
                    ["git", "fetch", "origin", branch], cwd=self.provider.repo_path
                )
                start_point = f"origin/{branch}"

            try:
                self._run_repo_cmd(
                    [
                        "git",
                        "worktree",
                        "add",
                        "-B",
                        local_branch,
                        str(path),
                        start_point,
                    ],
                    cwd=self.provider.repo_path,
                )
            except Exception:
                if start_point != f"origin/{branch}" and branch:
                    self._run_repo_cmd(
                        ["git", "fetch", "origin", branch], cwd=self.provider.repo_path
                    )
                    self._run_repo_cmd(
                        [
                            "git",
                            "worktree",
                            "add",
                            "-B",
                            local_branch,
                            str(path),
                            f"origin/{branch}",
                        ],
                        cwd=self.provider.repo_path,
                    )
                else:
                    raise
        return {
            "worktree_path": str(path),
            "local_branch": local_branch,
            "prepared": True,
            "dry_run": False,
        }

    def _render_remediation_prompt(
        self, snapshot: Dict[str, Any], attempts_used: int
    ) -> str:
        """Render the markdown prompt instructing the backend to address review feedback."""
        actionables = snapshot.get("actionable_comments", [])
        mnemo_context = self._build_mnemo_review_context(snapshot=snapshot)
        lines = [
            "# QA-Agent Review Remediation Task",
            "",
            f"Repository: {self.repo.config.name}",
            f"PR: #{snapshot.get('pr_number')}",
            f"URL: {snapshot.get('pr_url')}",
            f"Branch: {snapshot.get('branch')}",
            f"Attempt: {attempts_used + 1}/{int(self.repo.config.review_care.get('max_attempts', 3))}",
            "",
            "## Actionable review feedback",
        ]
        if actionables:
            for item in actionables:
                lines.append(
                    f"- ({item.get('author', 'reviewer')}) {item.get('body', '')}"
                )
        else:
            lines.append(
                "- Reviewer requested changes; inspect unresolved review context carefully."
            )
        lines.extend(
            [
                "",
                "## Constraints",
                "- Make the smallest safe change that resolves the review feedback.",
                "- Preserve existing behavior unless the review feedback explicitly requires change.",
                "- Run relevant tests/build/type checks before finishing.",
                "- Do NOT rebase or force-push.",
                "- Exit non-zero if the feedback cannot be resolved safely.",
                "",
                "## Current mode",
                "- This prompt is generated as part of bounded review remediation planning.",
            ]
        )
        if mnemo_context:
            lines.extend(["", mnemo_context.rstrip()])
        return "\n".join(lines) + "\n"

    def _plan_remediation(
        self, snapshot: Dict[str, Any], review_record: Dict[str, Any], dry_run: bool
    ) -> Optional[Dict[str, Any]]:
        """Prepare a remediation plan: prompt file, backend command, and worktree.

        Args:
            snapshot: PR snapshot dict.
            review_record: Prior review state with attempts_used.
            dry_run: If True, plan without creating files or worktrees.

        Returns:
            Plan dict with status, prompt_file, backend_command, and worktree info.
        """
        attempts_used = int(review_record.get("attempts_used", 0))
        prompt_path = self._prompt_path(int(snapshot["pr_number"]))
        if not dry_run:
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(
                self._render_remediation_prompt(snapshot, attempts_used),
                encoding="utf-8",
            )
        backend = self._render_backend_command(prompt_path)
        worktree = self._prepare_worktree(snapshot, dry_run=dry_run)
        return {
            "status": "retry_prepared" if worktree.get("prepared") else "retry_planned",
            "prompt_file": str(prompt_path),
            "planned_at": snapshot["fetched_at"],
            "attempt_number": attempts_used + 1,
            "backend_command": backend,
            "worktree": worktree,
        }

    def _run_shell(
        self, command: str, cwd: Path, timeout_seconds: int = 900
    ) -> Dict[str, Any]:
        """Execute a shell command with a timeout, capturing stdout/stderr."""
        try:
            result = subprocess.run(
                ["bash", "-lc", command],
                cwd=str(cwd),
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
            return {
                "returncode": result.returncode,
                "stdout": (result.stdout or "").strip(),
                "stderr": (result.stderr or "").strip(),
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "returncode": 124,
                "stdout": (exc.stdout or "").strip()
                if isinstance(exc.stdout, str)
                else "",
                "stderr": (exc.stderr or "").strip()
                if isinstance(exc.stderr, str)
                else "",
                "timed_out": True,
            }

    def _collect_changed_files(self, cwd: Path) -> List[str]:
        """Return a list of changed file paths from git status --porcelain."""
        out = self._run_repo_cmd(["git", "status", "--porcelain"], cwd=cwd, check=False)
        files: List[str] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            files.append(line[3:] if len(line) > 3 else line)
        return files

    def _run_git_result(self, args: List[str], cwd: Path) -> Dict[str, Any]:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "command": ["git", *args],
            "returncode": result.returncode,
            "stdout": (result.stdout or "").strip()[-2000:],
            "stderr": (result.stderr or "").strip()[-2000:],
        }

    def _get_worktree_branch(self, worktree_path: Path) -> str:
        """Return the branch that the given worktree is currently on, or empty string."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(worktree_path),
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    def _cleanup_worktree(self, worktree_path: Path) -> Dict[str, Any]:
        """Force-remove a git worktree and return the result."""
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=str(self.provider.repo_path),
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "returncode": result.returncode,
            "stdout": (result.stdout or "").strip()[-2000:],
            "stderr": (result.stderr or "").strip()[-2000:],
            "removed": result.returncode == 0,
        }

    def _apply_commit_push_boundary(
        self,
        worktree_path: Path,
        snapshot: Dict[str, Any],
        changed_files: List[str],
        allow_review_push: bool,
    ) -> Dict[str, Any]:
        """
        Commit and optionally push changed files to the PR branch.

        When allow_review_push is False (default), changes are held in
        pending_operator_confirmation state and never pushed.

        Args:
            worktree_path: Path to the worktree with staged changes.
            snapshot: PR snapshot for branch and PR number.
            changed_files: List of file paths to commit.
            allow_review_push: If True, commit and push immediately.

        Returns:
            Dict with status (pushed/pending_operator_confirmation/no_changes/etc.).
        """
        branch = str(snapshot.get("branch") or "")
        result: Dict[str, Any] = {
            "status": "pending_operator_confirmation",
            "allow_review_push": allow_review_push,
            "target_branch": branch,
            "changed_files": list(changed_files),
        }
        if not allow_review_push:
            return result
        if not changed_files:
            result["status"] = "no_changes"
            return result

        add_result = self._run_git_result(
            ["add", "--", *changed_files], cwd=worktree_path
        )
        result["git_add"] = add_result
        if add_result["returncode"] != 0:
            result["status"] = "commit_failed"
            return result

        commit_message = (
            f"bluei: address review feedback for PR #{int(snapshot['pr_number'])}"
        )
        commit_result = self._run_git_result(
            ["commit", "-m", commit_message], cwd=worktree_path
        )
        result["git_commit"] = commit_result
        if commit_result["returncode"] != 0:
            result["status"] = "commit_failed"
            return result

        push_result = self._run_git_result(
            ["push", "origin", f"HEAD:{branch}"], cwd=worktree_path
        )
        result["git_push"] = push_result
        if push_result["returncode"] != 0:
            result["status"] = "push_failed"
            return result

        result["status"] = "pushed"
        if self.repo.config.review_care.get("cleanup_worktrees_after_push", True):
            result["cleanup"] = self._cleanup_worktree(worktree_path)
        return result

    def _run_validation(self, worktree_path: Path) -> Dict[str, Any]:
        """Run configured or guessed test/lint/build commands in the worktree."""
        commands = self.repo.config.baseline_checks or self._guess_validation_commands()
        if not commands:
            return {"ok": True, "results": [], "reason": "no-validation-configured"}
        results = []
        ok = True
        for cmd in commands:
            result = subprocess.run(
                cmd, cwd=str(worktree_path), text=True, capture_output=True, check=False
            )
            results.append(
                {
                    "command": cmd,
                    "returncode": result.returncode,
                    "stdout": (result.stdout or "").strip()[-2000:],
                    "stderr": (result.stderr or "").strip()[-2000:],
                }
            )
            if result.returncode != 0:
                ok = False
                break
        return {"ok": ok, "results": results, "reason": "completed"}

    def _execute_prepared_remediation(
        self,
        remediation_plan: Dict[str, Any],
        review_record: Dict[str, Any],
        dry_run: bool,
        allow_review_push: bool = False,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a prepared remediation plan.

        By default (allow_review_push=False), validated changes enter
        retry_pending_push state and are not pushed to the remote.

        Args:
            remediation_plan: Plan dict from _plan_remediation.
            review_record: Prior review state with attempts_used.
            dry_run: If True, return plan status without execution.
            allow_review_push: If True, commit and push on success.
            snapshot: PR snapshot for commit/push metadata.

        Returns:
            Dict with status, execution details, validation, and push result.
        """
        if not remediation_plan:
            return {"status": "no_plan"}
        attempts_used_prior = int(review_record.get("attempts_used", 0))
        max_attempts = int(self.repo.config.review_care.get("max_attempts", 3))
        if attempts_used_prior >= max_attempts:
            return {
                "status": "retry_exhausted",
                "executed": False,
                "attempts_used": attempts_used_prior,
            }
        worktree_path = Path(
            (remediation_plan.get("worktree") or {}).get("worktree_path", "")
        )
        if dry_run or not worktree_path:
            return {
                "status": remediation_plan.get("status", "retry_planned"),
                "executed": False,
                "attempts_used": attempts_used_prior,
            }
        shell_result = self._run_shell(
            remediation_plan["backend_command"], worktree_path
        )
        changed_files = self._collect_changed_files(worktree_path)
        validation = (
            self._run_validation(worktree_path)
            if shell_result["returncode"] == 0
            else {"ok": False, "results": [], "reason": "backend-failed"}
        )
        attempts_used = attempts_used_prior + 1
        status = "retry_executed"
        push_result: Optional[Dict[str, Any]] = None
        if shell_result.get("timed_out"):
            status = "retry_failed_timeout"
        elif shell_result["returncode"] != 0:
            status = "retry_failed"
        elif not validation.get("ok", False):
            status = "retry_failed_validation"
        elif not changed_files:
            status = "retry_no_changes"
        else:
            push_result = self._apply_commit_push_boundary(
                worktree_path,
                snapshot or {},
                changed_files,
                allow_review_push=allow_review_push,
            )
            if push_result.get("status") == "pending_operator_confirmation":
                status = "retry_pending_push"
            elif push_result.get("status") == "pushed":
                status = "retry_pushed"
            elif push_result.get("status") in {"commit_failed", "push_failed"}:
                status = "retry_failed_push"
        return {
            "status": status,
            "executed": True,
            "attempts_used": attempts_used,
            "backend_result": shell_result,
            "changed_files": changed_files,
            "validation": validation,
            "push_result": push_result,
        }

    def _persist_review_state(
        self,
        active_state: Dict[str, Any],
        review_state: Dict[str, Any],
        result: ReviewCycleResult,
    ) -> None:
        active_state = dict(active_state or {})
        review_state = dict(review_state or {})
        active_state["prs"] = dict(active_state.get("prs", {}))
        review_state["prs"] = dict(review_state.get("prs", {}))
        self.state.save_active_prs(self.repo.config.name, active_state)
        self.state.save_review_state(self.repo.config.name, review_state)
        self._update_status_artifact(result)

    def _update_status_artifact(self, result: ReviewCycleResult) -> None:
        """Write review-care counters to the repo's status.json artifact."""
        status = self.state.load_state(self.repo.config.name)
        # keep runner state untouched; update status artifact instead
        status_file = self.state._get_state_dir(self.repo.config.name) / "status.json"
        data: Dict[str, Any] = {}
        if status_file.exists():
            try:
                with open(status_file) as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data["review_care"] = {
            "enabled": bool(self.repo.config.review_care.get("enabled", True)),
            "provider_order": list(
                self.repo.config.review_care.get("provider_order", ["github"])
            ),
            "active_managed_prs": result.active_prs,
            "review_blocked_prs": result.blocked_prs,
            "retry_eligible_prs": result.retry_eligible_prs,
            "retry_planned_prs": result.retry_planned_prs,
            "retry_prepared_prs": result.retry_prepared_prs,
            "retry_executed_prs": result.retry_executed_prs,
            "retry_failed_prs": result.retry_failed_prs,
            "retry_exhausted_prs": result.retry_exhausted_prs,
            "merge_ready_prs": result.merge_ready_prs,
            "paused_prs": result.paused_prs,
            "last_review_cycle_at": now_iso(),
        }
        status_file.parent.mkdir(parents=True, exist_ok=True)
        with open(status_file, "w") as f:
            json.dump(data, f, indent=2)

    # -------------------------------------------------------------------------
    # Mode dispatch
    # -------------------------------------------------------------------------

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

    # -------------------------------------------------------------------------
    # Observation cycle — preserves existing behavior exactly
    # -------------------------------------------------------------------------

    def _run_observation_cycle(
        self, dry_run: bool = True, allow_review_push: bool = False
    ) -> ReviewCycleResult:
        """Observe managed PRs, classify review feedback, and plan remediation.

        Args:
            dry_run: If True, skip lock acquisition, execution, and publication.
            allow_review_push: If True, allow pushing remediation commits.

        Returns:
            ReviewCycleResult with PR and finding counters.
        """
        result = ReviewCycleResult()
        managed_prs = self.provider.list_managed_prs()
        active_state = self.state.load_active_prs(self.repo.config.name)
        review_state = self.state.load_review_state(self.repo.config.name)
        previous_active_records = dict(active_state.get("prs", {}))
        previous_review_records = dict(review_state.get("prs", {}))
        active_records: Dict[str, Any] = {}
        review_records: Dict[str, Any] = {}
        active_state["prs"] = active_records
        review_state["prs"] = review_records

        # Preserve PRs not in current GitHub listing to avoid state loss
        # on transient API gaps. Also attempt recovery for previously unreachable PRs
        # with exponential backoff. After MAX_UNREACHABLE_ATTEMPTS, escalate.
        import datetime as _dt_mod

        _MAX_UNREACHABLE_ATTEMPTS = 10
        _RECOVERY_BACKOFF_MINUTES = [30, 60, 120, 240, 480, 720, 1440, 2880, 5760]
        current_pr_numbers = {str(p["number"]) for p in managed_prs}
        for pr_key, prev_record in previous_active_records.items():
            if pr_key not in current_pr_numbers:
                if (
                    prev_record.get("status") == "temporarily_unreachable"
                    and not dry_run
                ):
                    recovery_attempts = prev_record.get("recovery_attempts", 0)
                    next_recovery_at = prev_record.get("next_recovery_at")
                    now = datetime.now(timezone.utc)

                    # Check backoff — skip if not yet due
                    if next_recovery_at:
                        try:
                            if isinstance(next_recovery_at, str):
                                nxt = _dt_mod.datetime.fromisoformat(next_recovery_at)
                            else:
                                nxt = next_recovery_at
                            if now < nxt:
                                # Backoff active, preserve state as-is
                                active_records[pr_key] = {
                                    **prev_record,
                                    "status": "temporarily_unreachable",
                                    "updated_at": now_iso(),
                                }
                                review_records[pr_key] = previous_review_records.get(
                                    pr_key, {}
                                )
                                continue
                        except (ValueError, TypeError):
                            pass

                    # Check max attempts — escalate if exceeded
                    if recovery_attempts >= _MAX_UNREACHABLE_ATTEMPTS:
                        from bluei.app.escalation import write_escalation

                        write_escalation(
                            message=(
                                f"PR #{pr_key} unreachable after {recovery_attempts} recovery attempts — "
                                "manual intervention needed"
                            ),
                            severity="error",
                            repo=self.repo.config.name,
                        )
                        _logger.warning(
                            "Unreachable PR #%s exceeded %d recovery attempts — escalated",
                            pr_key,
                            _MAX_UNREACHABLE_ATTEMPTS,
                        )
                        active_records[pr_key] = {
                            **prev_record,
                            "status": "permanently_unreachable",
                            "recovery_attempts": recovery_attempts,
                            "updated_at": now_iso(),
                        }
                        review_records[pr_key] = previous_review_records.get(pr_key, {})
                        continue

                    # Recovery attempt: targeted API fetch for unreachable PRs
                    try:
                        slug = self._get_repo_slug()
                        pr_data = json.loads(
                            self._run(
                                ["gh", "api", f"/repos/{slug}/pulls/{pr_key}"],
                                retries=1,
                            )
                        )
                        if pr_data.get("state") == "open":
                            _logger.info(
                                "Recovered previously unreachable PR #%s "
                                "(was stuck for %d attempts)",
                                pr_key,
                                recovery_attempts,
                            )
                            managed_prs.append(pr_data)
                            current_pr_numbers.add(pr_key)
                            continue
                    except Exception as exc:
                        _logger.debug(
                            "Recovery fetch failed for PR #%s: %s", pr_key, exc
                        )

                    # Failed recovery — increment counter, apply exponential backoff
                    new_attempts = recovery_attempts + 1
                    backoff_idx = min(
                        new_attempts - 1, len(_RECOVERY_BACKOFF_MINUTES) - 1
                    )
                    backoff_minutes = _RECOVERY_BACKOFF_MINUTES[backoff_idx]
                    next_recovery = now + _dt_mod.timedelta(minutes=backoff_minutes)
                    _logger.info(
                        "Unreachable PR #%s recovery attempt %d failed — "
                        "next attempt in %d min",
                        pr_key,
                        new_attempts,
                        backoff_minutes,
                    )
                    active_records[pr_key] = {
                        **prev_record,
                        "status": "temporarily_unreachable",
                        "recovery_attempts": new_attempts,
                        "next_recovery_at": next_recovery.isoformat(),
                        "updated_at": now_iso(),
                    }
                    review_records[pr_key] = previous_review_records.get(pr_key, {})
                    continue

                active_records[pr_key] = {
                    **prev_record,
                    "status": "temporarily_unreachable",
                    "updated_at": now_iso(),
                }
                review_records[pr_key] = previous_review_records.get(pr_key, {})

        for pr in managed_prs[
            : int(self.repo.config.review_care.get("max_prs_per_run", 1) or 1) * 20
        ]:
            pr_number = int(pr["number"])
            pr_key = str(pr_number)
            lock_handle = None
            try:
                if not dry_run:
                    lock_handle = self._acquire_pr_lock(pr_number)
                snapshot = self.provider.fetch_review_snapshot(pr_number)
                existing_review = previous_review_records.get(pr_key, {})
                existing_fingerprint = existing_review.get("last_snapshot_fingerprint")
                previous_action = str(existing_review.get("last_action") or "")
                loop_count = int(existing_review.get("loop_count", 0))
                previous_attempted_remediation = previous_action in {
                    "retry_executed",
                    "retry_pushed",
                    "retry_failed",
                    "retry_failed_validation",
                    "retry_no_changes",
                    "retry_failed_timeout",
                    "retry_failed_push",
                }
                stale_pause = (not previous_attempted_remediation) and int(
                    existing_review.get("attempts_used", 0)
                ) == 0
                if (
                    existing_fingerprint == snapshot["fingerprint"]
                    and snapshot["actionable_comments"]
                ):
                    if previous_attempted_remediation:
                        loop_count += 1
                    else:
                        loop_count = 0
                elif (
                    existing_fingerprint
                    and existing_fingerprint != snapshot["fingerprint"]
                ):
                    loop_count = 0

                retry_eligible = bool(
                    snapshot["actionable_comments"]
                    or snapshot["active_change_requesters"]
                )
                merge_state_status = str(
                    snapshot.get("merge_state_status") or "UNKNOWN"
                )
                prior_comment_key = str(
                    existing_review.get("last_review_comment_key") or ""
                )
                current_snapshot_prefix = f"{snapshot['fingerprint']}:"
                current_merge_ready_key = f"{snapshot['fingerprint']}:merge_ready"
                review_artifact_exists_for_snapshot = prior_comment_key.startswith(
                    current_snapshot_prefix
                )
                merge_ready_artifact_exists_for_snapshot = (
                    prior_comment_key == current_merge_ready_key
                )
                merge_state = "not_merge_ready"
                merge_reason = "Awaiting review evaluation"
                status = "pending_review"
                paused = False
                remediation_plan: Optional[Dict[str, Any]] = None
                execution_result: Optional[Dict[str, Any]] = None
                if retry_eligible:
                    status = "review_feedback_detected"
                    merge_state = "blocked_by_review"
                    merge_reason = f"{len(snapshot['actionable_comments'])} actionable review comments"
                    result.blocked_prs += 1
                    attempts_used = int(existing_review.get("attempts_used", 0))
                    max_attempts = int(
                        self.repo.config.review_care.get("max_attempts", 3)
                    )
                    if (
                        previous_action == "retry_pending_push"
                        and existing_fingerprint == snapshot["fingerprint"]
                    ):
                        status = "retry_pending_push"
                        merge_state = "awaiting_operator_push"
                        merge_reason = "Validated remediation is waiting for explicit commit/push approval"
                        remediation_plan = existing_review.get("planned_remediation")
                        execution_result = existing_review.get("execution_result")
                    elif attempts_used >= max_attempts:
                        status = "retry_exhausted"
                        merge_reason = f"Max retry attempts ({max_attempts}) exhausted"
                        result.retry_exhausted_prs += 1
                    elif loop_count > int(
                        self.repo.config.review_care.get("max_loops", 2)
                    ):
                        status = "loop_guard_paused"
                        paused = True
                        result.paused_prs += 1
                    elif not dry_run and not lock_handle:
                        status = "retry_lock_busy"
                        merge_reason = "PR remediation lock already held"
                    else:
                        remediation_plan = self._plan_remediation(
                            snapshot, existing_review, dry_run
                        )
                        if remediation_plan:
                            status = remediation_plan["status"]
                            result.retry_planned_prs += 1
                            if remediation_plan["status"] == "retry_prepared":
                                result.retry_prepared_prs += 1
                elif merge_state_status in {"CLEAN", "UNKNOWN", "UNSTABLE"}:
                    if (
                        merge_ready_artifact_exists_for_snapshot
                        or review_artifact_exists_for_snapshot
                    ):
                        status = "merge_ready"
                        merge_state = "ready_for_merge"
                        if merge_state_status == "CLEAN":
                            merge_reason = "QA review artifact exists for this snapshot and no actionable review blockers were found"
                        else:
                            merge_reason = (
                                "QA review artifact exists for this snapshot and no actionable review blockers were found, "
                                f"with merge state {merge_state_status.lower()} pending fresh merge triage"
                            )
                        result.merge_ready_prs += 1
                    else:
                        status = "pending_review"
                        merge_state = "awaiting_review_artifact"
                        if merge_state_status == "CLEAN":
                            merge_reason = "Clean PR, but bluei review for this snapshot has not been published yet"
                        else:
                            merge_reason = (
                                "No actionable review blockers and merge state is "
                                f"{merge_state_status.lower()}, but bluei review for this snapshot has not been published yet"
                            )
                else:
                    status = "awaiting_mergeability"
                    merge_state = "awaiting_mergeability"
                    merge_reason = (
                        "No actionable review blockers, but merge state is "
                        f"{merge_state_status.lower()}"
                    )

                final_retry_eligible = False

                result.active_prs += 1
                active_records[pr_key] = {
                    "pr_number": pr_number,
                    "url": pr.get("url"),
                    "branch": pr.get("headRefName") or snapshot["branch"],
                    "author": (pr.get("author") or {}).get("login")
                    or snapshot["author"],
                    "source": "bluei-heuristic",
                    "status": status,
                    "opened_at": (previous_active_records.get(pr_key, {}) or {}).get(
                        "opened_at"
                    )
                    or snapshot["fetched_at"],
                    "updated_at": snapshot["fetched_at"],
                    "provider_summary": {
                        "provider": "github",
                        "review_decision": snapshot["review_decision"],
                        "merge_state_status": snapshot["merge_state_status"],
                        "active_change_requesters": snapshot[
                            "active_change_requesters"
                        ],
                        "actionable_comment_count": len(
                            snapshot["actionable_comments"]
                        ),
                        "score_optional": None,
                    },
                    "merge_readiness": {
                        "state": merge_state,
                        "reason": merge_reason,
                        "evaluated_at": snapshot["fetched_at"],
                    },
                    "remediation_plan": remediation_plan,
                    "execution_result": execution_result,
                }
                review_records[pr_key] = {
                    "last_provider": "github",
                    "last_polled_at": snapshot["fetched_at"],
                    "last_snapshot_fingerprint": snapshot["fingerprint"],
                    "last_snapshot": {
                        "review_decision": snapshot["review_decision"],
                        "merge_state_status": snapshot["merge_state_status"],
                        "active_change_requesters": snapshot[
                            "active_change_requesters"
                        ],
                        "actionable_comment_count": len(
                            snapshot["actionable_comments"]
                        ),
                        "informational_comment_count": len(
                            snapshot["informational_comments"]
                        ),
                    },
                    "attempts_used": int(
                        (execution_result or {}).get(
                            "attempts_used", existing_review.get("attempts_used", 0)
                        )
                    ),
                    "loop_count": loop_count,
                    "retry_eligible": final_retry_eligible,
                    "last_action": status,
                    "last_action_at": snapshot["fetched_at"],
                    "last_action_reason": merge_reason,
                    "planned_remediation": remediation_plan,
                    "execution_result": execution_result,
                    "last_review_comment_key": existing_review.get(
                        "last_review_comment_key"
                    ),
                    "last_review_comment_url": existing_review.get(
                        "last_review_comment_url"
                    ),
                    "last_review_comment_at": existing_review.get(
                        "last_review_comment_at"
                    ),
                    "escalation": (
                        {
                            "kind": "loop_guard_paused",
                            "reason": "fingerprint repeated beyond threshold",
                            "at": snapshot["fetched_at"],
                        }
                        if paused and not stale_pause
                        else None
                    ),
                }
                if not dry_run:
                    self.state.append_review_event(
                        self.repo.config.name,
                        {
                            "pr_number": pr_number,
                            "event": status,
                            "provider": "github",
                            "fingerprint": snapshot["fingerprint"],
                            "details": {
                                "review_decision": snapshot["review_decision"],
                                "actionable_comment_count": len(
                                    snapshot["actionable_comments"]
                                ),
                                "loop_count": loop_count,
                                "planned_prompt_file": (remediation_plan or {}).get(
                                    "prompt_file"
                                ),
                                "worktree_path": (
                                    (remediation_plan or {}).get("worktree") or {}
                                ).get("worktree_path"),
                                "backend_returncode": (
                                    (execution_result or {}).get("backend_result") or {}
                                ).get("returncode"),
                                "changed_files": (execution_result or {}).get(
                                    "changed_files"
                                ),
                                "validation_ok": (
                                    (execution_result or {}).get("validation") or {}
                                ).get("ok"),
                            },
                        },
                    )
                    # State persisted at end of per-PR processing (H2: removed intermediate write)

                    if (
                        remediation_plan
                        and remediation_plan.get("status") == "retry_prepared"
                    ):
                        execution_result = self._execute_prepared_remediation(
                            remediation_plan,
                            existing_review,
                            dry_run,
                            allow_review_push=allow_review_push,
                            snapshot=snapshot,
                        )
                        execution_status = execution_result.get("status") or status
                        if execution_result.get("executed"):
                            if execution_status in {
                                "retry_executed",
                                "retry_pending_push",
                                "retry_pushed",
                            }:
                                result.retry_executed_prs += 1
                            elif execution_status.startswith("retry_failed"):
                                result.retry_failed_prs += 1
                        active_records[pr_key]["status"] = execution_status
                        active_records[pr_key]["execution_result"] = execution_result
                        review_records[pr_key]["attempts_used"] = int(
                            execution_result.get(
                                "attempts_used",
                                review_records[pr_key].get("attempts_used", 0),
                            )
                        )
                        review_records[pr_key]["last_action"] = execution_status
                        review_records[pr_key]["execution_result"] = execution_result
                        review_records[pr_key]["planned_remediation"] = remediation_plan
                        if execution_status == "retry_pending_push":
                            active_records[pr_key]["merge_readiness"] = {
                                "state": "awaiting_operator_push",
                                "reason": "Validated remediation is waiting for explicit commit/push approval",
                                "evaluated_at": snapshot["fetched_at"],
                            }
                        elif execution_status in {"retry_executed", "retry_pushed"}:
                            active_records[pr_key]["merge_readiness"] = {
                                "state": "awaiting_re_review",
                                "reason": "Remediation pushed; awaiting new review snapshot"
                                if execution_status == "retry_pushed"
                                else "Remediation executed locally; awaiting commit/push boundary",
                                "evaluated_at": snapshot["fetched_at"],
                            }
                        self.state.append_review_event(
                            self.repo.config.name,
                            {
                                "pr_number": pr_number,
                                "event": execution_status,
                                "provider": "github",
                                "fingerprint": snapshot["fingerprint"],
                                "details": {
                                    "backend_returncode": (
                                        (execution_result or {}).get("backend_result")
                                        or {}
                                    ).get("returncode"),
                                    "changed_files": (execution_result or {}).get(
                                        "changed_files"
                                    ),
                                    "validation_ok": (
                                        (execution_result or {}).get("validation") or {}
                                    ).get("ok"),
                                    "push_status": (
                                        (execution_result or {}).get("push_result")
                                        or {}
                                    ).get("status"),
                                },
                            },
                        )
                        # H2: removed intermediate write after execution; state saved at end of per-PR block

                    final_status = active_records[pr_key]["status"]
                    final_retry_eligible = final_status in {
                        "review_feedback_detected",
                        "retry_planned",
                        "retry_prepared",
                        "retry_lock_busy",
                        "retry_failed",
                        "retry_failed_timeout",
                        "retry_failed_validation",
                        "retry_no_changes",
                    }
                    active_records[pr_key]["retry_eligible"] = final_retry_eligible
                    review_records[pr_key]["retry_eligible"] = final_retry_eligible
                    if final_retry_eligible:
                        result.retry_eligible_prs += 1

                    final_merge_readiness = active_records[pr_key]["merge_readiness"]
                    publication_key = f"{snapshot['fingerprint']}:{final_status}"
                    review_comment_url = self._publish_review_cycle_comment(
                        pr_number=pr_number,
                        summary_text=self._build_review_cycle_comment(
                            pr_number=pr_number,
                            snapshot=snapshot,
                            status=final_status,
                            merge_readiness=final_merge_readiness,
                            execution_result=active_records[pr_key].get(
                                "execution_result"
                            ),
                        ),
                        publication_key=publication_key,
                        existing_review=review_records[pr_key],
                    )
                    if review_comment_url:
                        review_records[pr_key]["last_review_comment_key"] = (
                            publication_key
                        )
                        review_records[pr_key]["last_review_comment_url"] = (
                            review_comment_url
                        )
                        review_records[pr_key]["last_review_comment_at"] = snapshot[
                            "fetched_at"
                        ]
                        active_records[pr_key]["review_comment"] = {
                            "url": review_comment_url,
                            "key": publication_key,
                            "published_at": snapshot["fetched_at"],
                        }
                    elif review_records[pr_key].get("last_review_comment_url"):
                        active_records[pr_key]["review_comment"] = {
                            "url": review_records[pr_key].get(
                                "last_review_comment_url"
                            ),
                            "key": review_records[pr_key].get(
                                "last_review_comment_key"
                            ),
                            "published_at": review_records[pr_key].get(
                                "last_review_comment_at"
                            ),
                        }
                    self._persist_review_state(active_state, review_state, result)
            finally:
                self._release_pr_lock(lock_handle)

        if not dry_run:
            self._persist_review_state(active_state, review_state, result)
        return result

    # -------------------------------------------------------------------------
    # Phase J: Live publication bridge helpers
    # -------------------------------------------------------------------------

    def _find_open_prs(self) -> List[Dict[str, Any]]:
        """Return open PRs for the repo via gh pr list, sorted by updatedAt desc.

        Returns:
            List of dicts with at least number (int) and updatedAt (str), or [].
        """
        try:
            owner = (
                self.repo.config.github.get("owner")
                or self.repo.config.name.split("/")[0]
                if "/" in self.repo.config.name
                else ""
            )
            repo_name = (
                self.repo.config.github.get("repo")
                or self.repo.config.name.split("/")[1]
                if "/" in self.repo.config.name
                else self.repo.config.name
            )
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--json",
                    "number,title,updatedAt",
                    "--repo",
                    f"{owner}/{repo_name}",
                    "--state",
                    "open",
                    "--limit",
                    "10",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return []
            import json as _json

            prs = _json.loads(result.stdout)
            prs.sort(key=lambda p: p.get("updatedAt", ""), reverse=True)
            return prs
        except Exception:
            return []

    def _resolve_target_pr_for_run(
        self,
        prior_publish: Dict[str, Any],
    ) -> Tuple[Optional[int], str]:
        """Resolve which PR to target for publication using a safety-first priority chain.

        Priority: prior explicit target → single managed PR → single open PR → refuse.

        Args:
            prior_publish: Loaded publish state dict with runs sub-dict.

        Returns:
            (pr_number, reason) tuple. pr_number is None when targeting is refused.
        """
        # --- Step 1: prior published runs' explicit targeting ---
        runs_entries = prior_publish.get("runs", {})
        prior_targeted: List[int] = []
        for rid, rentry in runs_entries.items():
            tpn = rentry.get("targeted_pr_number")
            if tpn is not None:
                try:
                    prior_targeted.append(int(tpn))
                except (TypeError, ValueError):
                    pass

        if len(set(prior_targeted)) == 1:
            # Exactly one distinct PR was targeted in all prior runs — safe to reuse
            confirmed = prior_targeted[0]
            open_prs = self._find_open_prs()
            open_numbers = {p["number"] for p in open_prs}
            if open_numbers and confirmed not in open_numbers:
                # Prior targeted PR is now closed — must not republish there
                return (
                    None,
                    f"prior-targeted-pr-{confirmed}-now-closed",
                )
            return (
                confirmed,
                f"prior-targeted-pr-{confirmed}-reused",
            )

        # --- Step 2: managed PRs (repo state — unambiguous by definition) ---
        try:
            managed_prs = self.provider.list_managed_prs()
        except Exception:
            managed_prs = []
        if len(managed_prs) == 1:
            pr_number = int(managed_prs[0]["number"])
            return (pr_number, f"single-managed-pr-{pr_number}")
        if len(managed_prs) > 1:
            # Multiple managed PRs — ambiguous, refuse
            return (
                None,
                f"multiple-managed-prs-{len(managed_prs)}-refused",
            )

        # --- Step 3: open PR discovery — only if exactly one ---
        open_prs = self._find_open_prs()
        if len(open_prs) == 1:
            pr_number = int(open_prs[0]["number"])
            return (pr_number, f"single-open-pr-{pr_number}")
        if len(open_prs) > 1:
            return (
                None,
                f"multiple-open-prs-{len(open_prs)}-refused",
            )

        # --- No open PRs at all ---
        return (None, "no-open-prs")

    def _post_summary_to_github(
        self,
        summary_text: str,
        run_id: str,
        prior_publish: Dict[str, Any],
        target_pr_number: Optional[int] = None,
    ) -> Optional[str]:
        """Post the autonomous-review summary comment to GitHub.

        Idempotent per run_id. Respects live_actions gate, shadow mode,
        and safe PR targeting. Retries transient failures with backoff.

        Args:
            summary_text: Pre-built summary comment body.
            run_id: Current run ID for deduplication.
            prior_publish: Loaded publish state dict (mutated in-place).
            target_pr_number: Explicit PR to target, or None for auto-resolution.

        Returns:
            GitHub comment URL on success, or None if not published.
        """
        # --- Policy gate ---
        if not self.repo.config.github.get("live_actions", False):
            return None

        # --- Idempotency: skip if already published for this run ---
        # Use setdefault so runs_entries is a reference to the actual prior_publish["runs"]
        runs_entries = prior_publish.setdefault("runs", {})
        existing = runs_entries.get(run_id, {})
        if existing.get("comment_url"):
            return existing["comment_url"]

        # --- Ensure run entry exists in publish state (needed for refusal/failure paths) ---
        if run_id not in runs_entries:
            runs_entries[run_id] = {}

        # --- Resolve target PR ---
        # Use explicit parameter if provided; otherwise resolve via priority chain.
        if target_pr_number is None:
            target_pr_number, resolution_reason = self._resolve_target_pr_for_run(
                prior_publish,
            )
        else:
            resolution_reason = f"explicit-{target_pr_number}"

        # --- SHADOW MODE: intercept ALL publication paths ---
        # Shadow mode records what WOULD have been published (for any targeting outcome:
        # no target, target not open, or target valid). This lets us validate the full
        # pipeline (targeting + summary) without actually mutating GitHub.
        # The shadow entry is recorded FIRST so it's set regardless of subsequent
        # refusal/failure logic.
        live_rollout_mode, _ = self._get_live_rollout_mode()
        if live_rollout_mode == LiveRolloutMode.SHADOW:
            # Always record shadow entry first (before any refusal)
            runs_entries[run_id]["status"] = PublishStatus.PENDING.value
            runs_entries[run_id]["shadow"] = True
            runs_entries[run_id]["shadow_summary_text"] = summary_text
            runs_entries[run_id]["targeted_pr_number"] = target_pr_number
            runs_entries[run_id]["rollout_mode"] = LiveRolloutMode.SHADOW.value

            # Determine targeting outcome for the event
            open_prs = self._find_open_prs()
            open_numbers = {p["number"] for p in open_prs}
            if target_pr_number is None:
                targeting_outcome = "no-target-pr"
            elif open_prs and target_pr_number not in open_numbers:
                targeting_outcome = f"target-pr-{target_pr_number}-not-open"
            else:
                targeting_outcome = f"target-pr-{target_pr_number}-would-post"

            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "autonomous-review-shadow-published",
                    "run_id": run_id,
                    "provider": "github",
                    "details": {
                        "pr": target_pr_number,
                        "rollout_mode": LiveRolloutMode.SHADOW.value,
                        "summary_length": len(summary_text),
                        "targeting_outcome": targeting_outcome,
                        "open_prs": list(open_numbers) if open_prs else [],
                        "action": "shadow-would-have-published",
                    },
                },
            )
            return None

        # --- Refuse if no safe target found ---
        if target_pr_number is None:
            runs_entries[run_id]["status"] = PublishStatus.FAILED.value
            runs_entries[run_id]["error"] = f"target-refused:{resolution_reason}"
            runs_entries[run_id]["targeted_pr_number"] = None
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "autonomous-review-publish-refused",
                    "run_id": run_id,
                    "provider": "github",
                    "details": {
                        "reason": resolution_reason,
                        "action": "stayed-local-only",
                    },
                },
            )
            return None

        # --- Verify target PR is still open ---
        open_prs = self._find_open_prs()
        open_numbers = {p["number"] for p in open_prs}
        if open_prs and target_pr_number not in open_numbers:
            # Target PR is no longer open — refuse rather than post to wrong PR
            runs_entries[run_id]["status"] = PublishStatus.FAILED.value
            runs_entries[run_id]["error"] = f"target-pr-{target_pr_number}-not-open"
            runs_entries[run_id]["targeted_pr_number"] = None
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "autonomous-review-publish-refused",
                    "run_id": run_id,
                    "provider": "github",
                    "details": {
                        "reason": f"target-pr-{target_pr_number}-not-open",
                        "targeted_pr": target_pr_number,
                        "open_prs": list(open_numbers),
                        "action": "stayed-local-only",
                    },
                },
            )
            return None

        # --- Post summary comment to the resolved target PR ---
        owner = (
            self.repo.config.github.get("owner") or self.repo.config.name.split("/")[0]
        )
        repo_name = (
            self.repo.config.github.get("repo") or self.repo.config.name.split("/")[1]
        )

        # Retry configuration: bounded exponential backoff for transient errors.
        # Uses retry_delay_minutes from review_care (in minutes) as the base delay,
        # scaled exponentially. Default base is 15 minutes but we cap at 4 seconds
        # to avoid long delays during a single run.
        base_delay_seconds = min(
            float(self.repo.config.review_care.get("retry_delay_minutes", 15)) * 60.0,
            4.0,
        )
        max_retries = 2  # Up to 3 attempts total

        def _is_transient_failure(returncode: int, stderr: str, stdout: str) -> bool:
            """Return True if the gh failure is likely transient and worth retrying."""
            if returncode == 124:
                return True
            combined = (stderr + stdout).lower()
            if any(
                kw in combined
                for kw in [
                    "rate limit",
                    "rate_limit",
                    "429",
                    "too many requests",
                    "secondary rate limit",
                    "abuse rate limit",
                ]
            ):
                return True
            if any(
                kw in combined
                for kw in [
                    "connection reset",
                    "connection refused",
                    "name or service not known",
                    "network is unreachable",
                    "temporary failure in name resolution",
                    "could not resolve host",
                    "ssl",
                    "tls",
                ]
            ):
                return True
            return False

        last_err = ""
        published_url: Optional[str] = None
        try:
            for attempt in range(max_retries + 1):
                try:
                    result = subprocess.run(
                        [
                            "gh",
                            "pr",
                            "comment",
                            str(target_pr_number),
                            "--repo",
                            f"{owner}/{repo_name}",
                            "--body",
                            summary_text,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                except Exception as exc:
                    last_err = str(exc)
                    if attempt < max_retries:
                        delay = base_delay_seconds * (2**attempt)
                        self.state.append_review_event(
                            self.repo.config.name,
                            {
                                "event": "autonomous-review-publish-retry",
                                "run_id": run_id,
                                "provider": "github",
                                "details": {
                                    "pr": target_pr_number,
                                    "attempt": attempt + 1,
                                    "max_retries": max_retries + 1,
                                    "delay_seconds": delay,
                                    "error": last_err,
                                },
                            },
                        )
                        import time as _time

                        _time.sleep(delay)
                        continue
                    # Exhausted retries
                    last_err = f"gh-call-exception:{last_err}"
                    if run_id in runs_entries:
                        runs_entries[run_id]["status"] = PublishStatus.FAILED.value
                        runs_entries[run_id]["error"] = last_err
                        runs_entries[run_id]["targeted_pr_number"] = target_pr_number
                    published_url = None
                    break

                if result.returncode == 0:
                    # Success — parse the comment URL and exit the retry loop
                    published_url = result.stdout.strip()
                    if run_id in runs_entries:
                        runs_entries[run_id]["status"] = PublishStatus.PUBLISHED.value
                        runs_entries[run_id]["comment_url"] = published_url
                        runs_entries[run_id]["targeted_pr_number"] = target_pr_number
                    self.state.append_review_event(
                        self.repo.config.name,
                        {
                            "event": "autonomous-review-published",
                            "run_id": run_id,
                            "provider": "github",
                            "details": {
                                "pr": target_pr_number,
                                "comment_url": published_url,
                                "resolution": resolution_reason,
                            },
                        },
                    )
                    break

                last_err = (
                    result.stderr.strip()
                    or f"gh-pr-comment-failed (code {result.returncode})"
                )
                if attempt < max_retries and _is_transient_failure(
                    result.returncode, last_err, result.stdout.strip()
                ):
                    delay = base_delay_seconds * (2**attempt)
                    self.state.append_review_event(
                        self.repo.config.name,
                        {
                            "event": "autonomous-review-publish-retry",
                            "run_id": run_id,
                            "provider": "github",
                            "details": {
                                "pr": target_pr_number,
                                "attempt": attempt + 1,
                                "max_retries": max_retries + 1,
                                "delay_seconds": delay,
                                "error": last_err,
                            },
                        },
                    )
                    import time as _time

                    _time.sleep(delay)
                    continue

                # Non-transient or exhausted retries — record failure and stop
                if run_id in runs_entries:
                    runs_entries[run_id]["status"] = PublishStatus.FAILED.value
                    runs_entries[run_id]["error"] = last_err
                    runs_entries[run_id]["targeted_pr_number"] = target_pr_number
                self.state.append_review_event(
                    self.repo.config.name,
                    {
                        "event": "autonomous-review-publish-failed",
                        "run_id": run_id,
                        "provider": "github",
                        "details": {
                            "pr": target_pr_number,
                            "error": last_err,
                            "retries_exhausted": attempt >= max_retries,
                        },
                    },
                )
                published_url = None
                break
        except Exception as exc:
            last_err = str(exc)
            if run_id in runs_entries:
                runs_entries[run_id]["status"] = PublishStatus.FAILED.value
                runs_entries[run_id]["error"] = last_err
                runs_entries[run_id]["targeted_pr_number"] = target_pr_number
            published_url = None

        return published_url

    # -------------------------------------------------------------------------
    # Phase G1: Autonomous review cycle — real local execution path
    # -------------------------------------------------------------------------

    def _resolve_pr_context_for_autonomous_run(
        self,
        prior_publish: Dict[str, Any],
    ) -> Tuple[Optional[int], str]:
        """Resolve PR context for an autonomous review run.

        Priority: explicit config pr_number → safe targeting from prior state.

        Args:
            prior_publish: The loaded publish state dict.

        Returns:
            (pr_number, reason) tuple. None means local-only mode.
        """
        # Step 1: explicit configured PR
        configured_pr = self.repo.config.github.get("pr_number")
        if configured_pr is not None:
            try:
                pr_num = int(configured_pr)
                return (pr_num, f"explicit-config-pr-{pr_num}")
            except (TypeError, ValueError):
                # Non-integer value in config — treat as no explicit PR
                pass

        # Step 2: safe targeting via prior state (only when live_actions is enabled)
        if self.repo.config.github.get("live_actions", False):
            pr_num, reason = self._resolve_target_pr_for_run(prior_publish)
            if pr_num is not None:
                return (pr_num, f"resolved-{reason}")
            # pr_num is None — reason explains why (ambiguous, no PRs, etc.)
            return (None, reason)

        # live_actions disabled — no PR context, local-only
        return (None, "live-actions-disabled")

    # -------------------------------------------------------------------------
    # Phase G4: Guarded-run policy for autonomous-review
    # -------------------------------------------------------------------------

    def _is_guarded_live_review_enabled(self) -> Tuple[bool, str]:
        """Check whether both guarded_live_review and live_actions are enabled.

        Returns:
            (enabled, reason) tuple.
        """
        guarded_flag = bool(
            self.repo.config.review_care.get("guarded_live_review", False)
        )
        live_actions = bool(self.repo.config.github.get("live_actions", False))

        if guarded_flag and live_actions:
            return (True, "guard-passed")
        if guarded_flag and not live_actions:
            return (False, "guard-failed-live-actions-disabled")
        if not guarded_flag and live_actions:
            return (False, "guard-failed-guarded-live-review-disabled")
        # neither is set
        return (False, "guard-failed-both-disabled")

    def _get_live_rollout_mode(self) -> Tuple[LiveRolloutMode, str]:
        """Read and validate live_rollout_mode from config, falling back to local_only.

        Returns:
            (mode, reason) tuple. Bad values or unsafe combos fall back to local_only.
        """
        raw = self.repo.config.review_care.get(
            "live_rollout_mode", LiveRolloutMode.LOCAL_ONLY.value
        )
        live_actions = bool(self.repo.config.github.get("live_actions", False))

        # Validate raw value against known enum members
        try:
            mode = LiveRolloutMode(raw)
        except ValueError:
            # Bad/unknown value — fall back to local_only with explicit reason
            return (
                LiveRolloutMode.LOCAL_ONLY,
                f"unknown-live-rollout-mode:{raw}-fallback-to-local-only",
            )

        # Refuse unsafe combinations cleanly
        if (
            mode in (LiveRolloutMode.SHADOW, LiveRolloutMode.LIMITED)
            and not live_actions
        ):
            return (
                LiveRolloutMode.LOCAL_ONLY,
                f"rollout-{mode.value}-requires-live-actions-false-falling-back-to-local-only",
            )

        # Shadow mode is always safe (never actually publishes)
        if mode == LiveRolloutMode.SHADOW:
            return (LiveRolloutMode.SHADOW, "shadow-mode-active")

        # Limited mode passes through to guarded_live_review check
        if mode == LiveRolloutMode.LIMITED:
            # Use the existing guarded_live_review check to determine the actual outcome
            guarded_enabled, guard_reason = self._is_guarded_live_review_enabled()
            if guarded_enabled:
                return (LiveRolloutMode.LIMITED, "limited-mode-guard-passed")
            else:
                return (
                    LiveRolloutMode.LOCAL_ONLY,
                    f"limited-mode-guard-failed-{guard_reason}-fallback-to-local-only",
                )

        # Default: local_only
        return (LiveRolloutMode.LOCAL_ONLY, "local-only-default")

    def _emit_guard_event(
        self,
        run_id: str,
        pr_number: Optional[int],
        guard_enabled: bool,
        guard_reason: str,
    ) -> None:
        """Emit a guard decision event to the review events log.

        Args:
            run_id: Current run ID.
            pr_number: Resolved PR number (or None).
            guard_enabled: Whether the guarded live path is active.
            guard_reason: Human-readable reason for the guard decision.
        """
        event_name = (
            "autonomous-review-guard-passed"
            if guard_enabled
            else "autonomous-review-guard-blocked"
        )
        self.state.append_review_event(
            self.repo.config.name,
            {
                "event": event_name,
                "run_id": run_id,
                "provider": "local-stub",
                "details": {
                    "guard_enabled": guard_enabled,
                    "guard_reason": guard_reason,
                    "targeted_pr": pr_number,
                    "live_actions": self.repo.config.github.get("live_actions", False),
                    "guarded_live_review": self.repo.config.review_care.get(
                        "guarded_live_review", False
                    ),
                },
            },
        )

    # -------------------------------------------------------------------------
    # Phase G6: Limited publish filters for autonomous-review live publication
    # -------------------------------------------------------------------------

    def _check_limited_publish_filters(
        self,
        findings_total: int,
        findings_list: List[Dict[str, Any]],
        pr_context: Optional[Dict[str, Any]],
    ) -> "PublishFilterResult":
        """Check whether a limited-mode run passes publish filters.

        Filters: max_findings_count, require_pr_context, allowed_severities,
        allowed_headers. Any failure falls back to shadow/local-only.

        Args:
            findings_total: Total deduplicated findings for the run.
            findings_list: Full list of deduplicated finding dicts.
            pr_context: Resolved PR context dict, or None.

        Returns:
            PublishFilterResult with passed, decision, and failed_reason.
        """
        # Step 1: max_findings_count
        max_findings = int(
            self.repo.config.review_care.get("limited_max_findings_count", 10)
        )
        if findings_total > max_findings:
            return PublishFilterResult(
                passed=False,
                decision="fail",
                failed_reason=(
                    f"findings_count={findings_total} exceeds "
                    f"limited_max_findings_count={max_findings}"
                ),
            )

        # Step 2: require_pr_context
        require_pr = bool(
            self.repo.config.review_care.get("limited_require_pr_context", True)
        )
        if require_pr and pr_context is None:
            return PublishFilterResult(
                passed=False,
                decision="fail",
                failed_reason="pr_context required but not available",
            )
        if (
            require_pr
            and pr_context is not None
            and pr_context.get("pr_number") is None
        ):
            return PublishFilterResult(
                passed=False,
                decision="fail",
                failed_reason="pr_context present but pr_number is None",
            )

        # Step 3: allowed_severities
        allowed_severities_raw = self.repo.config.review_care.get(
            "limited_allowed_severities", None
        )
        if allowed_severities_raw is not None:
            allowed_severities = {s.lower().strip() for s in allowed_severities_raw}
            failing_severities: List[str] = []
            for f in findings_list:
                severity_val = f.get("severity")
                if isinstance(severity_val, FindingSeverity):
                    sev_str = severity_val.value
                elif isinstance(severity_val, str):
                    sev_str = severity_val.lower().strip()
                else:
                    sev_str = "unknown"
                if sev_str not in allowed_severities:
                    failing_severities.append(sev_str)
            if failing_severities:
                unique_failing = sorted(set(failing_severities))
                return PublishFilterResult(
                    passed=False,
                    decision="fail",
                    failed_reason=(
                        f"severity subset check failed: "
                        f"found [{', '.join(unique_failing)}] "
                        f"but limited_allowed_severities={sorted(allowed_severities)}"
                    ),
                )

        # Step 4: allowed_headers
        allowed_headers_raw = self.repo.config.review_care.get(
            "limited_allowed_headers", None
        )
        if allowed_headers_raw is not None:
            allowed_headers = {h.lower().strip() for h in allowed_headers_raw}
            failing_headers: List[str] = []
            for f in findings_list:
                header_val = f.get("header", "")
                norm_header = normalize_finding_header(str(header_val))
                if norm_header not in allowed_headers:
                    failing_headers.append(str(header_val))
            if failing_headers:
                unique_failing = sorted(set(failing_headers))
                return PublishFilterResult(
                    passed=False,
                    decision="fail",
                    failed_reason=(
                        f"header subset check failed: "
                        f"found [{', '.join(unique_failing[:5])}"
                        f"{' ...' if len(unique_failing) > 5 else ''}] "
                        f"but limited_allowed_headers={sorted(allowed_headers)}"
                    ),
                )

        # All filters passed
        return PublishFilterResult(passed=True, decision="pass", failed_reason="")

    # -------------------------------------------------------------------------
    # Phase G7: Monitored-rollout safety (circuit breaker / open-cooldown)
    # -------------------------------------------------------------------------

    def _check_monitored_safety(
        self,
    ) -> Tuple[bool, str, "MonitoredSafetyState"]:
        """Check whether the circuit-breaker allows live publication.

        Returns:
            (circuit_allows_live, reason, safety_state) tuple.
        """
        from bluei.review.models import MonitoredSafetyState

        safety_data = self.state.load_monitored_safety_state(self.repo.config.name)
        safety_state = MonitoredSafetyState.from_dict(safety_data)

        if safety_state.auto_rollback_active:
            return (
                False,
                f"auto-rollback-active-{safety_state.auto_rollback_reason or 'feedback-threshold-exceeded'}",
                safety_state,
            )

        # Check if cooldown period has elapsed and circuit can close
        if safety_state.circuit_open and not safety_state.check_cooldown_ready():
            return (
                False,
                f"circuit-open-cooldown-active-until-{safety_state.cooldown_until}",
                safety_state,
            )

        # Cooldown expired OR circuit was never open — close the circuit
        if safety_state.circuit_open:
            safety_state.circuit_open = False
            self.state.save_monitored_safety_state(
                self.repo.config.name, safety_state.to_dict()
            )
            return (
                False,  # still False — must pass filters independently on next run
                f"circuit-closed-cooldown-expired-failure-count-{safety_state.failure_count}",
                safety_state,
            )

        # Circuit is closed, live publication can proceed
        return (True, "circuit-closed", safety_state)

    def _evaluate_feedback_auto_rollback(
        self,
        run_id: str,
    ) -> "MonitoredSafetyState":
        """Fail closed when recent negative feedback exceeds the configured threshold."""
        from datetime import datetime, timezone
        from bluei.review.models import MonitoredSafetyState

        safety_data = self.state.load_monitored_safety_state(self.repo.config.name)
        safety_state = MonitoredSafetyState.from_dict(safety_data)

        if safety_state.auto_rollback_active:
            return safety_state
        if not self.repo.config.review_care.get(
            "monitored_auto_rollback_enabled", False
        ):
            return safety_state

        window = int(self.repo.config.review_care.get("monitored_feedback_window", 20))
        min_events = int(
            self.repo.config.review_care.get("monitored_feedback_min_events", 3)
        )
        threshold = float(
            self.repo.config.review_care.get(
                "monitored_negative_feedback_threshold", 0.3
            )
        )

        feedback_events = self.state.load_feedback_events(self.repo.config.name)
        recent = feedback_events[-window:] if window > 0 else feedback_events
        negative_signals = {"negative", "request_change", "conflict"}
        positive_signals = {"positive", "approve"}

        negatives = 0
        positives = 0
        for event in recent:
            signal = (
                str(event.get("signal") or event.get("sentiment") or "").strip().lower()
            )
            if signal in negative_signals:
                negatives += 1
            elif signal in positive_signals:
                positives += 1

        considered = negatives + positives
        if considered < min_events or considered <= 0:
            return safety_state

        negative_ratio = negatives / considered
        if negative_ratio < threshold:
            return safety_state

        safety_state.auto_rollback_active = True
        safety_state.auto_rollback_reason = (
            f"negative-feedback-ratio-{negative_ratio:.2f}-"
            f"over-threshold-{threshold:.2f}-from-{negatives}-of-{considered}-signals"
        )
        safety_state.auto_rollback_triggered_at = datetime.now(timezone.utc).isoformat()
        safety_state.last_failure_reason = safety_state.auto_rollback_reason
        self.state.save_monitored_safety_state(
            self.repo.config.name, safety_state.to_dict()
        )
        self.state.append_review_event(
            self.repo.config.name,
            {
                "event": "monitored-safety-auto-rollback-activated",
                "run_id": run_id,
                "provider": "local-stub",
                "details": {
                    "negative_signals": negatives,
                    "positive_signals": positives,
                    "considered_signals": considered,
                    "negative_ratio": round(negative_ratio, 4),
                    "threshold": threshold,
                    "window": window,
                    "min_events": min_events,
                    "reason": safety_state.auto_rollback_reason,
                },
            },
        )
        return safety_state

    def _record_publish_failure_for_safety(
        self,
        run_id: str,
        failure_reason: str,
    ) -> "MonitoredSafetyState":
        """Record a publish failure and open the circuit breaker if threshold is met.

        Args:
            run_id: Current run ID for event logging.
            failure_reason: Human-readable failure reason.

        Returns:
            Updated MonitoredSafetyState.
        """
        from datetime import datetime, timezone, timedelta
        from bluei.review.models import MonitoredSafetyState

        safety_data = self.state.load_monitored_safety_state(self.repo.config.name)
        safety_state = MonitoredSafetyState.from_dict(safety_data)

        threshold = int(
            self.repo.config.review_care.get("monitored_failure_threshold", 3)
        )
        cooldown_seconds = int(
            self.repo.config.review_care.get("monitored_cooldown_seconds", 300)
        )

        safety_state.failure_count += 1
        safety_state.last_failure_at = datetime.now(timezone.utc).isoformat()
        safety_state.last_failure_reason = failure_reason

        # Open circuit if failure count meets threshold
        if safety_state.failure_count >= threshold:
            safety_state.circuit_open = True
            expires = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
            safety_state.cooldown_until = expires.isoformat()
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "monitored-safety-circuit-opened",
                    "run_id": run_id,
                    "provider": "local-stub",
                    "details": {
                        "failure_count": safety_state.failure_count,
                        "threshold": threshold,
                        "cooldown_seconds": cooldown_seconds,
                        "cooldown_until": safety_state.cooldown_until,
                        "last_failure_reason": failure_reason,
                    },
                },
            )
        else:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "monitored-safety-failure-recorded",
                    "run_id": run_id,
                    "provider": "local-stub",
                    "details": {
                        "failure_count": safety_state.failure_count,
                        "threshold": threshold,
                        "last_failure_reason": failure_reason,
                    },
                },
            )

        self.state.save_monitored_safety_state(
            self.repo.config.name, safety_state.to_dict()
        )
        return safety_state

    def _record_publish_success_for_safety(self, run_id: str) -> "MonitoredSafetyState":
        """Record a successful publication and reset the circuit breaker.

        Args:
            run_id: Current run ID for event logging.

        Returns:
            Updated MonitoredSafetyState with counters reset.
        """
        from bluei.review.models import MonitoredSafetyState

        safety_data = self.state.load_monitored_safety_state(self.repo.config.name)
        safety_state = MonitoredSafetyState.from_dict(safety_data)

        if safety_state.failure_count > 0 or safety_state.circuit_open:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "monitored-safety-circuit-reset",
                    "run_id": run_id,
                    "provider": "local-stub",
                    "details": {
                        "prior_failure_count": safety_state.failure_count,
                        "prior_circuit_open": safety_state.circuit_open,
                    },
                },
            )

        safety_state.record_success()
        self.state.save_monitored_safety_state(
            self.repo.config.name, safety_state.to_dict()
        )
        return safety_state

    def _run_autonomous_review_cycle(
        self, dry_run: bool = True, allow_review_push: bool = False
    ) -> ReviewCycleResult:
        """Execute a local autonomous-review cycle with guarded live publication.

        Generates candidates (backend or local stub), normalizes, deduplicates,
        checks remediation eligibility, reconciles against prior state, applies
        learned rules, and optionally publishes a summary comment to GitHub.

        Args:
            dry_run: If True, return immediately without processing.
            allow_review_push: If True, allow pushing remediation commits.

        Returns:
            ReviewCycleResult with finding counters.
        """
        from bluei.review.models import (
            CompressionMode,
        )  # local import to avoid circular risk

        result = ReviewCycleResult()

        # --- Guard: dry-run returns immediately without any processing ---
        if dry_run:
            return result

        # --- 1. Load prior publish state (for reconciliation) ---
        prior_publish = self.state.load_review_publish_state(self.repo.config.name)

        # --- 1b. Resolve PR context (explicit-first; used throughout the run) ---
        # Carries PR number through run creation, prompt artifact, publish state,
        # summary generation, and events when a PR is known.
        pr_number, pr_resolution_reason = self._resolve_pr_context_for_autonomous_run(
            prior_publish,
        )
        pr_context: Optional[Dict[str, Any]] = (
            {"pr_number": pr_number, "pr_url": None, "resolution": pr_resolution_reason}
            if pr_number is not None
            else None
        )

        # --- Guard evaluation (Phase G4) ---
        # Determine whether the guarded live-review path is enabled.
        # This gates backend generation and live GitHub publication.
        # The full local pipeline (normalization, state, summary artifact)
        # always runs regardless of guard status.
        guard_enabled, guard_reason = self._is_guarded_live_review_enabled()
        run_id = generate_id("arun")
        self._emit_guard_event(
            run_id=run_id,
            pr_number=pr_number,
            guard_enabled=guard_enabled,
            guard_reason=guard_reason,
        )

        # --- 2. Generate candidates (backend preferred; local stub fallback) ---
        # Backend generation is gated by live_rollout_mode (Phase G5):
        # - shadow mode: backend NOT blocked (shadow allows full backend + targeting,
        #   just never actually publishes to GitHub)
        # - limited mode: backend allowed only when guarded_live_review is enabled
        #   (standard guarded progression)
        # - local_only: backend blocked when live_actions=True (stays local-only)
        # - live_actions=False: backend always allowed for local analysis
        live_rollout_mode, rollout_reason = self._get_live_rollout_mode()
        live_actions = bool(self.repo.config.github.get("live_actions", False))
        if live_rollout_mode == LiveRolloutMode.SHADOW:
            backend_blocked = False
        elif live_rollout_mode == LiveRolloutMode.LIMITED:
            backend_blocked = live_actions and not guard_enabled
        else:
            # local_only (or fallback)
            backend_blocked = live_actions and not guard_enabled
        backend_raw_candidates: List[Dict[str, Any]] = []
        candidate_source: str
        if not backend_blocked:
            backend_raw_candidates = self._generate_from_backend(
                run_id, pr_context=pr_context
            )
            if backend_raw_candidates:
                raw_candidates = backend_raw_candidates
                candidate_source = "backend"
            else:
                # Backend returned no valid candidates; fall back to safe local stub.
                raw_candidates = self._generate_local_candidates()
                candidate_source = "local-stub-fallback"
        else:
            # Guard blocks backend — use local stub directly
            raw_candidates = self._generate_local_candidates()
            candidate_source = "local-stub"

        # --- 3. Normalize → deduplicate → assign identity ---
        validated_findings: List[Dict[str, Any]] = []
        skipped_findings: List[Dict[str, Any]] = []
        for raw in raw_candidates:
            try:
                normalized = normalize_candidate(raw)
            except CandidateValidationError:
                skipped_findings.append(raw)
                continue
            with_identity = assign_finding_identity(normalized)
            validated_findings.append(with_identity)

        deduped = dedupe_findings(validated_findings)
        deduped = self._apply_mnemo_candidate_signals(deduped)

        # --- Phase J: Process learned rules (conservative pattern learning) ---
        # Load existing rules and apply learned-rule suppression/activation.
        # This runs BEFORE remediation eligibility to allow learned rules
        # to suppress low-risk repeated findings before they enter the
        # publish pipeline.  Reaction-only signals are NOT consulted here.
        rules_state = _get_learned_rules_state(self.state, self.repo.config.name)
        _run_id_for_rules = generate_id("arun")
        _patterns_file = (
            self.state._get_state_dir(self.repo.config.name) / "fix_patterns.json"
        )
        (
            deduped_after_rules,
            updated_rules,
            rules_log,
        ) = _process_learned_rules_for_run(
            deduped, rules_state, _run_id_for_rules, patterns_file=_patterns_file
        )

        suppressed_by_rules = len(deduped) - len(deduped_after_rules)
        if suppressed_by_rules > 0 or rules_log:
            for log_line in rules_log:
                # Emit as structured events for traceability
                self.state.append_review_event(
                    self.repo.config.name,
                    {
                        "event": "learned-rule-log",
                        "run_id": _run_id_for_rules,
                        "provider": "local-stub",
                        "details": {"log": log_line},
                    },
                )

        # Persist updated rules state
        _save_learned_rules_state(
            self.state,
            self.repo.config.name,
            _build_learned_rules_payload(updated_rules),
        )
        # Use the filtered findings for the rest of the pipeline
        deduped = deduped_after_rules

        try:
            from bluei.app.emergent_rules import EmergentRuleStore
            from bluei.engine.models import Finding as _ERFinding

            _er_path = (
                self.state._get_state_dir(self.repo.config.name) / "emergent_rules.json"
            )
            _er_store = EmergentRuleStore(_er_path)
            _er_store.retire_stale_rules()
            _er_obs = [
                _ERFinding(
                    finding_id=d.get("finding_id", ""),
                    repo=d.get("repo", ""),
                    path=d.get("path", ""),
                    line=int(d.get("line", 0)),
                    rule=d.get("header", ""),
                    snippet=d.get("snippet", ""),
                    confidence=float(d.get("confidence", 0.5)),
                    quick_win=False,
                    safe_to_autofix=bool(d.get("safe_to_autofix", False)),
                )
                for d in deduped
            ]
            _er_store.observe_findings(_er_obs, run_id=_run_id_for_rules)
            _er_store.validate_proposals()
            _er_active = [r for r in _er_store.load() if r.status.value == "active"]
            if _er_active:
                _er_new = []
                for _er in _er_active:
                    _er_new.append(
                        {
                            "finding_id": f"er-{_er.rule_id}",
                            "rule": _er.rule_id,
                            "path": "",
                            "header": _er.header or "",
                            "severity": "low",
                            "source": "emergent",
                        }
                    )
                deduped.extend(_er_new)
        except Exception:
            pass

        # --- 4. Check remediation eligibility ---
        eligible_findings: List[Dict[str, Any]] = []
        ineligible_findings: List[Dict[str, Any]] = []
        for finding in deduped:
            eligibility = is_remediation_eligible(finding, repo_config=self.repo.config)
            if eligibility.eligible:
                eligible_findings.append(finding)
            else:
                ineligible_findings.append(finding)

        # --- 5. Reconcile against prior publish state ---
        reconciliation = reconcile_publish_state(deduped, prior_publish)

        # --- 6. Build per-finding publish entries (local only — no GitHub) ---
        finding_statuses: List[PublishStatus] = []

        for finding in deduped:
            fid = finding["finding_id"]
            if fid in reconciliation.already_published:
                status = PublishStatus.PUBLISHED
            elif fid in reconciliation.new_findings:
                # Stub: mark as published locally (no live GitHub call)
                status = PublishStatus.PUBLISHED
            elif fid in reconciliation.superseded_findings:
                status = PublishStatus.SUPERSEDED
            elif fid in reconciliation.absent_findings:
                status = PublishStatus.ABSENT
            else:
                status = PublishStatus.SKIPPED

            finding_statuses.append(status)

            if not dry_run:
                entry = build_publish_entry(
                    finding_id=fid,
                    status=status,
                    run_id=run_id,
                    finding_fingerprint=finding.get("finding_fingerprint"),
                )
                # Persist per-finding publish state
                prior_publish.setdefault("findings", {})
                prior_publish["findings"][fid] = entry

        # --- 7. Compute findings counts and rollup status (needed before Phase J) ---
        findings_total = len(deduped)
        findings_published = sum(
            1 for s in finding_statuses if s == PublishStatus.PUBLISHED
        )
        findings_failed = sum(1 for s in finding_statuses if s == PublishStatus.FAILED)
        findings_skipped_count = len(skipped_findings) + sum(
            1
            for s in finding_statuses
            if s in {PublishStatus.SKIPPED, PublishStatus.SUPERSEDED}
        )
        rollup_status = compute_run_publish_status(finding_statuses)

        # --- Phase J: Live publication bridge ---
        # Build summary text first so it can be published to GitHub.
        summary_text = build_review_summary_comment(
            repo=self.repo.config.name,
            run_id=run_id,
            reconciliation=reconciliation,
            run_status=rollup_status.value,
            run_error=None,
            pr_number=pr_number,
        )

        comment_url: Optional[str] = None
        resolved_target_pr: Optional[int] = pr_number  # Already resolved at top of run

        # Ensure runs dict exists before Phase J so that _post_summary_to_github
        # can safely update run entries in-place via prior_publish["runs"][run_id].
        prior_publish.setdefault("runs", {})

        # --- Phase G6: Limited publish filter check ---
        # Only apply filters when in limited mode AND guarded path is active.
        # Shadow mode always bypasses filters (it never publishes anyway).
        # local_only stays local regardless.
        publish_filter_result = _build_pass_filter_result()
        rollout_eligible = False
        attention_recommended = False
        filter_blocked_live = False

        if live_rollout_mode == LiveRolloutMode.LIMITED and guard_enabled:
            publish_filter_result = self._check_limited_publish_filters(
                findings_total=findings_total,
                findings_list=deduped,
                pr_context=pr_context,
            )
            rollout_eligible = publish_filter_result.passed
            attention_recommended = (
                not publish_filter_result.passed
                and publish_filter_result.failed_reason != ""
            )
            filter_blocked_live = not publish_filter_result.passed
        elif live_rollout_mode == LiveRolloutMode.SHADOW:
            # Shadow mode: still run filter check for observability but don't block
            publish_filter_result = self._check_limited_publish_filters(
                findings_total=findings_total,
                findings_list=deduped,
                pr_context=pr_context,
            )
            rollout_eligible = False  # shadow never goes live
            attention_recommended = (
                not publish_filter_result.passed
                and publish_filter_result.failed_reason != ""
            )

        # If filters failed in limited+guarded mode, block live publication
        # by nullifying the target PR (causes _post_summary_to_github to refuse)
        if filter_blocked_live:
            resolved_target_pr = None

        # --- Phase G7: Monitored-rollout safety check ---
        # Check circuit-breaker state before attempting live publication.
        # Only applies to limited+guarded path (shadow/local_only unaffected).
        circuit_allows_live = True
        safety_blocked_live = False
        safety_reason = "circuit-closed"
        safety_state_data = self.state.load_monitored_safety_state(
            self.repo.config.name
        )
        current_safety_state = MonitoredSafetyState.from_dict(safety_state_data)

        if live_rollout_mode == LiveRolloutMode.LIMITED and guard_enabled:
            current_safety_state = self._evaluate_feedback_auto_rollback(run_id)
            circuit_allows_live, safety_reason, current_safety_state = (
                self._check_monitored_safety()
            )
            safety_blocked_live = not circuit_allows_live
            if safety_blocked_live:
                resolved_target_pr = None
            # Recommend operator attention when monitored safety is carrying risk
            if (
                current_safety_state.circuit_open
                or current_safety_state.failure_count > 0
                or current_safety_state.auto_rollback_active
            ):
                attention_recommended = True

        # Guarded live publication: _post_summary_to_github is always called (to
        # record PR resolution errors in the run entry), but the gh API call inside
        # it is gated by the guard. When guard is not enabled the run stays
        # local-only; the guard event already documents the reason.
        if not dry_run:
            comment_url = self._post_summary_to_github(
                summary_text=summary_text,
                run_id=run_id,
                prior_publish=prior_publish,
                target_pr_number=resolved_target_pr,
            )

            # --- Record safety outcome after publish attempt ---
            # Only for limited+guarded path (shadow/local_only skip this)
            if live_rollout_mode == LiveRolloutMode.LIMITED and guard_enabled:
                run_entry_after = prior_publish.get("runs", {}).get(run_id, {})
                gh_status = run_entry_after.get("status", "")
                gh_error = run_entry_after.get("error", "")
                is_success = (
                    gh_status == PublishStatus.PUBLISHED.value
                    and comment_url is not None
                )
                is_failure = gh_status == PublishStatus.FAILED.value or (
                    run_entry_after.get("error")
                    and "target-refused" in run_entry_after.get("error", "")
                )
                if is_success:
                    updated_safety = self._record_publish_success_for_safety(run_id)
                elif is_failure:
                    updated_safety = self._record_publish_failure_for_safety(
                        run_id,
                        failure_reason=gh_error or "unknown-gh-error",
                    )
                else:
                    # Neither success nor failure (stayed local, refused, etc.)
                    updated_safety = current_safety_state
                # Carry updated safety state for artifact fields
                current_safety_state = updated_safety

        # --- Compute lifecycle_phase for run artifact and event ---
        # This makes the run's lifecycle explicit:
        # local-only | shadow | guarded-live-published | guarded-live-refused | guarded-live-failed
        # | safety-blocked
        runs_entries = prior_publish.get("runs", {})
        run_entry = runs_entries.get(run_id, {})
        is_shadow = run_entry.get("shadow", False)

        if is_shadow:
            lifecycle_phase = "shadow-published"
        elif not guard_enabled:
            lifecycle_phase = "guard-disabled"
        elif safety_blocked_live:
            # Circuit breaker blocked live publication; stayed local
            lifecycle_phase = "safety-blocked"
        elif filter_blocked_live:
            # Filters blocked live publication; stayed local/shadow
            lifecycle_phase = "filter-blocked"
        elif comment_url:
            lifecycle_phase = "guarded-live-published"
        elif run_entry.get("status") == PublishStatus.FAILED.value:
            lifecycle_phase = "guarded-live-failed"
        else:
            # Refused (target ambiguity, no open PR, etc.) or stayed local
            lifecycle_phase = "guarded-live-refused"

        operator_action_required = bool(current_safety_state.auto_rollback_active)
        operator_action_summary = None
        suggested_review_care_patch = None
        if current_safety_state.auto_rollback_active:
            operator_action_summary = (
                "disable-guarded-live-review-and-fallback-to-shadow"
            )
            suggested_review_care_patch = {
                "guarded_live_review": False,
                "live_rollout_mode": LiveRolloutMode.SHADOW.value,
            }

        # --- 7. Build run publish entry ---
        # The run entry was already updated in-place by _post_summary_to_github
        # (inside prior_publish["runs"][run_id]) with the gh-result status and error.
        # We read that entry back so the persisted entry reflects the actual
        # publication outcome, not just the local rollup status.
        if not dry_run:
            prior_publish.setdefault("runs", {})
            existing_run_entry = prior_publish["runs"].get(run_id, {})
            gh_status_str = existing_run_entry.get("status", "")
            if gh_status_str in {_s.value for _s in PublishStatus}:
                final_status = PublishStatus(gh_status_str)
            else:
                final_status = rollup_status
            # Preserve the error message set by Phase J's _post_summary_to_github
            gh_error = existing_run_entry.get("error")
            run_publish_entry = build_run_publish_entry(
                status=final_status,
                run_id=run_id,
                findings_total=findings_total,
                findings_published=findings_published,
                findings_failed=findings_failed,
                targeted_pr_number=pr_number,
                lifecycle_phase=lifecycle_phase,
                error=gh_error,
                comment_url=comment_url,
                publish_filter_decision=publish_filter_result.decision,
                publish_filter_reason=publish_filter_result.failed_reason,
                rollout_eligible=rollout_eligible,
                attention_recommended=attention_recommended,
                safety_circuit_open=current_safety_state.circuit_open,
                safety_failure_count=current_safety_state.failure_count,
                safety_cooldown_until=current_safety_state.cooldown_until,
                auto_rollback_active=current_safety_state.auto_rollback_active,
                auto_rollback_reason=current_safety_state.auto_rollback_reason,
                auto_rollback_triggered_at=current_safety_state.auto_rollback_triggered_at,
                operator_action_required=operator_action_required,
                operator_action_summary=operator_action_summary,
                suggested_review_care_patch=suggested_review_care_patch,
            )
            prior_publish["runs"][run_id] = run_publish_entry
            # Preserve shadow flag from _post_summary_to_github's shadow entry
            # (build_run_publish_entry doesn't know about shadow, so we carry it over)
            if existing_run_entry.get("shadow"):
                prior_publish["runs"][run_id]["shadow"] = True
                prior_publish["runs"][run_id]["shadow_summary_text"] = (
                    existing_run_entry.get("shadow_summary_text")
                )
                prior_publish["runs"][run_id]["rollout_mode"] = existing_run_entry.get(
                    "rollout_mode"
                )
        else:
            run_publish_entry = build_run_publish_entry(
                status=rollup_status,
                run_id=run_id,
                findings_total=findings_total,
                findings_published=findings_published,
                findings_failed=findings_failed,
                targeted_pr_number=pr_number,
                lifecycle_phase=lifecycle_phase,
                comment_url=comment_url,
                auto_rollback_active=current_safety_state.auto_rollback_active,
                auto_rollback_reason=current_safety_state.auto_rollback_reason,
                auto_rollback_triggered_at=current_safety_state.auto_rollback_triggered_at,
                operator_action_required=operator_action_required,
                operator_action_summary=operator_action_summary,
                suggested_review_care_patch=suggested_review_care_patch,
            )

        # --- 8. Update result counters ---
        result.findings_detected = len(deduped)
        result.findings_published = findings_published
        result.findings_failed = findings_failed
        result.findings_skipped = findings_skipped_count
        result.findings_absent = len(reconciliation.absent_findings)

        # --- 9. Persist ReviewRun artifact ---
        now = now_iso()

        # Compute run_completion_reason: human-readable summary of why the run ended
        if lifecycle_phase == "shadow-published":
            run_completion_reason = (
                f"shadow: summary would have been posted to PR #{pr_number} "
                f"(live_rollout_mode=shadow; no actual GitHub API call made); "
                f"candidates={candidate_source}"
            )
        elif lifecycle_phase == "guard-disabled":
            run_completion_reason = (
                f"guard-disabled: local-only run; "
                f"guarded_live_review={guard_enabled}, live_actions={live_actions}"
            )
        elif lifecycle_phase == "guarded-live-published":
            run_completion_reason = (
                f"published: summary comment posted to PR #{pr_number} via guarded path; "
                f"candidates={candidate_source}"
            )
        elif lifecycle_phase == "filter-blocked":
            run_completion_reason = (
                f"filter-blocked: publication blocked by limited-publish filters; "
                f"filters_decision={publish_filter_result.decision} "
                f"reason={publish_filter_result.failed_reason!r}; "
                f"candidates={candidate_source}"
            )
        elif lifecycle_phase == "guarded-live-refused":
            runs_entries = prior_publish.get("runs", {})
            run_entry = runs_entries.get(run_id, {})
            refusal_reason = run_entry.get("error", "unknown")
            run_completion_reason = (
                f"refused: publication blocked ({refusal_reason}); "
                f"candidates={candidate_source}"
            )
        elif lifecycle_phase == "guarded-live-failed":
            runs_entries = prior_publish.get("runs", {})
            run_entry = runs_entries.get(run_id, {})
            error_reason = run_entry.get("error", "unknown")
            run_completion_reason = (
                f"failed: publication error ({error_reason}); "
                f"candidates={candidate_source}"
            )
        elif lifecycle_phase == "safety-blocked":
            run_completion_reason = (
                f"safety-blocked: circuit-breaker blocked live publication; "
                f"safety_reason={safety_reason}; "
                f"circuit_open={current_safety_state.circuit_open}; "
                f"failure_count={current_safety_state.failure_count}; "
                f"candidates={candidate_source}"
            )
        else:
            run_completion_reason = (
                f"completed: lifecycle_phase={lifecycle_phase}, "
                f"candidates={candidate_source}"
            )

        review_run_data = {
            "id": run_id,
            "run_id": run_id,
            "repo": self.repo.config.name,
            "pr_number": pr_number,
            "status": "completed",
            "mode": "autonomous-review",
            "compression_mode": CompressionMode.FULL_DIFF.value,
            "token_budget": 0,
            "started_at": now,
            "ended_at": now,
            "findings_total": findings_total,
            "findings_eligible": len(eligible_findings),
            "findings_ineligible": len(ineligible_findings),
            "findings_published": findings_published,
            "findings_failed": findings_failed,
            "findings_skipped": findings_skipped_count,
            "run_publish_status": rollup_status.value,
            "guard_enabled": guard_enabled,
            "guard_reason": guard_reason,
            "live_rollout_mode": live_rollout_mode.value,
            "rollout_reason": rollout_reason,
            "candidate_source": candidate_source,
            "lifecycle_phase": lifecycle_phase,
            "run_completion_reason": run_completion_reason,
            "comment_url": comment_url,
            # Phase G6: publish filter monitoring signals
            "publish_filter_decision": publish_filter_result.decision,
            "publish_filter_reason": publish_filter_result.failed_reason,
            "rollout_eligible": rollout_eligible,
            "attention_recommended": attention_recommended,
            # Phase G7: monitored-rollout safety signals
            "safety_circuit_open": current_safety_state.circuit_open,
            "safety_failure_count": current_safety_state.failure_count,
            "safety_cooldown_until": current_safety_state.cooldown_until,
            "safety_last_failure_reason": current_safety_state.last_failure_reason,
            "auto_rollback_active": current_safety_state.auto_rollback_active,
            "auto_rollback_reason": current_safety_state.auto_rollback_reason,
            "auto_rollback_triggered_at": current_safety_state.auto_rollback_triggered_at,
            "operator_action_required": operator_action_required,
            "operator_action_summary": operator_action_summary,
            "suggested_review_care_patch": suggested_review_care_patch,
            "reconciliation": {
                "new": reconciliation.new_findings,
                "already_published": reconciliation.already_published,
                "absent": reconciliation.absent_findings,
                "superseded": reconciliation.superseded_findings,
                "pending": reconciliation.pending_findings,
            },
            "error": None,
        }

        summary_prompt_path: Optional[Path] = None
        if not dry_run:
            self.state.save_review_run(self.repo.config.name, review_run_data)
            # Persist findings to both JSONL index and individual JSON files
            if deduped:
                self.state.append_review_findings(
                    self.repo.config.name,
                    [{**f, "run_id": run_id} for f in deduped],
                )
            for finding in deduped:
                self.state.save_review_finding(
                    self.repo.config.name,
                    finding["finding_id"],
                    {**finding, "run_id": run_id},
                )
            # Persist summary as local artifact regardless of live publish outcome.
            # The summary is a local-only artifact (not a live GitHub action).
            summary_prompt_path = (
                self.state.get_review_prompts_dir(self.repo.config.name)
                / f"autonomous-run-{run_id}.md"
            )
            summary_prompt_path.parent.mkdir(parents=True, exist_ok=True)
            summary_prompt_path.write_text(summary_text, encoding="utf-8")
            # Persist full publish state AFTER the cycle so reconciliation
            # results and run entries are written through to storage.
            self.state.save_review_publish_state(self.repo.config.name, prior_publish)

        # --- 11. Append review event ---
        if not dry_run:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "autonomous-review-completed",
                    "run_id": run_id,
                    "provider": "local-stub",
                    "details": {
                        "mode": ReviewMode.AUTONOMOUS_REVIEW.value,
                        "findings_total": findings_total,
                        "findings_published": findings_published,
                        "findings_skipped": findings_skipped_count,
                        "findings_absent": result.findings_absent,
                        "rollup_status": rollup_status.value,
                        "lifecycle_phase": lifecycle_phase,
                        "live_rollout_mode": live_rollout_mode.value,
                        "rollout_reason": rollout_reason,
                        "guard_enabled": guard_enabled,
                        "guard_reason": guard_reason,
                        "candidate_source": candidate_source,
                        "resolved_target_pr": resolved_target_pr,
                        "comment_url": comment_url,
                        "summary_file": str(summary_prompt_path)
                        if summary_prompt_path
                        else None,
                        "run_file": f"review_runs/{run_id}.json",
                        # Phase G6: publish filter monitoring signals
                        "publish_filter_decision": publish_filter_result.decision,
                        "publish_filter_reason": publish_filter_result.failed_reason,
                        "rollout_eligible": rollout_eligible,
                        "attention_recommended": attention_recommended,
                        # Phase G7: monitored-rollout safety signals
                        "safety_circuit_open": current_safety_state.circuit_open,
                        "safety_failure_count": current_safety_state.failure_count,
                        "safety_cooldown_until": current_safety_state.cooldown_until,
                        "safety_last_failure_reason": current_safety_state.last_failure_reason,
                        "auto_rollback_active": current_safety_state.auto_rollback_active,
                        "auto_rollback_reason": current_safety_state.auto_rollback_reason,
                        "auto_rollback_triggered_at": current_safety_state.auto_rollback_triggered_at,
                        "operator_action_required": operator_action_required,
                        "operator_action_summary": operator_action_summary,
                        "suggested_review_care_patch": suggested_review_care_patch,
                        "safety_reason": safety_reason,
                    },
                },
            )

            # --- 12. Persist any injected feedback events ---
            flushed = _flush_injected_feedback(
                engine=self,
                repo_name=self.repo.config.name,
                state=self.state,
            )
            if flushed > 0:
                self.state.append_review_event(
                    self.repo.config.name,
                    {
                        "event": "feedback-events-recorded",
                        "count": flushed,
                        "provider": "local-stub",
                        "details": {
                            "injected_via": "inject_feedback_for_autonomous_review"
                        },
                    },
                )

        return result

    # -------------------------------------------------------------------------
    # Backend generation bridge for autonomous-review
    # -------------------------------------------------------------------------
    # Attempts to generate candidate findings via an explicit review backend
    # command (claude/opencode) when configured. Falls back to local stub on
    # failure. Safe: never crashes; all failures are logged and result in
    # fallback. Only runs when dry_run=False.
    # -------------------------------------------------------------------------

    def _generate_from_backend(
        self,
        run_id: str,
        pr_context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Generate candidate findings via a configured review backend command.

        Falls back to an empty list on any failure. The caller should fall
        back to _generate_local_candidates when this returns [].

        Args:
            run_id: Current run ID for event logging.
            pr_context: Optional PR context for targeted analysis.

        Returns:
            List of candidate finding dicts, or empty list on failure.
        """
        candidates: List[Dict[str, Any]] = []

        # --- Check if a review backend template is configured ---
        claude_template = self.repo.config.review_claude_template
        opencode_template = self.repo.config.review_opencode_template

        if not claude_template and not opencode_template:
            # No backend configured; signal caller to use local stub
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation skipped",
                    "run_id": run_id,
                    "reason": "no_review_backend_configured",
                    "provider": "local-stub",
                },
            )
            return candidates

        # --- Resolve which backend to use ---
        backend = self._resolve_backend()
        if backend not in ("claude", "opencode"):
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation skipped",
                    "run_id": run_id,
                    "reason": f"backend_{backend}_not_available",
                    "provider": "local-stub",
                },
            )
            return candidates

        template = (
            claude_template
            if backend == "claude" and claude_template
            else opencode_template
        )

        # --- Write prompt artifact for traceability ---
        prompts_dir = self.state.get_review_prompts_dir(self.repo.config.name)
        prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_artifact_path = prompts_dir / f"backend-candidates-{run_id}.md"

        prompt_artifact_content = self._build_candidate_prompt_artifact(
            pr_context=pr_context
        )
        prompt_artifact_path.write_text(prompt_artifact_content, encoding="utf-8")

        # --- Render and run the backend command ---
        try:
            raw_output = self._run_backend_candidate_command(
                backend=backend,
                template=template,
                prompt_file=prompt_artifact_path,
            )
        except Exception as exc:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation failed",
                    "run_id": run_id,
                    "backend": backend,
                    "error": str(exc),
                    "provider": "local-stub",
                    "details": {
                        "prompt_artifact": str(prompt_artifact_path),
                        "fallback": "local_stub_engaged",
                    },
                },
            )
            return candidates

        # --- Parse JSON payload ---
        if not raw_output.strip():
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation empty",
                    "run_id": run_id,
                    "backend": backend,
                    "provider": "local-stub",
                    "details": {
                        "prompt_artifact": str(prompt_artifact_path),
                        "fallback": "local_stub_engaged",
                    },
                },
            )
            return candidates

        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError as jexc:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation invalid-json",
                    "run_id": run_id,
                    "backend": backend,
                    "json_error": str(jexc),
                    "provider": "local-stub",
                    "details": {
                        "prompt_artifact": str(prompt_artifact_path),
                        "fallback": "local_stub_engaged",
                    },
                },
            )
            return candidates

        # --- Normalize and validate candidates ---
        # Accept either a dict with a "findings" key or a bare list
        raw_candidates: List[Dict[str, Any]] = []
        if isinstance(parsed, dict):
            raw_candidates = parsed.get("findings", [])
            if not isinstance(raw_candidates, list):
                raw_candidates = []
        elif isinstance(parsed, list):
            raw_candidates = parsed
        else:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation unexpected-type",
                    "run_id": run_id,
                    "backend": backend,
                    "type": type(parsed).__name__,
                    "provider": "local-stub",
                    "details": {
                        "prompt_artifact": str(prompt_artifact_path),
                        "fallback": "local_stub_engaged",
                    },
                },
            )
            return candidates

        # Validate each candidate through the Phase E layer
        validated: List[Dict[str, Any]] = []
        for raw in raw_candidates:
            try:
                normalized = normalize_candidate(raw)
                validated.append(normalized)
            except CandidateValidationError:
                # Skip invalid candidates but continue processing
                continue

        if not validated:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation no-valid-candidates",
                    "run_id": run_id,
                    "backend": backend,
                    "raw_count": len(raw_candidates),
                    "provider": "local-stub",
                    "details": {
                        "prompt_artifact": str(prompt_artifact_path),
                        "fallback": "local_stub_engaged",
                    },
                },
            )
            return candidates

        self.state.append_review_event(
            self.repo.config.name,
            {
                "event": "backend-generation succeeded",
                "run_id": run_id,
                "backend": backend,
                "candidate_count": len(validated),
                "provider": "backend",
                "details": {
                    "prompt_artifact": str(prompt_artifact_path),
                },
            },
        )

        return validated

    def _build_candidate_prompt_artifact(
        self,
        pr_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Build the prompt artifact instructing the backend to output structured JSON findings.

        Args:
            pr_context: Optional PR context for targeted analysis.

        Returns:
            Markdown prompt string.
        """
        repo_name = self.repo.config.name
        repo_path = self.repo.config.path
        language = self.repo.config.language or "unknown"

        pr_section = ""
        if pr_context and pr_context.get("pr_number") is not None:
            pr_num = pr_context["pr_number"]
            pr_section = (
                f"\n"
                f"## PR Context\n"
                f"- PR Number: #{pr_num}\n"
                f"- This review run is targeting PR #{pr_num}.\n"
                f"- Focus analysis on files changed in this PR and their interactions.\n"
            )
        mnemo_section = self._build_mnemo_review_context(pr_context=pr_context)

        return (
            f"# Autonomous Review Candidate Generation\n"
            f"\n"
            f"## Repository\n"
            f"- Name: {repo_name}\n"
            f"- Path: {repo_path}\n"
            f"- Language: {language}\n"
            f"{pr_section}"
            f"{mnemo_section}"
            f"\n"
            f"## Task\n"
            f"Scan the repository at `{repo_path}` and identify candidate findings "
            f"suitable for autonomous review. Each finding should represent a "
            f"verifiable quality or correctness issue.\n"
            f"\n"
            f"## Output Format\n"
            f"Output ONLY a valid JSON array of finding objects. Each object must "
            f"have these fields:\n"
            f"- repo (string): repository name\n"
            f"- path (string): relative file path\n"
            f"- line (integer): line number\n"
            f"- header (string): short finding type identifier\n"
            f"- snippet (string): relevant code/text excerpt (max 200 chars)\n"
            f"- source (string): one of linter, ai, manual, unknown\n"
            f"- actionability (string): informational, low, medium, high\n"
            f"- severity (string): none, low, medium, high, critical\n"
            f"- confidence (float): 0.0 to 1.0\n"
            f"- safe_to_autofix (boolean): whether auto-fix is safe\n"
            f"- discovered_at (string): ISO timestamp\n"
            f"\n"
            f"Example:\n"
            f"```json\n"
            f'[{{"repo": "{repo_name}", "path": "src/main.ts", "line": 10, '
            f'"header": "outstanding-todo", "snippet": "# TODO: fix this", '
            f'"source": "linter", "actionability": "medium", '
            f'"severity": "low", "confidence": 0.7, "safe_to_autofix": false, '
            f'"discovered_at": "2026-03-29T00:00:00Z"}}]\n'
            f"```\n"
            f"\n"
            f"Output nothing else. Start directly with the JSON array."
        )

    def _run_backend_candidate_command(
        self,
        backend: str,
        template: str,
        prompt_file: Path,
    ) -> str:
        """Render and execute a backend candidate generation command.

        Args:
            backend: Backend name (claude or opencode).
            template: Command template string with {prompt_file} placeholder.
            prompt_file: Path to the prompt artifact.

        Returns:
            Stdout from the backend command.

        Raises:
            RuntimeError: If the command exits non-zero.
        """
        # Format the template with the prompt file path
        cmd_str = template.format(prompt_file=str(prompt_file))

        # Execute via shell
        result = subprocess.run(
            cmd_str,
            shell=True,
            cwd=str(self.provider.repo_path),
            text=True,
            capture_output=True,
            check=False,
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            raise RuntimeError(
                f"backend command exited {result.returncode}; "
                f"stderr: {stderr or 'none'}; stdout: {stdout or 'none'}"
            )

        return result.stdout or ""

    # -------------------------------------------------------------------------
    # Candidate source stub — safe local-only source for autonomous review
    # -------------------------------------------------------------------------

    def _generate_local_candidates(self) -> List[Dict[str, Any]]:
        """Generate structured candidate findings from local repo files (no LLM/network).

        Scans for TODO/FIXME/BUG markers and excessively long lines. Override
        in tests to inject synthetic candidates.
        """
        candidates: List[Dict[str, Any]] = []
        repo_path = Path(self.repo.config.path)
        if not repo_path.exists():
            return candidates

        # Scan for TODO/FIXME/BUG markers — lightweight signal for review
        for py_file in repo_path.rglob("*.py"):
            try:
                for i, line in enumerate(
                    py_file.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    stripped = line.strip()
                    marker = None
                    if stripped.startswith("# TODO:"):
                        marker = "outstanding-todo"
                    elif stripped.startswith("# FIXME:"):
                        marker = "unresolved-fixme"
                    elif stripped.startswith("# BUG:") or stripped.startswith("# BUG:"):
                        marker = "documented-bug"
                    elif "raise NotImplementedError" in stripped:
                        marker = "not-implemented-raise"
                    elif "pass  # TODO" in stripped or "..." in stripped:
                        # Common placeholder pattern
                        if i > 1:
                            marker = "code-placeholder"

                    if marker:
                        snippet = stripped[:120]
                        candidates.append(
                            {
                                "repo": self.repo.config.name,
                                "path": str(py_file.relative_to(repo_path)),
                                "line": i,
                                "header": marker,
                                "snippet": snippet,
                                "source": FindingSource.LINTER.value,
                                "actionability": FindingActionability.MEDIUM.value,
                                "severity": FindingSeverity.LOW.value,
                                "confidence": 0.7,
                                "safe_to_autofix": False,  # Requires human review
                                "discovered_at": now_iso(),
                            }
                        )
            except (OSError, UnicodeDecodeError):
                continue

        # Scan for excessively long lines (>120 chars) in any text-like file
        for src_file in repo_path.rglob("*.py"):
            try:
                lines = src_file.read_text(encoding="utf-8").splitlines()
                for i, line in enumerate(lines, start=1):
                    if len(line) > 120 and not line.strip().startswith("#"):
                        candidates.append(
                            {
                                "repo": self.repo.config.name,
                                "path": str(src_file.relative_to(repo_path)),
                                "line": i,
                                "header": "excessively-long-line",
                                "snippet": line[:120],
                                "source": FindingSource.LINTER.value,
                                "actionability": FindingActionability.LOW.value,
                                "severity": FindingSeverity.LOW.value,
                                "confidence": 0.6,
                                "safe_to_autofix": True,
                                "discovered_at": now_iso(),
                            }
                        )
            except (OSError, UnicodeDecodeError):
                continue

        return candidates

    # -------------------------------------------------------------------------
    # Remediation cycle — stub; logs event, returns neutral result
    # -------------------------------------------------------------------------

    def _run_remediation_cycle(
        self, dry_run: bool = True, allow_review_push: bool = False
    ) -> ReviewCycleResult:
        """Remediation is not yet implemented; logs and returns neutral."""
        if not dry_run:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "remediation-not-implemented",
                    "provider": "github",
                    "details": {
                        "mode": ReviewMode.REMEDIATION.value,
                        "message": (
                            "remediation mode is not yet implemented; "
                            "no observation logic was run"
                        ),
                    },
                },
            )
        return ReviewCycleResult()


# ---------------------------------------------------------------------------
# Phase E1 + E2: Candidate Finding Validation / Normalization Layer
# ---------------------------------------------------------------------------
# Local-only helpers for autonomous review artifacts.
# No LLM call plumbing; no GitHub publish logic.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
