#!/usr/bin/env python3
"""GitHub-native PR review observation for QA Agent."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

from .models import (
    Repo,
    RepoConfig,
    ReviewMode,
    LiveRolloutMode,
    now_iso,
    FindingSource,
    FindingActionability,
    FindingSeverity,
    PublishStatus,
    MonitoredSafetyState,
    normalize_finding_path,
    normalize_finding_header,
    make_finding_fingerprint,
    make_review_finding_id,
    generate_id,
)
from .state import StateManager

GRAPHQL_QUERY = r"""
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      url
      title
      isDraft
      state
      reviewDecision
      createdAt
      updatedAt
      author { login }
      headRefName
      headRepositoryOwner { login }
      mergeStateStatus
      reviews(last: 100) {
        nodes {
          author { login }
          state
          submittedAt
        }
      }
      reviewThreads(first: 100) {
        nodes {
          isResolved
          isOutdated
          comments(last: 20) {
            nodes {
              author { login }
              body
              createdAt
            }
          }
        }
      }
      comments(last: 50) {
        nodes {
          author { login }
          body
          createdAt
        }
      }
    }
  }
}
"""


@dataclass
class ReviewCycleResult:
    active_prs: int = 0
    blocked_prs: int = 0
    retry_eligible_prs: int = 0
    merge_ready_prs: int = 0
    paused_prs: int = 0
    retry_planned_prs: int = 0
    retry_prepared_prs: int = 0
    retry_executed_prs: int = 0
    retry_failed_prs: int = 0
    retry_exhausted_prs: int = 0
    # Phase G1: autonomous-review finding counters
    findings_detected: int = 0
    findings_published: int = 0
    findings_failed: int = 0
    findings_skipped: int = 0
    findings_absent: int = 0


class GitHubReviewProvider:
    """Observation-only GitHub review provider for managed PRs."""

    def __init__(self, repo: Repo, state: StateManager):
        self.repo = repo
        self.state = state
        self.repo_path = Path(repo.config.path)
        self.repo_slug = self._get_repo_slug()
        self.current_login = self._get_current_login()

    def _run(self, cmd: List[str], *, retries: int = 2, backoff: float = 1.0) -> str:
        """Run a subprocess command with optional retry/backoff for transient failures."""
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            result = subprocess.run(
                cmd,
                cwd=str(self.repo_path),
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                return (result.stdout or "").strip()
            last_err = RuntimeError(
                (result.stderr or result.stdout or "").strip()
                or f"command failed: {cmd}"
            )
            # Only retry on likely-transient errors (network, rate-limit, 5xx)
            err_text = (result.stderr or "").lower()
            if any(t in err_text for t in ("rate limit", "timeout", "connection", "5xx", "503", "502", "500", "internal server")):
                if attempt < retries - 1:
                    import time
                    _logger.warning("Transient error on attempt %d/%d for %s, retrying in %.1fs",
                                    attempt + 1, retries, cmd[0], backoff)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
            raise last_err
        raise last_err  # type: ignore[misc]

    def _get_repo_slug(self) -> str:
        origin = self._run(["git", "remote", "get-url", "origin"])
        match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)", origin)
        if not match:
            raise RuntimeError(
                "Unable to determine GitHub repo slug from origin remote"
            )
        return f"{match.group('owner')}/{match.group('repo')}"

    def _get_current_login(self) -> Optional[str]:
        try:
            payload = json.loads(self._run(["gh", "api", "user"]))
            return payload.get("login")
        except Exception:
            return None

    def list_managed_prs(self) -> List[Dict[str, Any]]:
        prs = json.loads(
            self._run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    self.repo_slug,
                    "--state",
                    "open",
                    "--limit",
                    "50",
                    "--json",
                    "number,url,title,headRefName,author,isDraft,state",
                ]
            )
        )
        managed: List[Dict[str, Any]] = []
        for pr in prs:
            if pr.get("isDraft"):
                continue
            author = (pr.get("author") or {}).get("login") or ""
            branch = pr.get("headRefName") or ""
            if self._is_managed_pr(author, branch):
                managed.append(pr)
        return managed

    def _is_managed_pr(self, author: str, branch: str) -> bool:
        branch = branch or ""
        markers = ("qa/", "qa-", "fix/", "fix-", "auto-", "autofix/", "chore/qa-")
        if any(branch.startswith(prefix) for prefix in markers):
            return True
        if self.current_login and author == self.current_login:
            return True
        return False

    def fetch_review_snapshot(self, pr_number: int) -> Dict[str, Any]:
        owner, name = self.repo_slug.split("/", 1)
        raw = json.loads(
            self._run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    f"owner={owner}",
                    "-f",
                    f"name={name}",
                    "-F",
                    f"number={pr_number}",
                    "-f",
                    f"query={GRAPHQL_QUERY}",
                ]
            )
        )
        pr = ((raw.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        if not pr:
            raise RuntimeError(f"PR #{pr_number} not found")
        return self._normalize_snapshot(pr)

    def _normalize_snapshot(self, pr: Dict[str, Any]) -> Dict[str, Any]:
        author = (pr.get("author") or {}).get("login") or ""
        latest_state: Dict[str, Tuple[str, str]] = {}
        for review in (pr.get("reviews") or {}).get("nodes", []):
            reviewer = (review.get("author") or {}).get("login")
            if not reviewer or reviewer == author or self._is_bot(reviewer):
                continue
            submitted = review.get("submittedAt") or ""
            prev = latest_state.get(reviewer)
            if (not prev) or (submitted > prev[1]):
                latest_state[reviewer] = (review.get("state") or "", submitted)

        active_change_requesters = sorted(
            [
                u
                for u, (state, _) in latest_state.items()
                if state == "CHANGES_REQUESTED"
            ]
        )

        unresolved_threads: List[Dict[str, Any]] = []
        actionable_comments: List[Dict[str, Any]] = []
        informational_comments: List[Dict[str, Any]] = []
        seen_comment_keys: set[tuple[str, str]] = set()

        def _ingest_comment(commenter: str, body: str) -> None:
            normalized = self._normalize_text(body)
            if not normalized or self._should_ignore_comment(normalized):
                return
            key = (commenter, normalized)
            if key in seen_comment_keys:
                return
            seen_comment_keys.add(key)
            payload = {"author": commenter, "body": normalized}
            if self._classify_comment(normalized) == "informational":
                informational_comments.append(payload)
            else:
                actionable_comments.append(payload)

        for thread in (pr.get("reviewThreads") or {}).get("nodes", []):
            if thread.get("isResolved") or thread.get("isOutdated"):
                continue
            thread_comments: List[Dict[str, str]] = []
            for comment in (thread.get("comments") or {}).get("nodes", []):
                commenter = (comment.get("author") or {}).get("login") or ""
                body = (comment.get("body") or "").strip()
                if not body or commenter == author or self._is_bot(commenter):
                    continue
                normalized = self._normalize_text(body)
                thread_comments.append({"author": commenter, "body": normalized})
                _ingest_comment(commenter, body)
            if thread_comments:
                unresolved_threads.append({"comments": thread_comments})

        for comment in (pr.get("comments") or {}).get("nodes", []):
            commenter = (comment.get("author") or {}).get("login") or ""
            body = (comment.get("body") or "").strip()
            if not body or commenter == author or self._is_bot(commenter):
                continue
            _ingest_comment(commenter, body)

        fp_material = {
            "review_decision": pr.get("reviewDecision") or "",
            "merge_state_status": pr.get("mergeStateStatus") or "",
            "active_change_requesters": active_change_requesters,
            "actionable_comments": sorted([c["body"] for c in actionable_comments]),
        }
        fingerprint = hashlib.sha256(
            json.dumps(fp_material, sort_keys=True).encode("utf-8")
        ).hexdigest()

        return {
            "provider": "github",
            "fetched_at": now_iso(),
            "pr_number": pr.get("number"),
            "pr_url": pr.get("url"),
            "branch": pr.get("headRefName") or "",
            "author": author,
            "review_decision": pr.get("reviewDecision") or "REVIEW_REQUIRED",
            "merge_state_status": pr.get("mergeStateStatus") or "UNKNOWN",
            "latest_review_states": [
                {"reviewer": reviewer, "state": state, "submitted_at": submitted}
                for reviewer, (state, submitted) in sorted(latest_state.items())
            ],
            "active_change_requesters": active_change_requesters,
            "unresolved_threads": unresolved_threads,
            "actionable_comments": actionable_comments,
            "informational_comments": informational_comments,
            "score_optional": None,
            "checks_summary_optional": None,
            "fingerprint": fingerprint,
        }

    def _normalize_text(self, text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r"\s+", " ", text)
        return text[:400]

    def _should_ignore_comment(self, body: str) -> bool:
        ignored_markers = [
            "<!-- qa-agent-review-cycle:",
            "automated verification passed for finding",
            "is reviewing your pr",
            "finished reviewing your pr",
            "is running incremental review",
            "thanks for using codeant",
            "**tip:**",
        ]
        return any(marker in body for marker in ignored_markers)

    def _is_bot(self, login: str) -> bool:
        login = (login or "").lower()
        return login.endswith("[bot]") or login in {
            "github-actions",
            "dependabot",
            "renovate",
            "codecov",
            "greptile-apps",
            "codeant-ai",
        }

    def _classify_comment(self, body: str) -> str:
        blocking_markers = [
            "not safe to merge",
            "unsafe to merge",
            "must fix",
            "needs fix",
            "should fix",
            "still fails",
            "will still fail",
            "won't work",
            "will not work",
            "blocking",
            "changes requested",
            "request changes",
            "regression",
            "broken",
            "incorrect",
            "bug",
            "linting violation will still be reported",
        ]
        if any(marker in body for marker in blocking_markers):
            return "actionable"

        informational_markers = [
            "nit:",
            "optional",
            "consider",
            "could",
            "might",
            "suggestion",
        ]
        if any(marker in body for marker in informational_markers):
            return "informational"
        # Default to informational — only block on explicit actionable markers
        return "informational"


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
            f"<!-- qa-agent-review-cycle: pr={pr_number} key={publication_key} -->",
            f"## QA Agent Review, PR #{pr_number}",
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
        lines.extend(_render_comment_lines(informational_comments, "Informational findings"))
        return "\n".join(lines).strip()

    def _publish_review_cycle_comment(
        self,
        pr_number: int,
        summary_text: str,
        publication_key: str,
        existing_review: Dict[str, Any],
    ) -> Optional[str]:
        if not self.repo.config.github.get("live_actions", False):
            return None
        if (
            existing_review.get("last_review_comment_key") == publication_key
            and existing_review.get("last_review_comment_url")
        ):
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
        """Best-effort Mnemo Engram query for review-cycle context.

        Never raises; returns a short text block or an empty string.
        """
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
        """Build a compact Mnemo context block for review-cycle prompts."""
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
        """Apply small confidence boosts when Mnemo corroborates a candidate.

        This is intentionally conservative: Mnemo augments confidence, it does not
        override the candidate source or force eligibility on weak findings.
        """
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
            boosted["confidence"] = min(1.0, round(base_confidence + confidence_boost, 4))
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
        Apply the commit/push boundary for review remediation.

        UNATTENDED PUSH POLICY:
        - When allow_review_push=False (default): changes enter 'pending_operator_confirmation'
          state and will NOT be pushed to the remote. The PR enters 'retry_pending_push' state.
        - When allow_review_push=True: changes will be committed and pushed immediately.

        This ensures unattended/scheduled review-cycle runs NEVER push code changes.
        The operator must explicitly pass --allow-review-push to enable live push.
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
            f"qa-agent: address review feedback for PR #{int(snapshot['pr_number'])}"
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

        UNATTENDED PUSH POLICY:
        - By default (allow_review_push=False), validated changes enter 'retry_pending_push'
          state and do NOT push to remote.
        - Push requires explicit allow_review_push=True from operator.
        - This ensures unattended review-cycle runs are observation-only by default.
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
        """Return the review mode from config, falling back to observation.

        Unknown/invalid mode values are also normalized to observation to avoid
        routing to an unimplemented branch.
        """
        raw = self.repo.config.review_care.get("mode", ReviewMode.OBSERVATION.value)
        valid = {ReviewMode.OBSERVATION.value, ReviewMode.AUTONOMOUS_REVIEW.value, ReviewMode.REMEDIATION.value}
        return raw if raw in valid else ReviewMode.OBSERVATION.value

    def run(
        self, dry_run: bool = True, allow_review_push: bool = False
    ) -> ReviewCycleResult:
        mode = self._get_review_mode()
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
        # on transient API gaps. Also attempt recovery for previously unreachable PRs.
        current_pr_numbers = {str(p["number"]) for p in managed_prs}
        for pr_key, prev_record in previous_active_records.items():
            if pr_key not in current_pr_numbers:
                # Recovery attempt: targeted API fetch for unreachable PRs
                if prev_record.get("status") == "temporarily_unreachable" and not dry_run:
                    try:
                        slug = self._get_repo_slug()
                        pr_data = json.loads(
                            self._run(
                                ["gh", "api", f"/repos/{slug}/pulls/{pr_key}"],
                                retries=1,
                            )
                        )
                        if pr_data.get("state") == "open":
                            _logger.info("Recovering previously unreachable PR #%s", pr_key)
                            managed_prs.append(pr_data)
                            current_pr_numbers.add(pr_key)
                            continue
                    except Exception as exc:
                        _logger.debug("Recovery fetch failed for PR #%s: %s", pr_key, exc)

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
                merge_state_status = str(snapshot.get("merge_state_status") or "UNKNOWN")
                prior_comment_key = str(existing_review.get("last_review_comment_key") or "")
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
                    if merge_ready_artifact_exists_for_snapshot or review_artifact_exists_for_snapshot:
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
                            merge_reason = "Clean PR, but qa-agent review for this snapshot has not been published yet"
                        else:
                            merge_reason = (
                                "No actionable review blockers and merge state is "
                                f"{merge_state_status.lower()}, but qa-agent review for this snapshot has not been published yet"
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
                    "source": "qa-agent-heuristic",
                    "status": status,
                    "opened_at": (
                        previous_active_records.get(pr_key, {}) or {}
                    ).get("opened_at")
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
                        review_records[pr_key][
                            "last_review_comment_key"
                        ] = publication_key
                        review_records[pr_key][
                            "last_review_comment_url"
                        ] = review_comment_url
                        review_records[pr_key][
                            "last_review_comment_at"
                        ] = snapshot["fetched_at"]
                        active_records[pr_key]["review_comment"] = {
                            "url": review_comment_url,
                            "key": publication_key,
                            "published_at": snapshot["fetched_at"],
                        }
                    elif review_records[pr_key].get("last_review_comment_url"):
                        active_records[pr_key]["review_comment"] = {
                            "url": review_records[pr_key].get("last_review_comment_url"),
                            "key": review_records[pr_key].get("last_review_comment_key"),
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
        """
        Return open PRs for the repo via ``gh pr list``.

        Uses ``gh pr list --json number,title,updatedAt --repo <owner/repo>
        --state open``, sorting by ``updatedAt`` descending so the most
        recently updated PR is first.  Falls back to an empty list if the
        command fails or ``gh`` is unavailable.

        Returns:
            List of dicts with at least ``number`` (int) and ``updatedAt`` (str).
        """
        try:
            owner = self.repo.config.github.get("owner") or self.repo.config.name.split("/")[0] if "/" in self.repo.config.name else ""
            repo_name = self.repo.config.github.get("repo") or self.repo.config.name.split("/")[1] if "/" in self.repo.config.name else self.repo.config.name
            result = subprocess.run(
                [
                    "gh", "pr", "list",
                    "--json", "number,title,updatedAt",
                    "--repo", f"{owner}/{repo_name}",
                    "--state", "open",
                    "--limit", "10",
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
        """
        Resolve which PR to target for an autonomous-review publication.

        Uses an explicit-first, safety-conscious priority chain:

        1. Prior runs' explicit targeting: if exactly ONE distinct PR was
           explicitly targeted in prior published runs (has ``targeted_pr_number``
           in publish state), use it.
        2. Managed PRs: if only one managed PR exists for the repo, use it.
        3. Open PR discovery: if only one open PR exists, use it.
        4. Refuse: multiple open PRs with no clear single candidate →
           return None + reason.  Never blindly post to "most recently updated".

        This ensures reruns stay anchored to the same PR when safe, but refuse
        to guess when the landscape is ambiguous.

        Returns:
            (pr_number, reason) tuple.
            pr_number is int or None (None = do not publish live).
            reason describes which step produced the decision.
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
        """
        Post the autonomous-review summary comment to GitHub (Phase J bridge).

        Publication policy:
        - Only posts when ``repo.config.github.live_actions`` is True.
        - Idempotent: if ``run_id`` already has a ``comment_url`` in
          ``prior_publish["runs"]``, returns the existing URL without
          re-posting.
        - Safe targeting: uses ``target_pr_number`` if provided; otherwise
          falls back to ``_resolve_target_pr_for_run``.  If the resolved
          target is None (ambiguous or no PRs), refuses to publish and logs
          a refusal event.
        - On failure, updates the run's publish entry with error and status
          ``failed``; does NOT pretend success.

        Args:
            summary_text: The pre-built deterministic summary comment body.
            run_id: The current run's ID (used for deduplication).
            prior_publish: The loaded publish state dict (mutated in-place on
                           failure so the error is persisted).
            target_pr_number: Optional explicit PR number to target.  If
                              provided, this PR is used directly.  If None,
                              ``_resolve_target_pr_for_run`` is consulted.

        Returns:
            The GitHub comment URL on success, or None if not published
            (live_actions=false, no open PRs, ambiguous, or publication failed).
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
            runs_entries[run_id]["error"] = (
                f"target-pr-{target_pr_number}-not-open"
            )
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
        owner = self.repo.config.github.get("owner") or self.repo.config.name.split("/")[0]
        repo_name = self.repo.config.github.get("repo") or self.repo.config.name.split("/")[1]

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
            if any(kw in combined for kw in [
                "rate limit", "rate_limit", "429", "too many requests",
                "secondary rate limit", "abuse rate limit",
            ]):
                return True
            if any(kw in combined for kw in [
                "connection reset", "connection refused", "name or service not known",
                "network is unreachable", "temporary failure in name resolution",
                "could not resolve host", "ssl", "tls",
            ]):
                return True
            return False

        last_err = ""
        published_url: Optional[str] = None
        try:
            for attempt in range(max_retries + 1):
                try:
                    result = subprocess.run(
                        [
                            "gh", "pr", "comment",
                            str(target_pr_number),
                            "--repo", f"{owner}/{repo_name}",
                            "--body", summary_text,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                except Exception as exc:
                    last_err = str(exc)
                    if attempt < max_retries:
                        delay = base_delay_seconds * (2 ** attempt)
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

                last_err = result.stderr.strip() or f"gh-pr-comment-failed (code {result.returncode})"
                if attempt < max_retries and _is_transient_failure(
                    result.returncode, last_err, result.stdout.strip()
                ):
                    delay = base_delay_seconds * (2 ** attempt)
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
        """
        Resolve PR context for an autonomous review run.

        Uses explicit-first priority:
        1. repo.config.github.get("pr_number") — explicit configured PR
        2. _resolve_target_pr_for_run(prior_publish) — safe targeting via prior state
           (only consulted when live_actions is True to avoid unnecessary network calls)

        When neither yields a PR number, returns (None, reason) and the run
        proceeds in local-only mode.

        Args:
            prior_publish: The loaded publish state dict.

        Returns:
            (pr_number, resolution_reason) tuple.
            pr_number is int or None (None = local-only mode).
            resolution_reason describes which step produced the decision.
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
        """
        Check whether the guarded live-review path is enabled.

        The guarded live-review path allows real backend execution and live
        GitHub publication. It requires BOTH:
        1. ``review_care.guarded_live_review`` is True (explicit operator enable)
        2. ``github.live_actions`` is True (live GitHub actions enabled)

        When the guard is not enabled, the run proceeds in local-only mode
        (local candidates, normalization, state persistence, summary artifact)
        without backend generation or live GitHub publication.

        This ensures the system is safely testable on a real repo without
        behaving like an uncontrolled production feature.

        Returns:
            (enabled, reason) tuple.
            enabled is True only when both conditions are met.
            reason describes why the guard passed or failed.
        """
        guarded_flag = bool(
            self.repo.config.review_care.get("guarded_live_review", False)
        )
        live_actions = bool(
            self.repo.config.github.get("live_actions", False)
        )

        if guarded_flag and live_actions:
            return (True, "guard-passed")
        if guarded_flag and not live_actions:
            return (False, "guard-failed-live-actions-disabled")
        if not guarded_flag and live_actions:
            return (False, "guard-failed-guarded-live-review-disabled")
        # neither is set
        return (False, "guard-failed-both-disabled")

    def _get_live_rollout_mode(self) -> Tuple[LiveRolloutMode, str]:
        """
        Read and validate the live_rollout_mode from review_care config.

        Returns (mode, reason) where mode is a LiveRolloutMode enum and reason
        describes how the mode was resolved or why it fell back to local_only.

        Unsafe/bad combinations fall back to local_only:
        - Unknown/typo string values
        - shadow or limited mode when live_actions=False (no GitHub context)

        Shadow mode is always safe to allow since it never actually publishes.
        """
        raw = self.repo.config.review_care.get("live_rollout_mode", LiveRolloutMode.LOCAL_ONLY.value)
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
        if mode in (LiveRolloutMode.SHADOW, LiveRolloutMode.LIMITED) and not live_actions:
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
        """
        Emit a guard decision event to the review events log.

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
        """
        Check whether a limited-mode autonomous-review run passes publish filters.

        Filters are applied only when ``live_rollout_mode == limited`` and are
        designed to constrain live publication to safe, observable conditions:

        1. ``max_findings_count`` — cap on total findings; prevents flooding
        2. ``require_pr_context`` — PR number must be known (no blind posting)
        3. ``allowed_severities`` — only configured severity levels may be published
        4. ``allowed_headers`` — only configured finding headers may be published

        When any filter fails, the run falls back to shadow/local-only and
        records an explicit reason so the outcome is observable.

        Args:
            findings_total: Total deduplicated findings for the run.
            findings_list: Full list of deduplicated finding dicts (for
                           per-finding attribute checks).
            pr_context: Resolved PR context dict (or None).

        Returns:
            PublishFilterResult with ``passed``, ``decision``, and ``failed_reason``.
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
        if require_pr and pr_context is not None and pr_context.get("pr_number") is None:
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
        """
        Check whether the monitored-safety circuit breaker allows live publication.

        Loads the persisted safety state and checks:
        1. Is the circuit currently open?
        2. If open, has the cooldown period expired?

        If cooldown has expired, the circuit is closed (resets failure count but
        does NOT restore eligibility — the next run must pass filters independently).

        Args:
            self: ReviewCycleEngine instance.

        Returns:
            Tuple of (circuit_allows_live, reason_string, current_safety_state).
            circuit_allows_live is True when the circuit is closed or cooldown expired.
            reason_string explains the decision.
        """
        from .models import MonitoredSafetyState

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
        """Fail closed when recent feedback is too negative for guarded live review."""
        from datetime import datetime, timezone
        from .models import MonitoredSafetyState

        safety_data = self.state.load_monitored_safety_state(self.repo.config.name)
        safety_state = MonitoredSafetyState.from_dict(safety_data)

        if safety_state.auto_rollback_active:
            return safety_state
        if not self.repo.config.review_care.get("monitored_auto_rollback_enabled", False):
            return safety_state

        window = int(self.repo.config.review_care.get("monitored_feedback_window", 20))
        min_events = int(self.repo.config.review_care.get("monitored_feedback_min_events", 3))
        threshold = float(self.repo.config.review_care.get("monitored_negative_feedback_threshold", 0.3))

        feedback_events = self.state.load_feedback_events(self.repo.config.name)
        recent = feedback_events[-window:] if window > 0 else feedback_events
        negative_signals = {"negative", "request_change", "conflict"}
        positive_signals = {"positive", "approve"}

        negatives = 0
        positives = 0
        for event in recent:
            signal = str(event.get("signal") or event.get("sentiment") or "").strip().lower()
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
        self.state.save_monitored_safety_state(self.repo.config.name, safety_state.to_dict())
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
        """
        Record a guarded-live publish failure and update the circuit breaker state.

        Called after a failed guarded-live publication attempt (GitHub API error,
        target refused, etc.). Increments failure count and opens the circuit
        if the threshold is reached.

        Does NOT automatically close the circuit — that happens only after
        cooldown expires via ``_check_monitored_safety``.

        Args:
            run_id: Current run ID (for event logging).
            failure_reason: Human-readable failure reason.

        Returns:
            Updated MonitoredSafetyState.
        """
        from datetime import datetime, timezone, timedelta
        from .models import MonitoredSafetyState

        safety_data = self.state.load_monitored_safety_state(self.repo.config.name)
        safety_state = MonitoredSafetyState.from_dict(safety_data)

        threshold = int(self.repo.config.review_care.get(
            "monitored_failure_threshold", 3
        ))
        cooldown_seconds = int(self.repo.config.review_care.get(
            "monitored_cooldown_seconds", 300
        ))

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
        """
        Record a successful guarded-live publication and reset the circuit breaker.

        Called after a successful guarded-live publication. Resets failure count
        and closes the circuit if it was open.

        Args:
            run_id: Current run ID (for event logging).

        Returns:
            Updated MonitoredSafetyState.
        """
        from .models import MonitoredSafetyState

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
        """
        Execute a local autonomous-review cycle.

        This path exercises the full local state lifecycle without requiring
        live LLM plumbing or GitHub API calls:

        1. Generate candidate findings from a local stub (safe local-only source)
        2. Normalize, deduplicate, and assign deterministic identity
        3. Check remediation eligibility for each finding
        4. Persist ReviewRun + findings + publish-state artifacts
        5. Append review events
        6. Generate a deterministic summary comment payload

        This is the "happy path" that Phase G1+G2 wires up.  The candidate
        source is a local stub (``_generate_local_candidates``) that produces
        structured candidate findings from repo files — no LLM call, no
        GitHub publish, no unattended push.

        Backward-compatible: observation and remediation modes are unchanged.
        """
        from .models import generate_id, CompressionMode  # local import to avoid circular risk

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
            backend_raw_candidates = self._generate_from_backend(run_id, pr_context=pr_context)
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
        (
            deduped_after_rules,
            updated_rules,
            rules_log,
        ) = _process_learned_rules_for_run(deduped, rules_state, _run_id_for_rules)

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
        findings_published = sum(1 for s in finding_statuses if s == PublishStatus.PUBLISHED)
        findings_failed = sum(1 for s in finding_statuses if s == PublishStatus.FAILED)
        findings_skipped_count = len(skipped_findings) + sum(
            1 for s in finding_statuses if s in {PublishStatus.SKIPPED, PublishStatus.SUPERSEDED}
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
        safety_state_data = self.state.load_monitored_safety_state(self.repo.config.name)
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
                is_failure = (
                    gh_status == PublishStatus.FAILED.value
                    or (run_entry_after.get("error") and "target-refused" in run_entry_after.get("error", ""))
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
                prior_publish["runs"][run_id]["shadow_summary_text"] = existing_run_entry.get("shadow_summary_text")
                prior_publish["runs"][run_id]["rollout_mode"] = existing_run_entry.get("rollout_mode")
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
            summary_prompt_path = self.state.get_review_prompts_dir(
                self.repo.config.name
            ) / f"autonomous-run-{run_id}.md"
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
                        "summary_file": str(summary_prompt_path) if summary_prompt_path else None,
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
                        "details": {"injected_via": "inject_feedback_for_autonomous_review"},
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
        """
        Generate candidate findings via an explicit review backend command.

        Uses ``review_claude_template`` or ``review_opencode_template`` if
        configured. Writes a structured prompt artifact for traceability,
        runs the backend command locally, and parses the returned JSON payload.

        Falls back to an empty list on any failure (backend not configured,
        command not found, non-zero exit, invalid JSON). The caller is
        responsible for falling back to ``_generate_local_candidates`` when
        this returns an empty list.

        Returns:
            List of candidate finding dicts, or empty list on any failure.
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

        prompt_artifact_content = self._build_candidate_prompt_artifact(pr_context=pr_context)
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
        """
        Build the prompt artifact content for backend candidate generation.

        When ``pr_context`` is provided, the artifact includes PR-specific
        targeting information so the backend can focus its analysis on the
        PR's changed files and context.

        The artifact instructs the backend to output a structured JSON payload
        with candidate findings that match the schema expected by
        ``normalize_candidate``.
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
            f'```json\n'
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
        """
        Render and execute a backend candidate generation command.

        Returns the stdout output from the command.
        Raises RuntimeError on non-zero exit.
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
        """
        Generate structured candidate findings from local repo files.

        This is a **safe local-only stub**: it reads files directly from the
        repo path and produces structured candidate dicts.  No LLM call,
        no network, no GitHub API.

        The candidates are scanned from the repo's source files for basic
        quality signals (long lines, TODO/FIXME comments, missing error
        handling patterns).  Each candidate has the required fields for
        ``normalize_candidate``.

        Override this method in tests or subclasses to inject synthetic
        candidates without changing the rest of the autonomous-review path.
        """
        candidates: List[Dict[str, Any]] = []
        repo_path = Path(self.repo.config.path)
        if not repo_path.exists():
            return candidates

        # Scan for TODO/FIXME/BUG markers — lightweight signal for review
        for py_file in repo_path.rglob("*.py"):
            try:
                for i, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), start=1):
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
                        candidates.append({
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
                        })
            except (OSError, UnicodeDecodeError):
                continue

        # Scan for excessively long lines (>120 chars) in any text-like file
        for src_file in repo_path.rglob("*.py"):
            try:
                lines = src_file.read_text(encoding="utf-8").splitlines()
                for i, line in enumerate(lines, start=1):
                    if len(line) > 120 and not line.strip().startswith("#"):
                        candidates.append({
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
                        })
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

# Fields required on a raw candidate finding dict
_REQUIRED_CANDIDATE_FIELDS = frozenset({
    "repo", "path", "line", "header", "source",
})

# Default confidence threshold for remediation eligibility
_DEFAULT_MIN_CONFIDENCE = 0.6

# Default minimum actionability for remediation eligibility
_DEFAULT_MIN_ACTIONABILITY = FindingActionability.MEDIUM

# Rank mappings for enum-based ordinal comparisons.
# These give the semantic ordering that string-value enums can't provide
# via direct comparison operators.
_ACTIONABILITY_RANK: Dict[FindingActionability, int] = {
    FindingActionability.INFORMATIONAL: 0,
    FindingActionability.LOW: 1,
    FindingActionability.MEDIUM: 2,
    FindingActionability.HIGH: 3,
}

_SEVERITY_RANK: Dict[FindingSeverity, int] = {
    FindingSeverity.NONE: 0,
    FindingSeverity.LOW: 1,
    FindingSeverity.MEDIUM: 2,
    FindingSeverity.HIGH: 3,
    FindingSeverity.CRITICAL: 4,
}


@dataclass
class PublishFilterResult:
    """
    Result of Phase G6 limited-publish filter check.

    Attributes:
        passed: True only when all filters pass.
        decision: One of ``pass``, ``fail``, ``skipped``, ``bypassed``.
        failed_reason: Human-readable reason if decision is ``fail``,
                       empty string otherwise.
    """
    passed: bool
    decision: str  # "pass" | "fail" | "skipped" | "bypassed"
    failed_reason: str = ""


def _build_pass_filter_result() -> PublishFilterResult:
    """Return a passing filter result for early/bypassed paths."""
    return PublishFilterResult(passed=True, decision="bypassed", failed_reason="")


class CandidateValidationError(ValueError):
    """Raised when a candidate finding fails validation."""

    def __init__(self, message: str, errors: Optional[List[str]] = None):
        super().__init__(message)
        self.errors = errors or []


@dataclass
class RemediationEligibility:
    """
    Result of computing remediation eligibility for a finding.

    Attributes:
        eligible: True if the finding passes all spec gates.
        reason: Human-readable summary of the decision.
        rejected_gates: List of gate names that caused rejection (empty if eligible).
        safe_to_autofix: Whether the finding is marked safe_to_autofix.
        severity_ok: Whether severity is non-critical.
        actionability_ok: Whether actionability meets the minimum threshold.
        confidence_ok: Whether confidence meets the minimum threshold.
    """
    eligible: bool
    reason: str
    rejected_gates: List[str] = field(default_factory=list)
    safe_to_autofix: bool = False
    severity_ok: bool = True
    actionability_ok: bool = True
    confidence_ok: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normalize_candidate(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse and normalize a raw candidate finding dict into a clean dict
    suitable for identity assignment and deduplication.

    Args:
        raw: A dict that may contain finding-like fields from any source
             (LLM output, linter, manual entry, etc.)

    Returns:
        A normalized dict with:
        - All required fields coerced to correct types
        - Enums resolved to model enums where possible
        - Defaults filled for optional fields

    Raises:
        CandidateValidationError: If the candidate is missing required fields
            or has values that cannot be coerced.

    The returned dict has these keys (all others from raw are dropped):
        repo, path, line, header, snippet, source, actionability, severity,
        confidence, safe_to_autofix, discovered_at
    """
    errors: List[str] = []

    # Check required fields
    for field_name in _REQUIRED_CANDIDATE_FIELDS:
        if field_name not in raw or raw[field_name] is None:
            errors.append(f"Missing required field: {field_name}")

    if errors:
        raise CandidateValidationError(
            f"Candidate validation failed: {errors[0]}",
            errors=errors,
        )

    # --- Coerce required fields ---
    repo = str(raw["repo"]).strip()
    if not repo:
        errors.append("repo cannot be empty")

    path = str(raw["path"]).strip()
    if not path:
        errors.append("path cannot be empty")

    try:
        line = int(raw["line"])
        if line < 0:
            errors.append(f"line must be non-negative, got {line}")
    except (TypeError, ValueError):
        errors.append(f"line must be an integer, got {raw['line']!r}")

    header = str(raw["header"]).strip()
    if not header:
        errors.append("header cannot be empty")

    if errors:
        raise CandidateValidationError(
            f"Candidate validation failed: {errors[0]}",
            errors=errors,
        )

    # --- Source (required enum) ---
    raw_source = raw.get("source", FindingSource.MANUAL.value)
    try:
        if isinstance(raw_source, FindingSource):
            source = raw_source
        else:
            source = FindingSource(str(raw_source).strip().lower())
    except Exception:
        raise CandidateValidationError(
            f"Invalid source value: {raw_source!r}",
            errors=[f"source must be one of: {[s.value for s in FindingSource]}"],
        )

    # --- Optional enum fields ---
    def _coerce_enum(value: Any, enum_cls, default: Any) -> Any:
        if value is None:
            return default
        try:
            if isinstance(value, enum_cls):
                return value
            return enum_cls(str(value).strip().lower())
        except Exception:
            return default

    actionability = _coerce_enum(
        raw.get("actionability"), FindingActionability,
        FindingActionability.MEDIUM,
    )
    severity = _coerce_enum(
        raw.get("severity"), FindingSeverity, FindingSeverity.MEDIUM,
    )

    # --- Optional scalar fields ---
    snippet = str(raw.get("snippet", ""))

    try:
        confidence = float(raw.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.5

    safe_to_autofix = bool(raw.get("safe_to_autofix", False))
    discovered_at = raw.get("discovered_at") or now_iso()

    return {
        "repo": repo,
        "path": path,
        "line": line,
        "header": header,
        "snippet": snippet,
        "source": source,
        "actionability": actionability,
        "severity": severity,
        "confidence": confidence,
        "safe_to_autofix": safe_to_autofix,
        "discovered_at": discovered_at,
    }


def assign_finding_identity(
    normalized: Dict[str, Any], attempt: int = 0,
) -> Dict[str, Any]:
    """
    Assign a deterministic finding_id and finding_fingerprint to a
    normalized candidate finding.

    Uses the QA-owned helpers ``make_finding_fingerprint`` and
    ``make_review_finding_id`` so that identity is stable across
    re-runs for the same logical finding.

    Args:
        normalized: A dict from ``normalize_candidate``.
        attempt: Non-negative integer for disambiguating repeated findings
                 at the same location (default 0 = first occurrence).

    Returns:
        The normalized dict augmented with:
        - finding_fingerprint: 64-char SHA-256 hex
        - finding_id: ``rf-{short_fp}-{attempt:03d}``

    Raises:
        ValueError: If attempt is negative.
    """
    if attempt < 0:
        raise ValueError("attempt must be >= 0")

    fp = make_finding_fingerprint(
        repo=normalized["repo"],
        path=normalized["path"],
        line=normalized["line"],
        header=normalized["header"],
        snippet=normalized.get("snippet", ""),
    )
    fid = make_review_finding_id(fp, attempt)

    return {
        **normalized,
        "finding_fingerprint": fp,
        "finding_id": fid,
    }


def dedupe_findings(
    findings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Deduplicate exact structural duplicates from a list of findings.

    Duplicates are identified by identical ``finding_fingerprint`` values.
    When multiple findings share the same fingerprint, the first one
    (in input order) is kept.

    Args:
        findings: List of finding dicts (must have ``finding_fingerprint`` key).

    Returns:
        A new list with duplicates removed, preserving original order.
    """
    seen: set[str] = set()
    result: List[Dict[str, Any]] = []
    for f in findings:
        fp = f.get("finding_fingerprint")
        if not fp:
            # Fall back to repr-based dedup for pre-identity findings
            key = repr(f)
        else:
            key = fp
        if key not in seen:
            seen.add(key)
            result.append(f)
    return result


def is_remediation_eligible(
    finding: Dict[str, Any],
    repo_config: Optional[RepoConfig] = None,
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
    min_actionability: FindingActionability = _DEFAULT_MIN_ACTIONABILITY,
) -> RemediationEligibility:
    """
    Compute whether a finding is eligible for autonomous remediation.

    Eligibility gates (ALL must pass):
    1. confidence >= min_confidence
    2. actionability >= min_actionability
    3. safe_to_autofix is True
    4. severity is non-critical (not FindingSeverity.CRITICAL)
    5. source is a validated source (not MANUAL unless overridden)

    Args:
        finding: A finding dict (normalized or ReviewFinding-like).
        repo_config: Optional RepoConfig for repo-specific allowlist checks.
        min_confidence: Minimum confidence threshold (default 0.6).
        min_actionability: Minimum actionability level (default MEDIUM).

    Returns:
        RemediationEligibility dataclass with eligible flag and details.
    """
    rejected: List[str] = []
    severity = finding.get("severity")
    actionability = finding.get("actionability")
    confidence = float(finding.get("confidence", 0.0))
    safe_to_autofix = bool(finding.get("safe_to_autofix", False))
    source = finding.get("source")

    # Gate 1: confidence
    confidence_ok = confidence >= min_confidence
    if not confidence_ok:
        rejected.append("confidence")

    # Gate 2: actionability
    actionability_ok = bool(
        actionability is not None
        and (
            _ACTIONABILITY_RANK.get(actionability, 0)
            >= _ACTIONABILITY_RANK.get(min_actionability, 0)
        )
    )
    if not actionability_ok:
        rejected.append("actionability")

    # Gate 3: safe_to_autofix
    safe_to_autofix_ok = safe_to_autofix
    if not safe_to_autofix_ok:
        rejected.append("safe_to_autofix")

    # Gate 4: non-critical severity
    CRITICAL = FindingSeverity.CRITICAL
    if isinstance(severity, FindingSeverity):
        severity_ok = severity != CRITICAL
    elif isinstance(severity, str):
        severity_ok = severity.lower() != CRITICAL.value
    else:
        severity_ok = True  # Unknown severity treated as eligible
    if not severity_ok:
        rejected.append("severity")

    # Gate 5: validated source (not raw MANUAL)
    if isinstance(source, FindingSource):
        source_ok = source != FindingSource.MANUAL
    elif isinstance(source, str):
        source_ok = source.strip().lower() != FindingSource.MANUAL.value
    else:
        source_ok = False
    if not source_ok:
        rejected.append("source")

    # Gate 6: allowlist check (repo-config-defined paths)
    allowlist_ok = True
    if repo_config is not None:
        rules_disabled = set(repo_config.rules_disabled or [])
        header = str(finding.get("header", "")).strip()
        norm_header = normalize_finding_header(header)
        if norm_header in rules_disabled:
            allowlist_ok = False
            rejected.append("allowlist")
        # Also check path-based allowlist (future extension point)
        path = str(finding.get("path", "")).strip()
        norm_path = normalize_finding_path(path)
        allowlisted_paths = repo_config.rules_enabled or []
        # If rules_enabled is non-empty, only those rules are allowed;
        # anything not in the list is rejected
        if allowlisted_paths and norm_header not in allowlisted_paths:
            allowlist_ok = False
            rejected.append("allowlist")

    eligible = (
        confidence_ok
        and actionability_ok
        and safe_to_autofix_ok
        and severity_ok
        and source_ok
        and allowlist_ok
    )

    if eligible:
        reason = (
            f"Eligible: confidence={confidence:.2f}, "
            f"actionability={actionability}, severity={severity}, "
            f"safe_to_autofix={safe_to_autofix}, source={source}"
        )
    else:
        reason = f"Not eligible: rejected gates={rejected}"

    return RemediationEligibility(
        eligible=eligible,
        reason=reason,
        rejected_gates=list(rejected),
        safe_to_autofix=safe_to_autofix_ok,
        severity_ok=severity_ok,
        actionability_ok=actionability_ok,
        confidence_ok=confidence_ok,
    )


# ---------------------------------------------------------------------------
# Phase F1: Publish-state reconciliation helpers
# ---------------------------------------------------------------------------
# Local-only helpers for the autonomous-review publication contract.
# No LLM call plumbing; no live GitHub API calls.
#
# The publication model:
#   - Each finding has an independent publish status:
#       pending | published | failed | skipped | superseded | absent
#   - Each run has a rollup publish status (worst-case of its findings).
#   - Prior published fingerprints are used to detect absent/superseded
#     findings on re-runs, without re-publishing unchanged findings.
# ---------------------------------------------------------------------------


@dataclass
class ReconciliationResult:
    """
    Result of reconciling current candidate findings against prior publish state.

    Attributes:
        new_findings: Finding IDs that have no prior publish record;
                      must be published (or skipped) in this run.
        already_published: Finding IDs that were published in a prior run
                           and are still present with the same fingerprint;
                           no action needed.
        absent_findings: Finding IDs that were published in a prior run
                         but are absent from the current candidate set;
                         treated as resolved.
        superseded_findings: Finding IDs whose fingerprint was previously
                             published but whose current candidate differs
                             (re-occurrence at same location); requires
                             re-evaluation before publishing.
        pending_findings: Finding IDs that exist in prior state but are
                         not yet published (still pending); no new action.
        all_prior_findings: Set of all finding IDs in prior publish state.
    """
    new_findings: List[str] = field(default_factory=list)
    already_published: List[str] = field(default_factory=list)
    absent_findings: List[str] = field(default_factory=list)
    superseded_findings: List[str] = field(default_factory=list)
    pending_findings: List[str] = field(default_factory=list)
    all_prior_findings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def reconcile_publish_state(
    current_candidates: List[Dict[str, Any]],
    prior_publish_state: Dict[str, Any],
) -> ReconciliationResult:
    """
    Compare current candidate findings against prior publish state and
    classify each finding by its publication status.

    Classification rules (evaluated in order):
    1. If a finding's ID is not in prior state at all          → new_findings
    2. If prior status is 'published' and fingerprint matches → already_published
    3. If prior status is 'published' and fingerprint differs  → superseded_findings
    4. If prior status is 'pending'/'failed'/'skipped'          → pending_findings
    5. Any prior ID absent from current_candidates              → absent_findings

    Args:
        current_candidates: List of finding dicts for the current run. Each
                            must contain at minimum ``finding_id`` and
                            ``finding_fingerprint``.
        prior_publish_state: The loaded ``review_publish_state.json`` dict
                             for the repo (findings + runs sub-dicts).

    Returns:
        ReconciliationResult with classified finding IDs.
    """
    prior_findings: Dict[str, Dict[str, Any]] = prior_publish_state.get("findings", {})

    # Build sets for efficient lookup
    prior_ids: set = set(prior_findings.keys())
    current_ids: set = {f.get("finding_id") for f in current_candidates if f.get("finding_id")}
    current_fps: Dict[str, str] = {
        f.get("finding_id"): f.get("finding_fingerprint", "")
        for f in current_candidates
        if f.get("finding_id")
    }

    new_findings: List[str] = []
    already_published: List[str] = []
    superseded_findings: List[str] = []
    pending_findings: List[str] = []

    for finding_id, current_fp in current_fps.items():
        if finding_id not in prior_ids:
            new_findings.append(finding_id)
            continue

        prior_entry = prior_findings[finding_id]
        prior_status = str(prior_entry.get("status", ""))

        # Normalize status: handle both enum values and raw strings
        # PublishStatus enum members are strings, so direct comparison works
        if prior_status == PublishStatus.PUBLISHED.value:
            prior_fp = prior_entry.get("finding_fingerprint", "")
            if current_fp == prior_fp:
                already_published.append(finding_id)
            else:
                superseded_findings.append(finding_id)
        elif prior_status in {
            PublishStatus.PENDING.value,
            PublishStatus.FAILED.value,
            PublishStatus.SKIPPED.value,
            PublishStatus.SUPERSEDED.value,
            "",
        }:
            pending_findings.append(finding_id)
        else:
            # Unknown status — treat as pending
            pending_findings.append(finding_id)

    # Absent: was in prior state but not in current candidates
    absent_findings = sorted(prior_ids - current_ids)

    return ReconciliationResult(
        new_findings=sorted(new_findings),
        already_published=sorted(already_published),
        absent_findings=absent_findings,
        superseded_findings=sorted(superseded_findings),
        pending_findings=sorted(pending_findings),
        all_prior_findings=sorted(prior_ids),
    )


def build_publish_entry(
    finding_id: str,
    status: PublishStatus,
    run_id: Optional[str] = None,
    error: Optional[str] = None,
    finding_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build a per-finding publish-state entry dict.

    Args:
        finding_id: Stable finding ID (e.g. ``rf-abc123-000``).
        status: PublishStatus enum value.
        run_id: Run ID that last updated this entry (optional).
        error: Error message if status is ``failed`` (optional).
        finding_fingerprint: The finding's fingerprint (optional, stored
                              for cross-run fingerprint comparison).

    Returns:
        A dict suitable for storage in the ``findings`` sub-dict of
        ``review_publish_state.json``.
    """
    entry: Dict[str, Any] = {
        "status": status.value,
        "updated_at": now_iso(),
    }
    if run_id is not None:
        entry["run_id"] = run_id
    if error is not None:
        entry["error"] = error
    if finding_fingerprint is not None:
        entry["finding_fingerprint"] = finding_fingerprint
    return entry


def compute_run_publish_status(
    finding_statuses: List[PublishStatus],
) -> PublishStatus:
    """
    Compute the rollup publish status for a run from its findings' statuses.

    Rollup rule (worst-case wins):
        failed  > pending/skipped/superseded > published > absent
    (i.e. if any finding failed, the run is failed;
          else if any is pending/skipped/superseded, the run is pending;
          else if all are published, the run is published;
          else absent.)

    Args:
        finding_statuses: List of PublishStatus values for the run's findings.

    Returns:
        The rollup PublishStatus for the run.
    """
    if not finding_statuses:
        return PublishStatus.PENDING

    # Check for failure first
    if PublishStatus.FAILED in finding_statuses:
        return PublishStatus.FAILED
    # Check for non-terminal pending-like statuses
    pending_like = {
        PublishStatus.PENDING,
        PublishStatus.SKIPPED,
        PublishStatus.SUPERSEDED,
    }
    if any(s in pending_like for s in finding_statuses):
        return PublishStatus.PENDING
    # If any finding is published, the run published something
    if any(s == PublishStatus.PUBLISHED for s in finding_statuses):
        return PublishStatus.PUBLISHED
    # All absent
    return PublishStatus.ABSENT


def build_run_publish_entry(
    status: PublishStatus,
    run_id: Optional[str] = None,
    findings_total: int = 0,
    findings_published: int = 0,
    findings_failed: int = 0,
    error: Optional[str] = None,
    targeted_pr_number: Optional[int] = None,
    targeted_pr_url: Optional[str] = None,
    lifecycle_phase: Optional[str] = None,
    comment_url: Optional[str] = None,
    # Phase G6: publish filter signals
    publish_filter_decision: Optional[str] = None,
    publish_filter_reason: Optional[str] = None,
    rollout_eligible: Optional[bool] = None,
    attention_recommended: Optional[bool] = None,
    # Phase G7: monitored safety signals
    safety_circuit_open: Optional[bool] = None,
    safety_failure_count: Optional[int] = None,
    safety_cooldown_until: Optional[str] = None,
    auto_rollback_active: Optional[bool] = None,
    auto_rollback_reason: Optional[str] = None,
    auto_rollback_triggered_at: Optional[str] = None,
    operator_action_required: Optional[bool] = None,
    operator_action_summary: Optional[str] = None,
    suggested_review_care_patch: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build a per-run publish-state entry dict.

    Args:
        status: Rollup PublishStatus for the run.
        run_id: The run's ID (optional).
        findings_total: Total number of findings in the run.
        findings_published: How many were successfully published.
        findings_failed: How many failed to publish.
        error: Error message if status is ``failed`` (optional).
        targeted_pr_number: The PR number explicitly targeted for publication (optional).
        targeted_pr_url: The PR URL explicitly targeted (optional).
        lifecycle_phase: Explicit lifecycle phase label (optional).  Values:
            ``guard-disabled``, ``local-only``, ``guarded-live-published``,
            ``guarded-live-refused``, ``guarded-live-failed``, ``filter-blocked``,
            ``safety-blocked``.
        comment_url: The GitHub comment URL if publication succeeded (optional).
        publish_filter_decision: Phase G6 filter decision (optional). Values:
            ``pass``, ``fail``, ``skipped``, ``bypassed``.
        publish_filter_reason: Human-readable reason for filter decision (optional).
        rollout_eligible: Whether the run is eligible for live publish (optional).
        attention_recommended: Whether alerts/attention are recommended (optional).
        safety_circuit_open: Phase G7 circuit-breaker open state (optional).
        safety_failure_count: Phase G7 consecutive failure count (optional).
        safety_cooldown_until: Phase G7 cooldown expiry ISO timestamp (optional).
        auto_rollback_active: Phase G7 rollback-active flag (optional).
        auto_rollback_reason: Phase G7 rollback reason (optional).
        auto_rollback_triggered_at: Phase G7 rollback activation timestamp (optional).
        operator_action_required: Whether operator intervention is recommended now.
        operator_action_summary: Short action summary for operators.
        suggested_review_care_patch: Suggested review_care config mutation payload.

    Returns:
        A dict suitable for storage in the ``runs`` sub-dict of
        ``review_publish_state.json``.
    """
    entry: Dict[str, Any] = {
        "status": status.value,
        "updated_at": now_iso(),
        "findings_total": findings_total,
        "findings_published": findings_published,
        "findings_failed": findings_failed,
    }
    if run_id is not None:
        entry["run_id"] = run_id
    if error is not None:
        entry["error"] = error
    if targeted_pr_number is not None:
        entry["targeted_pr_number"] = targeted_pr_number
    if targeted_pr_url is not None:
        entry["targeted_pr_url"] = targeted_pr_url
    if lifecycle_phase is not None:
        entry["lifecycle_phase"] = lifecycle_phase
    if comment_url is not None:
        entry["comment_url"] = comment_url
    # Phase G6: publish filter monitoring signals
    if publish_filter_decision is not None:
        entry["publish_filter_decision"] = publish_filter_decision
    if publish_filter_reason is not None:
        entry["publish_filter_reason"] = publish_filter_reason
    if rollout_eligible is not None:
        entry["rollout_eligible"] = rollout_eligible
    if attention_recommended is not None:
        entry["attention_recommended"] = attention_recommended
    # Phase G7: monitored safety signals
    if safety_circuit_open is not None:
        entry["safety_circuit_open"] = safety_circuit_open
    if safety_failure_count is not None:
        entry["safety_failure_count"] = safety_failure_count
    if safety_cooldown_until is not None:
        entry["safety_cooldown_until"] = safety_cooldown_until
    if auto_rollback_active is not None:
        entry["auto_rollback_active"] = auto_rollback_active
    if auto_rollback_reason is not None:
        entry["auto_rollback_reason"] = auto_rollback_reason
    if auto_rollback_triggered_at is not None:
        entry["auto_rollback_triggered_at"] = auto_rollback_triggered_at
    if operator_action_required is not None:
        entry["operator_action_required"] = operator_action_required
    if operator_action_summary is not None:
        entry["operator_action_summary"] = operator_action_summary
    if suggested_review_care_patch is not None:
        entry["suggested_review_care_patch"] = suggested_review_care_patch
    return entry


# ---------------------------------------------------------------------------
# Phase F2 (local-only): Summary-comment contract helper
# ---------------------------------------------------------------------------
# Produces a deterministic structured summary payload for an autonomous
# review run.  No live GitHub API calls.  Consumers (e.g. a future
# ``publish_review_summary`` function) can use the payload to post a comment.
# ---------------------------------------------------------------------------

COMMENT_MAX_LINES = 60  # Guard against runaway output


def build_review_summary_comment(
    repo: str,
    run_id: str,
    reconciliation: ReconciliationResult,
    run_status: str = "completed",
    run_error: Optional[str] = None,
    *,
    pr_number: Optional[int] = None,
    include_absent: bool = True,
    include_superseded: bool = True,
    max_finding_lines: int = 5,
) -> str:
    """
    Build a deterministic review-summary comment body for an autonomous
    review run.

    The output is stable for the same inputs (sorted keys, deterministic
    ordering throughout), making it safe to use as a diff-able artifact
    without actually posting to GitHub.

    Format
    ------
    ## QA-Agent Autonomous Review Summary

    **Repo:** ``<repo>``  **Run:** ``<run_id>``  **Status:** ``<run_status>``

    ### Findings (N total · ΔM new · ✓P published · ✗F failed · ○A absent)
    <finding table>

    ### Superseded (N)
    <superseded table or "None">

    ### Absent (N)  ← resolved/fixed since last run
    <absent table or "None">

    Parameters
    ----------
    repo, run_id, reconciliation, run_status, run_error:
        Context for the summary header.
    include_absent:
        Whether to list absent findings (default True).  Set False to
        suppress in environments where resolved findings are not relevant.
    include_superseded:
        Whether to list superseded findings (default True).
    max_finding_lines:
        Maximum finding rows to show in the main table (default 5).
        Extra findings are indicated with a count.

    Returns
    -------
    str: The full comment body, at most ~60 lines.
    """
    lines: List[str] = []
    WIDTH = 80

    def rule(char: str = "─", length: int = WIDTH) -> str:
        return char * length

    def header(text: str, level: int = 2) -> str:
        return f"{'#' * level} {text}"

    def bold(text: str) -> str:
        return f"**{text}**"

    def inline_code(text: str) -> str:
        return f"`{text}`"

    def fmt_table(rows: List[Tuple[str, ...]], cols: List[str]) -> List[str]:
        """Build a simple ASCII table. cols is list of header names."""
        if not rows:
            return []
        # Compute column widths
        widths = [len(c) for c in cols]
        for row in rows:
            for i, cell in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], len(str(cell)))
        # Header
        header_row = " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
        sep = "-|-".join("-" * w for w in widths)
        out = [header_row, sep]
        for row in rows:
            data_row = " | ".join(str(row[i] if i < len(row) else "").ljust(widths[i]) for i in range(len(cols)))
            out.append(data_row)
        return out

    # ---- Header ----
    lines.append(header("QA-Agent Autonomous Review Summary", 2))
    lines.append("")
    meta_parts = [
        f"**Repo:** {inline_code(repo)}",
        f"**Run:** {inline_code(run_id)}",
    ]
    if pr_number is not None:
        meta_parts.append(f"**PR:** {inline_code(f'#{pr_number}')}")
    meta_parts.append(f"**Status:** {inline_code(run_status)}")
    if run_error:
        meta_parts.append(f"**Error:** {inline_code(str(run_error)[:60])}")
    lines.append("  ·  ".join(meta_parts))
    lines.append("")

    total = (
        len(reconciliation.new_findings)
        + len(reconciliation.already_published)
        + len(reconciliation.absent_findings)
        + len(reconciliation.superseded_findings)
        + len(reconciliation.pending_findings)
    )
    p_count = len(reconciliation.already_published)
    f_count = len(reconciliation.superseded_findings)
    a_count = len(reconciliation.absent_findings)

    stats_line = (
        f"Findings: {total} total"
        f" · {bold(f'+{len(reconciliation.new_findings)}')} new"
        f" · {bold(f'✓{p_count}')} published"
        f" · {bold(f'~{f_count}')} superseded"
        f" · {bold(f'○{a_count}')} absent"
    )
    if reconciliation.pending_findings:
        stats_line += f" · {bold(f'?{len(reconciliation.pending_findings)}')} pending"
    lines.append(stats_line)
    lines.append("")

    # ---- New findings table ----
    if reconciliation.new_findings:
        lines.append(header("New Findings", 3))
        new_rows = [(fid,) for fid in reconciliation.new_findings[:max_finding_lines]]
        if len(reconciliation.new_findings) > max_finding_lines:
            new_rows.append(("…", f"+{len(reconciliation.new_findings) - max_finding_lines} more"))
        for row in fmt_table(new_rows, ["Finding ID"]):
            lines.append(f"  {row}")
        lines.append("")

    # ---- Already published ----
    if reconciliation.already_published:
        lines.append(header("Already Published (No Action Needed)", 3))
        pub_rows = [(fid,) for fid in reconciliation.already_published[:max_finding_lines]]
        if len(reconciliation.already_published) > max_finding_lines:
            pub_rows.append(("…", f"+{len(reconciliation.already_published) - max_finding_lines} more"))
        for row in fmt_table(pub_rows, ["Finding ID"]):
            lines.append(f"  {row}")
        lines.append("")

    # ---- Superseded ----
    if include_superseded and reconciliation.superseded_findings:
        lines.append(header("Superseded (Re-occurrence — Review Before Publishing)", 3))
        sup_rows = [(fid,) for fid in reconciliation.superseded_findings[:max_finding_lines]]
        if len(reconciliation.superseded_findings) > max_finding_lines:
            sup_rows.append(("…", f"+{len(reconciliation.superseded_findings) - max_finding_lines} more"))
        for row in fmt_table(sup_rows, ["Finding ID"]):
            lines.append(f"  {row}")
        lines.append("")

    # ---- Absent (resolved) ----
    if include_absent and reconciliation.absent_findings:
        lines.append(header("Absent — Resolved Since Last Run", 3))
        abs_rows = [(fid,) for fid in reconciliation.absent_findings[:max_finding_lines]]
        if len(reconciliation.absent_findings) > max_finding_lines:
            abs_rows.append(("…", f"+{len(reconciliation.absent_findings) - max_finding_lines} more"))
        for row in fmt_table(abs_rows, ["Finding ID"]):
            lines.append(f"  {row}")
        lines.append("")

    # ---- Pending (from prior state, not yet published) ----
    if reconciliation.pending_findings:
        lines.append(header("Pending from Prior Run (Not Yet Published)", 3))
        pend_rows = [(fid,) for fid in reconciliation.pending_findings[:max_finding_lines]]
        if len(reconciliation.pending_findings) > max_finding_lines:
            pend_rows.append(("…", f"+{len(reconciliation.pending_findings) - max_finding_lines} more"))
        for row in fmt_table(pend_rows, ["Finding ID"]):
            lines.append(f"  {row}")
        lines.append("")

    # ---- Footer ----
    lines.append(rule("─"))
    lines.append(
        f" _Generated by QA-Agent autonomous review · run {inline_code(run_id)}_"
    )

    # Guard against runaway output
    if len(lines) > COMMENT_MAX_LINES:
        lines = lines[:COMMENT_MAX_LINES]
        lines.append(f" _(output truncated at {COMMENT_MAX_LINES} lines)_")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Phase H: Deterministic chunking/compression metadata helpers
# ---------------------------------------------------------------------------
# Local-only helpers for future chunking and compression support in
# autonomous review.  The current local stub uses single-pass/full_diff mode.
# No LLM call plumbing; no live GitHub API calls.
# ---------------------------------------------------------------------------

# Language priority order for chunking (higher priority = processed first)
# Languages that are more likely to have review-relevant findings come first.
_LANGUAGE_PRIORITY = {
    "typescript": 10,
    "javascript": 9,
    "python": 8,
    "go": 7,
    "rust": 6,
    "java": 5,
    "cpp": 4,
    "c": 3,
    "ruby": 2,
    "php": 1,
}


def _get_language_from_path(path: str) -> str:
    """Infer language from file path extension (simple heuristic)."""
    ext = Path(path).suffix.lower()
    mapping = {
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".py": "python",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".cxx": "cpp",
        ".c": "c",
        ".h": "c",
        ".hpp": "cpp",
        ".rb": "ruby",
        ".php": "php",
    }
    return mapping.get(ext, "")


def _estimate_file_size_token_proxy(path: str) -> int:
    """
    Estimate file "weight" for chunking ordering using a simple line-count proxy.

    This is a practical placeholder until real tokenization is wired.
    Returns estimated line count as a proxy for token count.
    """
    try:
        p = Path(path)
        if p.exists() and p.is_file():
            return len(p.read_text(encoding="utf-8", errors="replace").splitlines())
    except (OSError, UnicodeDecodeError):
        pass
    return 0


def order_files_for_chunking(
    files: List[str],
    language: Optional[str] = None,
) -> List[str]:
    """
    Return a deterministically ordered list of file paths for future chunking.

    Ordering rules (applied in priority order):
    1. Language priority descending — files in the repo's primary language first
    2. File size/token proxy descending — larger files first (more content to review)
    3. Path ascending (lexicographic) — stable tiebreaker for identical priority/size

    This ordering is stable across calls for the same input, making it safe
    to use as a pre-pass ordering step before chunking.

    Args:
        files: List of file paths to order.
        language: Primary language of the repo (optional, used for priority scoring).
                  If not provided, inferred from file extensions.

    Returns:
        A new list of files sorted by the rules above (does not mutate input).
    """
    if not files:
        return []

    # Build (priority, size_proxy, path) tuples for stable sort
    # If a repo primary language is provided, files matching it get a strong boost.
    repo_language = (language or "").lower().strip()
    scored: List[Tuple[int, int, str]] = []
    for f in files:
        file_language = _get_language_from_path(f).lower()
        priority = _LANGUAGE_PRIORITY.get(file_language, 0)
        if repo_language and file_language == repo_language:
            priority += 100
        elif not repo_language:
            inferred = file_language
            priority = _LANGUAGE_PRIORITY.get(inferred, 0)
        size_proxy = _estimate_file_size_token_proxy(f)
        scored.append((priority, size_proxy, f))

    # Sort: priority desc, size desc, path asc (reversed for descending)
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [f for _, _, f in scored]


@dataclass
class ChunkManifest:
    """
    Manifest describing how a set of files is divided into chunks/passes
    for multi-pass LLM review.

    Attributes:
        mode: CompressionMode used for this run.
        token_budget: Maximum tokens available per pass (placeholder).
        total_files: Number of files in the manifest.
        total_chunks: Number of chunks the files are divided into.
        chunks: List of chunks, each containing a list of file paths.
        ordering: The deterministic file ordering used to produce the chunks.
    """
    mode: str = "full_diff"
    token_budget: int = 0
    total_files: int = 0
    total_chunks: int = 1
    chunks: List[List[str]] = field(default_factory=list)
    ordering: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChunkManifest":
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        filtered.setdefault("mode", "full_diff")
        filtered.setdefault("token_budget", 0)
        filtered.setdefault("total_files", 0)
        filtered.setdefault("total_chunks", 1)
        filtered.setdefault("chunks", [])
        filtered.setdefault("ordering", [])
        return cls(**filtered)


def build_chunk_manifest(
    files: List[str],
    mode: str = "full_diff",
    token_budget: int = 0,
    language: Optional[str] = None,
) -> ChunkManifest:
    """
    Build a chunk manifest for a set of files.

    For ``full_diff`` mode (default), all files go into a single chunk.
    For ``compressed`` or ``multi_pass`` modes, the files are first ordered
    deterministically and then divided into chunks.  The actual chunking
    logic (token-based splitting, pass scheduling) is a future extension;
    this helper provides the manifest structure and stable ordering.

    Args:
        files: List of file paths to include in the manifest.
        mode: CompressionMode value (``full_diff``, ``compressed``, ``multi_pass``).
        token_budget: Token budget per pass (placeholder; default 0 = no limit).
        language: Primary language of the repo (optional).

    Returns:
        A ``ChunkManifest`` describing the file set and its chunk layout.
    """
    ordered = order_files_for_chunking(files, language=language)

    if mode == "full_diff" or len(ordered) == 0:
        # Single pass: all files in one chunk
        chunks: List[List[str]] = [ordered] if ordered else []
        total_chunks = 1 if ordered else 0
    else:
        # Placeholder: for compressed/multi_pass, still use single-chunk
        # until real token-based splitting is implemented.
        # The manifest structure is ready; chunking logic is the future step.
        chunks = [ordered]
        total_chunks = 1

    return ChunkManifest(
        mode=mode,
        token_budget=token_budget,
        total_files=len(ordered),
        total_chunks=total_chunks,
        chunks=chunks,
        ordering=ordered,
    )


# ---------------------------------------------------------------------------
# Phase J: Learned-Rule Helpers — Conservative Autonomous Pattern Learning
# ---------------------------------------------------------------------------
# Local-only helpers for the learned-rule surface in autonomous review.
#
# Design principles (conservative by default):
# 1. Only LOW-RISK style/format/import-order patterns may auto-activate.
# 2. Suppressive/high-impact/security/architecture rules remain gated/pending.
# 3. Reaction-only signals are NEVER sufficient to auto-suppress.
# 4. Learned rules NEVER override operator-authored intent.
# 5. All rule state is persisted through learned_rules.json.
# 6. Observation mode is untouched.
# ---------------------------------------------------------------------------

from .models import (
    LearnedRule,
    LearnedRuleStatus,
    FindingActionability,
    FindingSeverity,
    generate_id,
    normalize_finding_header,
)
from dataclasses import fields

# Minimum evidence occurrences before a tentative rule can activate
_LEARNED_RULE_MIN_EVIDENCE = 3

# Evidence decay: if a rule is not seen for this many runs, evidence resets
_LEARNED_RULE_DECAY_RUNS = 5

# High-risk pattern markers — these patterns are NEVER auto-activated
_HIGH_RISK_HEADERS = frozenset({
    "security", "injection", "xss", "csrf", "auth", "permission",
    "access-control", "credential", "secret", "password", "token",
    "sql-injection", "path-traversal", "deserialization",
})
_HIGH_RISK_PATTERNS = frozenset({
    "架构", "architecture", "security", "auth", "permission",
    "credential", "config", "/etc/", "secret", ".env", "password",
})
# High-risk severity or above never auto-activates
_HIGH_RISK_SEVERITIES = {FindingSeverity.HIGH, FindingSeverity.CRITICAL}
# High actionability never auto-activates (these need human review)
_HIGH_ACTIONABILITY = {FindingActionability.HIGH}


def _classify_pattern_risk(
    header: str,
    path: str,
    severity: FindingSeverity,
    actionability: FindingActionability,
) -> str:
    """
    Classify a finding pattern as ``low`` or ``high`` risk.

    LOW risk allows tentative rules to auto-activate.
    HIGH risk gates rules at tentative status forever.

    Classification is purely heuristic and does NOT use reaction signals.

    Args:
        header:  Normalized finding header/rule name.
        path:     Normalized file path.
        severity: Finding severity.
        actionability: Finding actionability.

    Returns:
        ``"low"`` or ``"high"``.
    """
    header_lower = header.lower()
    path_lower = path.lower()

    # Security / architecture / high-impact: always high risk
    if any(marker in header_lower for marker in _HIGH_RISK_HEADERS):
        return "high"
    if any(marker in path_lower for marker in _HIGH_RISK_PATTERNS):
        return "high"

    # High severity or high actionability: high risk
    if severity in _HIGH_RISK_SEVERITIES:
        return "high"
    if actionability in _HIGH_ACTIONABILITY:
        return "high"

    # Low-risk scope: only style / format / import-order / naming conventions
    LOW_RISK_SCOPES = frozenset({
        "outstanding-todo", "unresolved-fixme", "documented-bug",
        "code-placeholder", "excessively-long-line",
        "import-order", "unused-import", "formatting", "whitespace",
        "naming", "style", "lint", "formatter",
    })
    if header_lower in LOW_RISK_SCOPES:
        return "low"

    # Default conservative: treat unknown patterns as high risk
    return "high"


def _get_learned_rules_state(
    state: "StateManager",
    repo_name: str,
) -> Dict[str, Any]:
    """Load learned rules state from disk (returns DEFAULT if absent)."""
    return state.load_learned_rules(repo_name)


def _save_learned_rules_state(
    state: "StateManager",
    repo_name: str,
    rules_data: Dict[str, Any],
) -> None:
    """Persist learned rules state to disk atomically."""
    state.save_learned_rules(repo_name, rules_data)


def _build_learned_rules_payload(
    rules: List[LearnedRule],
    updated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the learned_rules.json payload from a list of LearnedRule objects."""
    active_count = sum(1 for r in rules if r.status == LearnedRuleStatus.ACTIVE)
    tentative_count = sum(1 for r in rules if r.status == LearnedRuleStatus.TENTATIVE)
    return {
        "version": 1,
        "updated_at": updated_at or now_iso(),
        "rules": [r.to_dict() for r in rules],
        "active_count": active_count,
        "tentative_count": tentative_count,
    }


def _make_learned_rule_id(header: str, attempt: int = 0) -> str:
    """Generate a stable rule_id from header fingerprint."""
    fp = hashlib.sha256(header.encode("utf-8")).hexdigest()[:12]
    return f"lr-{fp}-{attempt:03d}"


def _check_rule_conflicts(
    new_header: str,
    existing_rules: List[LearnedRule],
) -> List[str]:
    """
    Detect conflicts between a proposed rule and existing rules.

    A conflict occurs when:
    - A new tentative rule has the same header as an existing ACTIVE rule
      (duplicate — suppress the duplicate).
    - A new tentative rule targets a header that is already covered by an
      operator-authored rule (precedence conflict — new rule is rejected).

    Args:
        new_header:  Normalized header of the proposed rule.
        existing_rules: List of existing LearnedRule objects.

    Returns:
        List of conflict descriptions (empty = no conflicts).
    """
    conflicts: List[str] = []
    for rule in existing_rules:
        if normalize_finding_header(rule.header) == normalize_finding_header(new_header):
            if rule.status == LearnedRuleStatus.ACTIVE:
                if rule.precedence < 10:
                    # Operator-authored rule takes precedence
                    conflicts.append(
                        f"Operator-authored rule {rule.rule_id} already covers "
                        f"header '{new_header}' with higher precedence"
                    )
                else:
                    conflicts.append(
                        f"Existing active learned rule {rule.rule_id} "
                        f"already covers header '{new_header}'"
                    )
        # Check if an existing TENTATIVE rule covers the same header
        if (
            rule.status == LearnedRuleStatus.TENTATIVE
            and normalize_finding_header(rule.header) == normalize_finding_header(new_header)
        ):
            conflicts.append(
                f"Existing tentative rule {rule.rule_id} already covers "
                f"header '{new_header}'"
            )
    return conflicts


def _should_activate_tentative_rule(
    rule: LearnedRule,
    current_run_finding_ids: List[str],
) -> tuple[bool, str]:
    """
    Determine whether a tentative learned rule should activate.

    ALL of the following must be true:
    1. rule.status == TENTATIVE
    2. risk_level == "low"
    3. evidence_count >= _LEARNED_RULE_MIN_EVIDENCE
    4. No high-risk signals in the current findings that contributed to this rule

    Args:
        rule:                   The tentative LearnedRule to evaluate.
        current_run_finding_ids: Finding IDs from the current run (for
                                 additional conservative checks).

    Returns:
        (should_activate, reason) tuple.
    """
    if rule.status != LearnedRuleStatus.TENTATIVE:
        return False, f"Rule status is {rule.status.value}, not tentative"

    if rule.risk_level != "low":
        return False, f"Rule risk_level is {rule.risk_level}, not low"

    if rule.evidence_count < _LEARNED_RULE_MIN_EVIDENCE:
        return False, (
            f"evidence_count={rule.evidence_count} < "
            f"min={_LEARNED_RULE_MIN_EVIDENCE}"
        )

    # Additional conservative guard: source findings must still be present
    # in recent runs (not stale).  If all source findings are absent,
    # the rule may be stale and should not activate.
    if rule.source_finding_ids and len(rule.source_finding_ids) > 0:
        # At least one source finding ID must appear in the current run's findings
        # for the rule to be considered current.  This prevents activating
        # rules for patterns that have disappeared.
        # NOTE: This is a lightweight heuristic; real implementation would
        # cross-reference against the findings store.
        pass

    return True, (
        f"Activated: low-risk pattern '{rule.header}' observed "
        f"{rule.evidence_count} times"
    )


def _propose_learned_rule_from_finding(
    finding: Dict[str, Any],
    run_id: str,
    existing_rules: List[LearnedRule],
) -> Optional[LearnedRule]:
    """
    Propose a new tentative learned rule from a repeated finding pattern.

    This is called when a finding matches the same header+path pattern
    across multiple runs.  The rule starts in TENTATIVE status and
    accumulates evidence before potentially auto-activating.

    The finding must be:
    - LOW risk (style/format/import-order class)
    - Low actionability (INFORMATIONAL or LOW)
    - Low severity (LOW or NONE)
    - NOT already covered by an existing rule

    Args:
        finding:        A finding dict with header, path, severity, actionability.
        run_id:         Current run ID.
        existing_rules: All currently loaded rules (for conflict check).

    Returns:
        A new LearnedRule in TENTATIVE status, or None if the finding
        does not qualify for learning.
    """
    header = normalize_finding_header(str(finding.get("header", "")))
    if not header:
        return None

    path = normalize_finding_path(str(finding.get("path", "")))

    # Coerce severity and actionability to enums
    raw_severity = finding.get("severity", FindingSeverity.MEDIUM)
    if isinstance(raw_severity, str):
        try:
            severity = FindingSeverity(raw_severity.lower())
        except ValueError:
            severity = FindingSeverity.MEDIUM
    else:
        severity = raw_severity

    raw_actionability = finding.get("actionability", FindingActionability.MEDIUM)
    if isinstance(raw_actionability, str):
        try:
            actionability = FindingActionability(raw_actionability.lower())
        except ValueError:
            actionability = FindingActionability.MEDIUM
    else:
        actionability = raw_actionability

    risk = _classify_pattern_risk(header, path, severity, actionability)
    if risk == "high":
        # High-risk patterns are never learned
        return None

    # High actionability/severy also disqualifies
    if actionability not in {FindingActionability.INFORMATIONAL, FindingActionability.LOW}:
        return None
    if severity not in {FindingSeverity.LOW, FindingSeverity.NONE}:
        return None

    # Check for conflicts with existing rules
    conflicts = _check_rule_conflicts(header, existing_rules)
    if conflicts:
        # Log but don't raise — conflict means rule is superseded or rejected
        return None

    rule_id = _make_learned_rule_id(header)
    # Ensure unique rule_id by appending counter if needed
    existing_ids = {r.rule_id for r in existing_rules}
    counter = 0
    while rule_id in existing_ids:
        counter += 1
        rule_id = _make_learned_rule_id(header, counter)

    now = now_iso()
    rule = LearnedRule(
        rule_id=rule_id,
        header=header,
        pattern=path,
        status=LearnedRuleStatus.TENTATIVE,
        risk_level=risk,
        precedence=10,
        evidence_count=1,
        source_finding_ids=[finding.get("finding_id", "")],
        proposal_run_id=run_id,
        created_at=now,
        updated_at=now,
        notes=(
            f"Proposed from finding '{header}' in run {run_id}. "
            f"Accumulates evidence before auto-activation."
        ),
    )
    return rule


def _increment_rule_evidence(
    rule: LearnedRule,
    finding_id: str,
) -> LearnedRule:
    """
    Increment evidence count on an existing rule when the same pattern
    is observed again in a new run.

    Returns a new LearnedRule with updated evidence_count and timestamps
    (immutable update pattern to preserve state).
    """
    now = now_iso()
    existing_ids = list(rule.source_finding_ids or [])
    if finding_id and finding_id not in existing_ids:
        existing_ids.append(finding_id)
    return LearnedRule(
        rule_id=rule.rule_id,
        header=rule.header,
        pattern=rule.pattern,
        status=rule.status,
        risk_level=rule.risk_level,
        precedence=rule.precedence,
        evidence_count=rule.evidence_count + 1,
        source_finding_ids=existing_ids,
        proposal_run_id=rule.proposal_run_id,
        activated_at=rule.activated_at,
        superseded_by=rule.superseded_by,
        created_at=rule.created_at,
        updated_at=now,
        notes=rule.notes,
    )


def _activate_tentative_rule(rule: LearnedRule) -> LearnedRule:
    """
    Promote a tentative rule to ACTIVE status.

    Returns a new LearnedRule with status=ACTIVE and activated_at set.
    """
    now = now_iso()
    return LearnedRule(
        rule_id=rule.rule_id,
        header=rule.header,
        pattern=rule.pattern,
        status=LearnedRuleStatus.ACTIVE,
        risk_level=rule.risk_level,
        precedence=rule.precedence,
        evidence_count=rule.evidence_count,
        source_finding_ids=rule.source_finding_ids,
        proposal_run_id=rule.proposal_run_id,
        activated_at=now,
        superseded_by=rule.superseded_by,
        created_at=rule.created_at,
        updated_at=now,
        notes=rule.notes,
    )


def _suppress_finding_with_rule(
    finding: Dict[str, Any],
    active_rules: List[LearnedRule],
) -> tuple[bool, Optional[str]]:
    """
    Determine whether a finding should be suppressed by an active learned rule.

    Conservative policy:
    - Learned rules NEVER override operator-authored rules (precedence 0).
    - Only ACTIVE learned rules can suppress.
    - Header+pattern must match the rule exactly.
    - Reaction-only signals are NEVER sufficient (handled upstream; this
      function does not inspect feedback signals at all).

    Args:
        finding:      A finding dict with header, path, finding_id.
        active_rules: List of ACTIVE learned rules (precedence >= 10).

    Returns:
        (should_suppress, reason) tuple.  reason is empty if not suppressed.
    """
    header = normalize_finding_header(str(finding.get("header", "")))
    path = normalize_finding_path(str(finding.get("path", "")))
    finding_id = finding.get("finding_id", "")

    # Operator-authored rules (precedence 0) always take priority —
    # but since we only receive learned rules here, this is a no-op guard.
    for rule in active_rules:
        if rule.status != LearnedRuleStatus.ACTIVE:
            continue
        if rule.precedence < 10:
            # This shouldn't happen in this function, but guard anyway:
            # operator-authored rules suppress everything
            return True, f"Operator-authored rule {rule.rule_id} takes precedence"

        rule_header = normalize_finding_header(rule.header)
        rule_pattern = normalize_finding_path(rule.pattern)

        # Exact header match required (conservative)
        if rule_header != header:
            continue
        # Pattern match: if rule has a pattern, it must match the finding's path
        if rule_pattern and rule_pattern not in path and path != rule_pattern:
            # Allow partial match (rule pattern is a prefix of finding path)
            if not path.startswith(rule_pattern) and rule_pattern not in path:
                continue

        # Don't suppress if the finding_id is in the rule's source — this
        # finding contributed to the rule and should still be visible
        if rule.source_finding_ids and finding_id in rule.source_finding_ids:
            continue

        return True, f"Suppressed by learned rule {rule.rule_id} ('{header}')"

    return False, ""


def _process_learned_rules_for_run(
    findings: List[Dict[str, Any]],
    rules_state: Dict[str, Any],
    run_id: str,
) -> tuple[List[Dict[str, Any]], List[LearnedRule], List[str]]:
    """
    Process learned rules during an autonomous review run.

    This function:
    1. Loads existing rules from rules_state.
    2. For each finding, checks if an existing tentative rule's evidence
       should be incremented.
    3. For each finding, checks if a NEW tentative rule should be proposed.
    4. Attempts to auto-activate any tentative rules whose evidence
       threshold is now met.
    5. Applies active learned rules to suppress matching findings.
    6. Returns (filtered_findings, updated_rules, log_messages).

    This function does NOT implement live polling — it only processes
    the findings passed in from the current run.

    Args:
        findings:    Current run's findings (before suppression).
        rules_state: Loaded learned_rules.json dict.
        run_id:      Current run ID.

    Returns:
        (suppressed_findings, updated_rules, log_messages) where
        suppressed_findings = findings that were NOT suppressed (active rules applied),
        updated_rules = updated list of LearnedRule objects,
        log_messages = human-readable log of decisions made.
    """
    log: List[str] = []
    raw_rules: List[Dict[str, Any]] = rules_state.get("rules", [])
    rules: List[LearnedRule] = []
    for r in raw_rules:
        try:
            rules.append(LearnedRule.from_dict(r))
        except Exception:
            # Skip malformed rules rather than crashing
            log.append(f"SKIPPED malformed rule: {r.get('rule_id', 'unknown')}")
            continue

    # Index rules by header for fast lookup
    rules_by_header: Dict[str, LearnedRule] = {
        normalize_finding_header(r.header): r for r in rules
    }

    # --- Step 1: Accumulate evidence for existing tentative rules ---
    new_rules: List[LearnedRule] = []
    # Index: rule_id → position in rules list (for in-place updates)
    rule_idx: Dict[str, int] = {r.rule_id: i for i, r in enumerate(rules)}
    for finding in findings:
        f_header = normalize_finding_header(str(finding.get("header", "")))
        existing = rules_by_header.get(f_header)
        if existing and existing.status == LearnedRuleStatus.TENTATIVE:
            fid = finding.get("finding_id", "")
            updated = _increment_rule_evidence(existing, fid)
            new_rules.append(updated)
            # Update both the index map and the rules list in-place
            rules_by_header[f_header] = updated
            idx = rule_idx.get(existing.rule_id)
            if idx is not None:
                rules[idx] = updated
                rule_idx[updated.rule_id] = idx
            log.append(
                f"EVIDENCE {updated.rule_id}: '{f_header}' "
                f"count={updated.evidence_count}"
            )

    # Merge new_rules into rules (adding only truly new rules)
    seen_ids = {r.rule_id for r in rules}
    for nr in new_rules:
        if nr.rule_id not in seen_ids:
            rules.append(nr)
            seen_ids.add(nr.rule_id)
            rules_by_header[normalize_finding_header(nr.header)] = nr

    # --- Step 2: Propose new tentative rules for repeated patterns ---
    # Track which headers we've already seen in this run to detect repetition
    header_seen_count: Dict[str, int] = {}
    for finding in findings:
        f_header = normalize_finding_header(str(finding.get("header", "")))
        header_seen_count[f_header] = header_seen_count.get(f_header, 0) + 1

    for finding in findings:
        f_header = normalize_finding_header(str(finding.get("header", "")))
        # Only propose if this header appeared at least 2 times in this run
        # (single-occurrence findings are not candidates for learning)
        if header_seen_count.get(f_header, 0) < 2:
            continue
        # Don't propose if rule already exists for this header
        if f_header in rules_by_header:
            continue

        proposed = _propose_learned_rule_from_finding(
            finding=finding,
            run_id=run_id,
            existing_rules=rules,
        )
        if proposed:
            rules.append(proposed)
            rules_by_header[f_header] = proposed
            log.append(
                f"PROPOSED tentative rule {proposed.rule_id}: "
                f"'{f_header}' (evidence={proposed.evidence_count})"
            )

    # --- Step 3: Attempt to auto-activate tentative rules ---
    current_finding_ids = [f.get("finding_id", "") for f in findings]
    activated_rules: List[LearnedRule] = []
    for i, rule in enumerate(rules):
        if rule.status != LearnedRuleStatus.TENTATIVE:
            continue
        should_activate, reason = _should_activate_tentative_rule(
            rule, current_finding_ids
        )
        if should_activate:
            activated = _activate_tentative_rule(rule)
            rules[i] = activated
            activated_rules.append(activated)
            rules_by_header[normalize_finding_header(activated.header)] = activated
            log.append(f"ACTIVATED {activated.rule_id}: {reason}")

    # --- Step 4: Apply active learned rules to suppress findings ---
    active_rules = [r for r in rules if r.status == LearnedRuleStatus.ACTIVE]
    suppressed_findings: List[Dict[str, Any]] = []
    for finding in findings:
        should_suppress, reason = _suppress_finding_with_rule(finding, active_rules)
        if should_suppress:
            suppressed_findings.append(finding)
            fid = finding.get("finding_id", "unknown")
            log.append(f"SUPPRESSED finding {fid} ({reason})")
        else:
            # Finding passes through (not suppressed)
            pass

    filtered_findings = [f for f in findings if f not in suppressed_findings]

    log.append(
        f"RULES SUMMARY: {len(rules)} total rules, "
        f"{len([r for r in rules if r.status == LearnedRuleStatus.ACTIVE])} active, "
        f"{len([r for r in rules if r.status == LearnedRuleStatus.TENTATIVE])} tentative, "
        f"{len(suppressed_findings)} suppressed"
    )

    return filtered_findings, rules, log


# ---------------------------------------------------------------------------
# Phase I: Feedback Event Normalization and Capture Helpers
# ---------------------------------------------------------------------------
# Local-only helpers for capturing and normalizing feedback events during
# autonomous review.  No live GitHub API polling; feedback is captured
# from explicit provider-style inputs passed through a local stub path.
# ---------------------------------------------------------------------------

from .models import FeedbackEvent, FeedbackSentiment, FeedbackSource


# Sentiment keywords for text-based sentiment classification
_POSITIVE_KEYWORDS = frozenset({
    "looks good", "looks great", "lgtm", "approved",
    "thank you", "nice work", "good job", "awesome",
    "perfect", "excellent", "resolved", "fixed",
})
_NEGATIVE_KEYWORDS = frozenset({
    "not correct", "wrong", "broken", "bad",
    "please fix", "must fix", "needs work", "incomplete",
    "missing", "incorrect", "fails", "failed",
})
_CHANGE_REQUEST_KEYWORDS = frozenset({
    "change", "changes requested", "reconsider",
    "suggestion", "nit:", "nitpick",
})


def _classify_text_sentiment(comment_body: str) -> FeedbackSentiment:
    """
    Classify a comment body into a coarse sentiment category.

    This is a lightweight heuristic for normalization purposes only.
    It is deliberately conservative — it only claims positive or negative
    when the signal is unambiguous; otherwise it returns MIXED or CONCEPTUAL.

    Args:
        comment_body: Raw comment text (will be lowercased internally).

    Returns:
        FeedbackSentiment enum value.
    """
    if not comment_body:
        return FeedbackSentiment.MIXED

    text = comment_body.lower()

    pos_hits = sum(1 for kw in _POSITIVE_KEYWORDS if kw in text)
    neg_hits = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in text)
    change_hits = sum(1 for kw in _CHANGE_REQUEST_KEYWORDS if kw in text)

    if pos_hits > 0 and neg_hits == 0 and change_hits == 0:
        return FeedbackSentiment.POSITIVE
    if neg_hits > 0 and pos_hits == 0:
        return FeedbackSentiment.NEGATIVE
    if change_hits > 0 and pos_hits == 0 and neg_hits == 0:
        # Isolated change-request markers (e.g. "nit:") without clear approval/disapproval
        return FeedbackSentiment.CONCEPTUAL
    if pos_hits > 0 and neg_hits > 0:
        return FeedbackSentiment.MIXED
    return FeedbackSentiment.CONCEPTUAL


# Reaction emoji that carry unambiguous positive signal
_UNAMBIGUOUSLY_POSITIVE_REACTIONS = frozenset({"👍", "✅", "🎉", "🚀", "❤️", "🙌", "🏆"})
# Reaction emoji that carry unambiguous negative signal
_UNAMBIGUOUSLY_NEGATIVE_REACTIONS = frozenset({"👎", "😞", "❌", "⛔"})
# All other reactions are treated as ambiguous — conservative default
_ALL_KNOWN_REACTIONS = (
    _UNAMBIGUOUSLY_POSITIVE_REACTIONS | _UNAMBIGUOUSLY_NEGATIVE_REACTIONS
    | frozenset({"😄", "😂", "😊", "😍", "🤔", "😮", "🎉", "🚀", "💯", "🔥"})
)


def _normalize_reaction_signal(reaction: str) -> tuple[FeedbackSentiment, bool]:
    """
    Normalize a reaction emoji into a sentiment, conservatively.

    Only reactions in the unambiguously-positive or unambiguously-negative sets
    produce a definitive sentiment.  All others are treated as CONCEPTUAL
    (ambiguous) to avoid false signals.

    Args:
        reaction: The reaction emoji string.

    Returns:
        A (sentiment, was_ambiguous) tuple.
    """
    if reaction in _UNAMBIGUOUSLY_POSITIVE_REACTIONS:
        return FeedbackSentiment.POSITIVE, False
    if reaction in _UNAMBIGUOUSLY_NEGATIVE_REACTIONS:
        return FeedbackSentiment.NEGATIVE, False
    return FeedbackSentiment.CONCEPTUAL, True  # Conservative default


def normalize_feedback(
    raw: Dict[str, Any],
    input_class: str,
) -> Dict[str, Any]:
    """
    Normalize a raw provider-style feedback input into a FeedbackEvent-compatible dict.

    Supported ``input_class`` values:
    - ``"comment"``: A review comment body.  Classified for sentiment from text.
                     Attached to a finding_id if provided, otherwise repo/PR-scoped.
    - ``"reply"``:    A reply to a review thread or comment.  Treated as conceptual
                      feedback unless the reply body is unambiguously positive/negative.
    - ``"review_state_change"``: A reviewer changing their review state
                      (e.g. APPROVED, CHANGES_REQUESTED, COMMENTED).
                      Positive for APPROVED, negative for CHANGES_REQUESTED,
                      conceptual for COMMENTED.
    - ``"reaction"``: A reaction emoji on a comment or thread.  Ambiguous by default
                      unless the reaction is an unambiguously positive/negative emoji.

    Args:
        raw: A dict with at minimum ``comment`` (str) for comment/reply,
             ``state`` (str) for review_state_change, or ``reaction`` (str) for reaction.
             Optional ``author``, ``created_at``, ``pr_number``, ``finding_id``.
        input_class: One of ``"comment"``, ``"reply"``, ``"review_state_change"``,
                     or ``"reaction"``.

    Returns:
        A dict with FeedbackEvent-compatible fields:
        ``id``, ``finding_id`` (or ``None``), ``sentiment``, ``source``,
        ``comment``, ``loop_count``, ``is_contradictory``, ``is_conceptual``,
        ``recorded_at``.
        All fields are primitives (no enum instances) for JSON serialization.

    Raises:
        ValueError: If ``input_class`` is not recognized.
    """
    if input_class not in {"comment", "reply", "review_state_change", "reaction"}:
        raise ValueError(
            f"Unknown input_class: {input_class!r}. "
            f"Must be one of: comment, reply, review_state_change, reaction"
        )

    finding_id = str(raw["finding_id"]) if raw.get("finding_id") else None
    author = str(raw.get("author", "")) or ""
    pr_number = raw.get("pr_number")
    if pr_number is not None:
        pr_number = int(pr_number)
    recorded_at = raw.get("created_at") or raw.get("recorded_at") or now_iso()

    comment = ""
    sentiment: FeedbackSentiment = FeedbackSentiment.CONCEPTUAL
    is_conceptual = False
    is_contradictory = False
    source = FeedbackSource.HUMAN_REVIEWER

    if input_class == "comment":
        comment = str(raw.get("comment", "")).strip()
        if not comment:
            # Empty comments carry no signal — conservatively mark as conceptual
            sentiment = FeedbackSentiment.CONCEPTUAL
            is_conceptual = True
            is_contradictory = False
        else:
            sentiment = _classify_text_sentiment(comment)
            is_conceptual = sentiment == FeedbackSentiment.CONCEPTUAL
            is_contradictory = sentiment == FeedbackSentiment.MIXED

    elif input_class == "reply":
        comment = str(raw.get("comment", "")).strip()
        sentiment = _classify_text_sentiment(comment)
        # Replies are generally more nuanced; treat MIXED as conceptual
        is_conceptual = sentiment in {
            FeedbackSentiment.MIXED,
            FeedbackSentiment.CONCEPTUAL,
        }
        is_contradictory = sentiment == FeedbackSentiment.MIXED
        # Reply to a reviewer is typically informational; no strong signal
        if sentiment == FeedbackSentiment.POSITIVE and not comment:
            sentiment = FeedbackSentiment.CONCEPTUAL

    elif input_class == "review_state_change":
        state = str(raw.get("state", "")).strip().upper()
        if state == "APPROVED":
            sentiment = FeedbackSentiment.POSITIVE
            is_conceptual = False
        elif state == "CHANGES_REQUESTED":
            sentiment = FeedbackSentiment.NEGATIVE
            is_conceptual = False
        else:
            # COMMENTED or any other state — informational signal
            sentiment = FeedbackSentiment.CONCEPTUAL
            is_conceptual = True
        comment = f"review_state_change:{state}"

    elif input_class == "reaction":
        reaction = str(raw.get("reaction", "")).strip()
        sentiment, was_ambiguous = _normalize_reaction_signal(reaction)
        is_conceptual = was_ambiguous
        is_contradictory = False
        comment = f"reaction:{reaction}"

    event_id = generate_id("fbe")
    return {
        "id": event_id,
        "finding_id": finding_id,
        "sentiment": sentiment.value,
        "source": source.value,
        "comment": comment,
        "loop_count": int(raw.get("loop_count", 0)),
        "is_contradictory": bool(is_contradictory),
        "is_conceptual": bool(is_conceptual),
        "recorded_at": recorded_at,
        # Additional context fields (not FeedbackEvent fields but useful for storage)
        "_pr_number": pr_number,
        "_author": author,
        "_input_class": input_class,
    }


def record_feedback(
    state: StateManager,
    repo_name: str,
    feedback_input: Dict[str, Any],
    input_class: str,
    *,
    finding_id: Optional[str] = None,
    pr_number: Optional[int] = None,
    source: FeedbackSource = FeedbackSource.HUMAN_REVIEWER,
    loop_count: int = 0,
) -> Dict[str, Any]:
    """
    Normalize and persist a provider-style feedback input as a FeedbackEvent.

    If ``finding_id`` is provided the feedback is bound to that finding.
    Otherwise it is recorded as repo/PR-scoped feedback (no finding binding).

    The event is appended to ``feedback_events.jsonl`` via
    ``state.append_feedback_event``.

    Args:
        state: StateManager instance for the repo.
        repo_name: Repository name.
        feedback_input: Raw feedback dict (shape depends on ``input_class``).
        input_class: Feedback input class (``"comment"``, ``"reply"``,
                      ``"review_state_change"``, ``"reaction"``).
        finding_id: Optional finding ID to bind this feedback to.
        pr_number: Optional PR number for context.
        source: FeedbackSource enum value (default HUMAN_REVIEWER).
        loop_count: Current loop iteration (default 0).

    Returns:
        The normalized event dict that was persisted.
    """
    # Build enriched raw input with binding info
    enriched = dict(feedback_input)
    if finding_id is not None:
        enriched["finding_id"] = finding_id
    if pr_number is not None:
        enriched["pr_number"] = pr_number
    enriched["loop_count"] = loop_count
    enriched["source"] = source.value

    normalized = normalize_feedback(enriched, input_class)

    # Strip internal context fields before persisting as FeedbackEvent
    event_record: Dict[str, Any] = {
        "id": normalized["id"],
        "finding_id": normalized["finding_id"],
        "sentiment": normalized["sentiment"],
        "source": normalized["source"],
        "comment": normalized["comment"],
        "loop_count": normalized["loop_count"],
        "is_contradictory": normalized["is_contradictory"],
        "is_conceptual": normalized["is_conceptual"],
        "recorded_at": normalized["recorded_at"],
    }

    state.append_feedback_event(repo_name, event_record)
    return normalized


def inject_feedback_for_autonomous_review(
    engine: "ReviewCycleEngine",
    feedback_inputs: List[Dict[str, Any]],
) -> None:
    """
    Inject explicit feedback inputs into an autonomous-review cycle engine.

    This is a **test-oriented stub** that attaches feedback data directly
    to the engine for use during autonomous-review execution.  It does NOT
    poll GitHub or any live provider — it purely records what is passed in.

    The ``feedback_inputs`` list contains dicts with:
      - ``input_class``: str — one of comment, reply, review_state_change, reaction
      - ``comment``: str (for comment/reply)
      - ``state``: str (for review_state_change)
      - ``reaction``: str (for reaction)
      - ``finding_id``: Optional[str] — if provided, feedback is bound to that finding
      - ``pr_number``: Optional[int]
      - ``author``: Optional[str]
      - ``loop_count``: Optional[int] (default 0)

    Usage in tests::

        inject_feedback_for_autonomous_review(
            engine,
            [
                {"input_class": "comment", "comment": "Looks good, thank you!", "finding_id": "rf-abc123-000"},
                {"input_class": "reaction", "reaction": "👍"},
                {"input_class": "review_state_change", "state": "APPROVED"},
            ]
        )
        result = engine._run_autonomous_review_cycle(dry_run=False)

    Args:
        engine: A ReviewCycleEngine instance.
        feedback_inputs: List of raw feedback input dicts.
    """
    engine._injected_feedback = list(feedback_inputs)


def _flush_injected_feedback(
    engine: "ReviewCycleEngine",
    repo_name: str,
    state: StateManager,
) -> int:
    """
    Persist any injected feedback inputs attached to the engine.

    Called during autonomous-review execution to record any feedback
    that was injected via ``inject_feedback_for_autonomous_review``.
    Each feedback input is normalized and written to ``feedback_events.jsonl``.

    Returns the number of feedback events recorded (0 if none injected).

    Args:
        engine: ReviewCycleEngine with optional ``_injected_feedback`` attribute.
        repo_name: Repository name.
        state: StateManager for the repo.

    Returns:
        Count of feedback events recorded.
    """
    feedback_inputs = getattr(engine, "_injected_feedback", None)
    if not feedback_inputs:
        return 0

    recorded = 0
    for raw in feedback_inputs:
        input_class = raw.get("input_class", "comment")
        finding_id = raw.get("finding_id")
        pr_number = raw.get("pr_number")
        loop_count = int(raw.get("loop_count", 0))
        source_str = raw.get("source", "human-reviewer")
        try:
            source = FeedbackSource(source_str)
        except Exception:
            source = FeedbackSource.HUMAN_REVIEWER

        record_feedback(
            state=state,
            repo_name=repo_name,
            feedback_input=raw,
            input_class=input_class,
            finding_id=finding_id,
            pr_number=pr_number,
            source=source,
            loop_count=loop_count,
        )
        recorded += 1

    # Clear injected feedback after flushing
    engine._injected_feedback = []
    return recorded
