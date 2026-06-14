#!/usr/bin/env python3
"""GitHub review provider — extracted from review.cycle for single-responsibility."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bluei.app.models import Repo, now_iso
from bluei.app.state import StateManager

_logger = logging.getLogger(__name__)

# Default --limit for `gh pr list` when enumerating managed PRs. Overridable
# per-repo via review_care.managed_pr_limit. Large repos with many managed
# PRs would otherwise be capped at 50 and lose visibility.
_DEFAULT_MANAGED_PR_LIMIT = 200

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


class GitHubReviewProvider:
    """Observation-only GitHub review provider for managed PRs."""

    # Default markers for bot-generated and automated-review comment filtering
    _DEFAULT_IGNORED_COMMENT_MARKERS: List[str] = [
        "<!-- bluei-review-cycle:",
        "automated verification passed for finding",
        "is reviewing your pr",
        "finished reviewing your pr",
        "is running incremental review",
        "thanks for using codeant",
        "**tip:**",
    ]

    def __init__(
        self,
        repo: Repo,
        state: StateManager,
        ignored_comment_markers: Optional[List[str]] = None,
        graphql_reviews_limit: int = 100,
        graphql_threads_limit: int = 100,
        graphql_comments_limit: int = 50,
        graphql_thread_comments_limit: int = 20,
    ):
        self.repo = repo
        self.state = state
        self.repo_path = Path(repo.config.path)
        self.repo_slug = self._get_repo_slug()
        self.current_login = self._get_current_login()
        self.ignored_comment_markers = (
            list(ignored_comment_markers)
            if ignored_comment_markers is not None
            else list(self._DEFAULT_IGNORED_COMMENT_MARKERS)
        )
        self.graphql_reviews_limit = graphql_reviews_limit
        self.graphql_threads_limit = graphql_threads_limit
        self.graphql_comments_limit = graphql_comments_limit
        self.graphql_thread_comments_limit = graphql_thread_comments_limit

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
            if any(
                t in err_text
                for t in (
                    "rate limit",
                    "timeout",
                    "connection",
                    "5xx",
                    "503",
                    "502",
                    "500",
                    "internal server",
                )
            ):
                if attempt < retries - 1:
                    import time

                    _logger.warning(
                        "Transient error on attempt %d/%d for %s, retrying in %.1fs",
                        attempt + 1,
                        retries,
                        cmd[0],
                        backoff,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
            raise last_err
        raise last_err  # type: ignore[misc]

    def _get_repo_slug(self) -> str:
        """Extract owner/repo from the git origin remote URL."""
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
        """Return open, non-draft PRs that match managed-PR heuristics."""
        managed_pr_limit = int(
            (self.repo.config.review_care or {}).get(
                "managed_pr_limit", _DEFAULT_MANAGED_PR_LIMIT
            )
        )
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
                    str(managed_pr_limit),
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
        """Check if a PR was authored by the QA agent or uses a QA branch prefix."""
        branch = branch or ""
        markers = ("qa/", "qa-", "fix/", "fix-", "auto-", "autofix/", "chore/qa-")
        if any(branch.startswith(prefix) for prefix in markers):
            return True
        if self.current_login and author == self.current_login:
            return True
        return False

    def fetch_review_snapshot(self, pr_number: int) -> Dict[str, Any]:
        """Fetch a PR's review state via the GitHub GraphQL API.

        Args:
            pr_number: GitHub pull request number.

        Returns:
            Normalized snapshot dict with review decision, comments, and fingerprint.
        """
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
                    f"query={self._build_graphql_query()}",
                ]
            )
        )
        pr = ((raw.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        if not pr:
            raise RuntimeError(f"PR #{pr_number} not found")
        return self._normalize_snapshot(pr)

    def _normalize_snapshot(self, pr: Dict[str, Any]) -> Dict[str, Any]:
        """Convert raw GraphQL PR data into a structured, fingerprinted snapshot."""
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
        """Filter out bot-generated and automated-review comments.

        Markers are configurable via the `ignored_comment_markers` constructor
        parameter; defaults to ``_DEFAULT_IGNORED_COMMENT_MARKERS``.
        """
        markers = (
            getattr(self, "ignored_comment_markers", None)
            or self._DEFAULT_IGNORED_COMMENT_MARKERS
        )
        return any(marker in body for marker in markers)

    def _build_graphql_query(self) -> str:
        """Build the GraphQL query with configured page sizes (L4).

        Returns the default ``GRAPHQL_QUERY`` when limits match the defaults;
        otherwise formats a query string with custom limits to handle PRs
        with more than the default number of reviews or threads.
        """
        if (
            self.graphql_reviews_limit == 100
            and self.graphql_threads_limit == 100
            and self.graphql_comments_limit == 50
            and self.graphql_thread_comments_limit == 20
        ):
            return GRAPHQL_QUERY
        return (
            f"query($owner: String!, $name: String!, $number: Int!) {{\n"
            f"  repository(owner: $owner, name: $name) {{\n"
            f"    pullRequest(number: $number) {{\n"
            f"      number\n"
            f"      url\n"
            f"      title\n"
            f"      isDraft\n"
            f"      state\n"
            f"      reviewDecision\n"
            f"      createdAt\n"
            f"      updatedAt\n"
            f"      author {{ login }}\n"
            f"      headRefName\n"
            f"      headRepositoryOwner {{ login }}\n"
            f"      mergeStateStatus\n"
            f"      reviews(last: {self.graphql_reviews_limit}) {{\n"
            f"        nodes {{\n"
            f"          author {{ login }}\n"
            f"          state\n"
            f"          submittedAt\n"
            f"        }}\n"
            f"      }}\n"
            f"      reviewThreads(first: {self.graphql_threads_limit}) {{\n"
            f"        nodes {{\n"
            f"          isResolved\n"
            f"          isOutdated\n"
            f"          comments(last: {self.graphql_thread_comments_limit}) {{\n"
            f"            nodes {{\n"
            f"              author {{ login }}\n"
            f"              body\n"
            f"              createdAt\n"
            f"            }}\n"
            f"          }}\n"
            f"        }}\n"
            f"      }}\n"
            f"      comments(last: {self.graphql_comments_limit}) {{\n"
            f"        nodes {{\n"
            f"          author {{ login }}\n"
            f"          body\n"
            f"          createdAt\n"
            f"        }}\n"
            f"      }}\n"
            f"    }}\n"
            f"  }}\n"
            f"}}\n"
        )

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
        """Classify a comment as actionable or informational based on keyword markers."""
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
