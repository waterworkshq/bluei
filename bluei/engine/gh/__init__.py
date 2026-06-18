"""GitHub API interaction layer for the sandbox local runner.

Historically a single 1222-line module (``bluei/engine/gh.py``); now a
package being decomposed incrementally per ``docs/plans/god-module-decomp/
gh.md``.  This ``__init__.py`` is the backward-compat facade: every public
symbol remains importable from ``bluei.engine.gh``, and the test patch
surface (``bluei.engine.gh.{gh_json, run_capture, time}``) is preserved.

Step 1 extracts only ``gh_json`` and ``finding_dedupe_marker`` into
:mod:`bluei.engine.gh._core`; the remaining functions stay here and are
moved out into per-concern submodules in later steps.

Every function wraps a ``gh`` CLI call via ``run_capture``.  Functions
return ``None`` / empty on failure — no exceptions are raised.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from bluei.engine.constants import DETECTOR_CATALOG
from bluei.engine.models import Finding
from bluei.engine.safety_gates import (
    check_merge_allowed,
    check_pr_creation_allowed,
    resolve_base_branch,
)
from bluei.engine.utils import run_capture

# Re-export the two primitives now living in _core so the public surface
# ``bluei.engine.gh.{gh_json, finding_dedupe_marker}`` stays intact and the
# test patch surface (patch("bluei.engine.gh.gh_json")) continues to reach
# the call sites in this module (name resolution happens at call time).
from bluei.engine.gh._core import finding_dedupe_marker, gh_json  # noqa: F401

# Step 2: repo identity & slug helpers moved to bluei.engine.gh.repo.
# get_origin_url resolves run_capture via the facade pattern (see repo.py);
# the other three are pure.  Re-exported here to preserve the public surface
# ``bluei.engine.gh.{get_origin_url, parse_github_repo, ...}``.
from bluei.engine.gh.repo import (  # noqa: F401
    get_origin_url,
    parse_github_repo,
    parse_issue_number_from_url,
    parse_pr_number_from_url,
)

logger = logging.getLogger(__name__)


def find_existing_github_issue(
    repo_slug: str, finding_id: str, cwd: Path
) -> Optional[Dict[str, Any]]:
    """Search for an existing GitHub issue containing a finding's dedupe marker.

    Args:
        repo_slug: GitHub repo in ``owner/repo`` format.
        finding_id: Unique finding identifier embedded in issue bodies.
        cwd: Working directory for the ``gh`` subprocess.

    Returns:
        Matching issue dict from ``gh issue list --json``, or ``None``.
    """
    marker = finding_dedupe_marker(finding_id)
    payload = gh_json(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo_slug,
            "--state",
            "all",
            "--limit",
            "200",
            "--json",
            "number,title,url,state,body",
        ],
        cwd=cwd,
    )
    if not isinstance(payload, list):
        return None
    for issue in payload:
        body = str(issue.get("body") or "")
        if marker in body:
            return issue
    return None


def find_existing_github_pr(
    repo_slug: str, finding_id: str, cwd: Path
) -> Optional[Dict[str, Any]]:
    """Search for an existing GitHub PR containing a finding's dedupe marker.

    Args:
        repo_slug: GitHub repo in ``owner/repo`` format.
        finding_id: Unique finding identifier embedded in PR bodies.
        cwd: Working directory for the ``gh`` subprocess.

    Returns:
        Matching PR dict from ``gh pr list --json``, or ``None``.
    """
    marker = finding_dedupe_marker(finding_id)
    payload = gh_json(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo_slug,
            "--state",
            "all",
            "--limit",
            "200",
            "--json",
            "number,title,url,state,body,headRefName",
        ],
        cwd=cwd,
    )
    if not isinstance(payload, list):
        return None
    for pr in payload:
        body = str(pr.get("body") or "")
        if marker in body:
            return pr
    return None


def find_batch_pr_by_rule(
    repo_slug: str,
    rule_pattern: str,
    cwd: Path,
    max_age_hours: int = 24,
) -> Optional[Dict[str, Any]]:
    """Find an existing open batch PR for the same rule pattern.

    Args:
        repo_slug: GitHub repo in ``owner/repo`` format.
        rule_pattern: Rule name used to derive the branch prefix.
        cwd: Working directory for the ``gh`` subprocess.
        max_age_hours: Maximum PR age in hours to consider a match.

    Returns:
        Matching PR dict, or ``None`` if no active duplicate found.
    """
    rule_short = rule_pattern.replace("ruff-", "")[:8]
    branch_prefix = f"qa/batch-{rule_short}-"

    payload = gh_json(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo_slug,
            "--state",
            "open",
            "--limit",
            "50",
            "--json",
            "number,title,headRefName,createdAt,url",
        ],
        cwd=cwd,
    )
    if not isinstance(payload, list):
        return None

    now = datetime.now(timezone.utc)

    for pr in payload:
        ref = pr.get("headRefName", "")
        if not ref.startswith(branch_prefix):
            continue

        created_str = pr.get("createdAt")
        if not created_str:
            continue

        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        age_hours = (now - created).total_seconds() / 3600
        if age_hours <= max_age_hours:
            return pr

    return None


def gh_issue_comment(repo_slug: str, issue_number: int, body: str, cwd: Path) -> bool:
    rc, _ = run_capture(
        [
            "gh",
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            repo_slug,
            "--body",
            body,
        ],
        cwd=cwd,
    )
    return rc == 0


def gh_issue_close(repo_slug: str, issue_number: int, comment: str, cwd: Path) -> bool:
    rc, _ = run_capture(
        [
            "gh",
            "issue",
            "close",
            str(issue_number),
            "--repo",
            repo_slug,
            "--comment",
            comment,
        ],
        cwd=cwd,
    )
    return rc == 0


def gh_pr_comment(repo_slug: str, pr_number: int, body: str, cwd: Path) -> bool:
    rc, _ = run_capture(
        ["gh", "pr", "comment", str(pr_number), "--repo", repo_slug, "--body", body],
        cwd=cwd,
    )
    return rc == 0


def finding_from_issue_record(issue: Dict[str, Any]) -> Optional[Finding]:
    """Reconstruct a :class:`Finding` from a persisted issue record dict.

    Args:
        issue: Dict with keys ``finding_id``, ``path``, ``rule``, ``snippet``,
            ``repo``, ``line``, ``confidence``, ``quick_win``, ``safe_to_autofix``.

    Returns:
        Populated :class:`Finding`, or ``None`` if required fields are missing.
    """
    finding_id = str(issue.get("finding_id") or "").strip()
    path = str(issue.get("path") or "").strip()
    rule = str(issue.get("rule") or "").strip()
    rule_aliases = {
        "max-lines": "xo-max-lines",
        "complexity": "xo-complexity",
        "no-warning-comments": "xo-no-warning-comments",
    }
    rule = rule_aliases.get(rule, rule)
    snippet = str(issue.get("snippet") or "").strip()
    repo = str(issue.get("repo") or "qa-sandbox-repo").strip() or "qa-sandbox-repo"
    if not finding_id or not path or not rule:
        return None
    try:
        line = int(issue.get("line", 0))
    except Exception:
        line = 0
    try:
        confidence = float(issue.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    rule_meta = next(
        (entry for entry in DETECTOR_CATALOG if entry.get("rule") == rule), {}
    )
    inferred_autofix = bool(rule_meta.get("autofix", False))
    return Finding(
        finding_id=finding_id,
        repo=repo,
        path=path,
        line=line,
        rule=rule,
        snippet=snippet,
        confidence=confidence,
        quick_win=bool(issue.get("quick_win", confidence >= 0.9)),
        safe_to_autofix=bool(issue.get("safe_to_autofix", inferred_autofix)),
    )


def _load_self_merge_repos_from_global_config() -> List[str]:
    """Read ``github.self_merge_repos`` from the workspace ``config.yaml``
    without going through the app layer.

    Mirrors the path resolution of ``bluei.app.config.load_global_config``
    (``QA_AGENT_WORKSPACE`` env var or repo root) but reads only the specific
    key we need. Avoids an engine->app import (rec-08).
    """
    workspace = Path(
        os.environ.get("QA_AGENT_WORKSPACE", Path(__file__).resolve().parents[2])
    ).expanduser()
    config_path = workspace / "config.yaml"
    if not config_path.exists():
        return []
    try:
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return []
        value = data.get("github", {}).get("self_merge_repos", [])
        return list(value) if value else []
    except Exception:
        return []


def repo_is_sandbox(
    repo_slug: str, self_merge_repos: Optional[List[str]] = None
) -> bool:
    """Check whether a repo slug is a sandbox or self-merge repo.

    Args:
        repo_slug: GitHub repo in ``owner/repo`` format.
        self_merge_repos: Optional list of self-merge repo slugs. If None,
            reads from global config (``github.self_merge_repos``) with
            env var fallback (``BLUEI_SELF_MERGE_REPOS``).

    Returns:
        ``True`` if the repo ends with ``qa-sandbox-repo`` or is listed
        in the self-merge list.
    """
    if repo_slug.endswith("/qa-sandbox-repo"):
        return True
    if self_merge_repos is None:
        self_merge_repos = _load_self_merge_repos_from_global_config()
        if not self_merge_repos:
            env_val = os.environ.get("BLUEI_SELF_MERGE_REPOS", "")
            self_merge_repos = (
                [s.strip() for s in env_val.split(",") if s.strip()] if env_val else []
            )
    for slug in self_merge_repos:
        if repo_slug == slug or repo_slug.endswith("/" + slug):
            return True
    return False


def fetch_open_prs_for_merge(repo_slug: str, cwd: Path) -> List[Dict[str, Any]]:
    """Fetch open PRs sorted for merge ordering (non-drafts first, oldest first).

    Args:
        repo_slug: GitHub repo in ``owner/repo`` format.
        cwd: Working directory for the ``gh`` subprocess.

    Returns:
        List of PR dicts sorted by ``(isDraft, createdAt, number)``.
    """
    payload = gh_json(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo_slug,
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number,url,title,state,isDraft,createdAt,headRefName,baseRefName",
        ],
        cwd=cwd,
    )
    if not isinstance(payload, list):
        return []

    def _sort_key(pr: Dict[str, Any]) -> tuple[int, str, int]:
        created_at = str(pr.get("createdAt") or "")
        number = int(pr.get("number") or 0)
        draft_rank = 1 if bool(pr.get("isDraft")) else 0
        return (draft_rank, created_at, number)

    return sorted(payload, key=_sort_key)


def evaluate_pr_check_health(
    repo_slug: str, pr_number: int, cwd: Path
) -> Dict[str, Any]:
    """Evaluate CI check status for a PR.

    Args:
        repo_slug: GitHub repo in ``owner/repo`` format.
        pr_number: PR number.
        cwd: Working directory for the ``gh`` subprocess.

    Returns:
        Dict with ``eligible`` (bool), ``has_checks`` (bool), and ``reason`` (str).
    """
    payload = gh_json(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo_slug,
            "--json",
            "number,url,statusCheckRollup",
        ],
        cwd=cwd,
    )
    if not isinstance(payload, dict):
        return {
            "eligible": True,
            "has_checks": False,
            "reason": "checks-unavailable-proceed-cautiously",
        }

    rollup = payload.get("statusCheckRollup")
    if not isinstance(rollup, list) or len(rollup) == 0:
        return {
            "eligible": True,
            "has_checks": False,
            "reason": "no-checks-detected-proceed-cautiously",
        }

    failing_states = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR"}
    pending_states = {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED"}
    seen_pending = False
    for check in rollup:
        state = str(
            check.get("conclusion") or check.get("state") or check.get("status") or ""
        ).upper()
        if state in failing_states:
            return {
                "eligible": False,
                "has_checks": True,
                "reason": f"checks-failing:{state}",
            }
        if state in pending_states:
            seen_pending = True

    if seen_pending:
        return {
            "eligible": True,
            "has_checks": True,
            "reason": "checks-pending-no-failures",
        }

    return {
        "eligible": True,
        "has_checks": True,
        "reason": "checks-pass-or-neutral",
    }


def evaluate_pr_reviews(repo_slug: str, pr_number: int, cwd: Path) -> Dict[str, Any]:
    """Evaluate PR review status and branch-protection requirements.

    Args:
        repo_slug: GitHub repo in ``owner/repo`` format.
        pr_number: PR number.
        cwd: Working directory for the ``gh`` subprocess.

    Returns:
        Dict with ``eligible`` (bool), ``has_reviews`` (bool), and ``reason`` (str).
    """
    payload = gh_json(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo_slug,
            "--json",
            "number,reviews,latestReviews,state,baseRefName",
        ],
        cwd=cwd,
    )
    if not isinstance(payload, dict):
        return {
            "eligible": False,
            "has_reviews": False,
            "reason": "reviews-unavailable-block",
        }

    latest = payload.get("latestReviews") or []
    states = {r.get("state") for r in latest}
    # Treat COMMENTED-only reviews as absent — they carry no substantive verdict
    substantive_states = states - {"COMMENTED", "DISMISSED"}

    if "CHANGES_REQUESTED" in substantive_states:
        return {"eligible": False, "has_reviews": True, "reason": "changes-requested"}
    if "PENDING" in substantive_states:
        return {"eligible": False, "has_reviews": True, "reason": "review-pending"}

    approved = "APPROVED" in substantive_states
    has_reviews = len(substantive_states) > 0

    # If reviews exist, enforce standard review policy
    if has_reviews:
        if not approved:
            return {
                "eligible": False,
                "has_reviews": True,
                "reason": "no-approval-found",
            }
        return {"eligible": True, "has_reviews": True, "reason": "review-check-pass"}

    # No reviews at all — check if branch protection requires them
    base_branch = payload.get("baseRefName") or "main"
    protection = gh_json(
        ["gh", "api", f"repos/{repo_slug}/branches/{base_branch}/protection"],
        cwd=cwd,
    )
    requires_reviews = isinstance(protection, dict) and isinstance(
        protection.get("required_pull_request_reviews"), dict
    )
    if requires_reviews:
        return {
            "eligible": False,
            "has_reviews": False,
            "reason": "no-reviews-but-protection-requires-them",
        }

    # No reviews, no branch protection requiring them — proceed
    return {
        "eligible": True,
        "has_reviews": False,
        "reason": "no-reviews-no-protection-pass",
    }


def evaluate_pr_mergeability(
    repo_slug: str, pr_number: int, cwd: Path
) -> Dict[str, Any]:
    """Evaluate merge readiness from GitHub's ``mergeStateStatus``.

    Args:
        repo_slug: GitHub repo in ``owner/repo`` format.
        pr_number: PR number.
        cwd: Working directory for the ``gh`` subprocess.

    Returns:
        Dict with ``eligible`` (bool), ``requires_pr_fix`` (bool),
        ``merge_state_status`` (str), and ``reason`` (str).
    """
    payload = gh_json(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo_slug,
            "--json",
            "number,url,mergeStateStatus,reviewDecision",
        ],
        cwd=cwd,
    )
    if not isinstance(payload, dict):
        return {
            "eligible": True,
            "requires_pr_fix": False,
            "merge_state_status": "UNKNOWN",
            "reason": "merge-state-unavailable-proceed-cautiously",
        }

    merge_state = str(payload.get("mergeStateStatus") or "UNKNOWN").upper()
    if merge_state == "DIRTY":
        return {
            "eligible": False,
            "requires_pr_fix": True,
            "merge_state_status": merge_state,
            "reason": "merge-conflict-dirty",
        }
    if merge_state == "BEHIND":
        return {
            "eligible": False,
            "requires_pr_fix": True,
            "merge_state_status": merge_state,
            "reason": "branch-behind-base",
        }
    if merge_state == "UNKNOWN":
        return {
            "eligible": True,
            "requires_pr_fix": False,
            "merge_state_status": merge_state,
            "reason": "merge-state-unknown-proceed-cautiously",
        }
    if merge_state == "UNSTABLE":
        check_health = evaluate_pr_check_health(repo_slug, pr_number, cwd)
        if check_health.get("eligible"):
            return {
                "eligible": True,
                "requires_pr_fix": False,
                "merge_state_status": merge_state,
                "reason": f"merge-state-unstable-{check_health.get('reason')}",
            }
        return {
            "eligible": False,
            "requires_pr_fix": False,
            "merge_state_status": merge_state,
            "reason": f"merge-state-unstable-{check_health.get('reason')}",
        }
    if merge_state == "BLOCKED":
        return {
            "eligible": False,
            "requires_pr_fix": False,
            "merge_state_status": merge_state,
            "reason": "merge-state-blocked",
        }
    if merge_state == "HAS_HOOKS":
        return {
            "eligible": True,
            "requires_pr_fix": False,
            "merge_state_status": merge_state,
            "reason": "mergeable-with-hooks",
        }

    return {
        "eligible": True,
        "requires_pr_fix": False,
        "merge_state_status": merge_state,
        "reason": "mergeable-state-pass",
    }


def merge_failure_requires_pr_fix(reason: str) -> bool:
    """Return ``True`` if a merge failure reason indicates a fixable conflict.

    Args:
        reason: Error string from a failed ``gh pr merge``.

    Returns:
        ``True`` if the failure is due to conflicts or a behind branch.
    """
    normalized = reason.strip().lower()
    markers = (
        "not mergeable",
        "cannot be cleanly created",
        "merge conflict",
        "conflict",
        "is behind the base branch",
        "head branch is out of date",
    )
    return any(marker in normalized for marker in markers)


def merge_pr(
    repo_slug: str,
    pr_number: int,
    dry_run: bool,
    cwd: Path,
    safety_config: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Merge a PR via ``gh pr merge --merge --delete-branch``.

    Args:
        repo_slug: GitHub repo in ``owner/repo`` format.
        pr_number: PR number.
        dry_run: If ``True``, simulate the merge without executing.
        cwd: Working directory for the ``gh`` subprocess.
        safety_config: Optional safety policy dict. When provided, the merge is
            blocked unless the safety mode is ``merge``. When omitted/empty, no
            enforcement is applied (backward compat).

    Returns:
        ``(success, reason)`` tuple.
    """
    if safety_config:
        allowed, reason = check_merge_allowed(safety_config)
        if not allowed:
            return False, f"blocked-by-safety-mode: {reason}"
    if dry_run:
        return True, "dry-run-merge-simulated"
    rc, out = run_capture(
        [
            "gh",
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            repo_slug,
            "--merge",
            "--delete-branch",
        ],
        cwd=cwd,
    )
    if rc == 0:
        return True, "merged"
    normalized = (out or "").lower()
    already_handled_markers = (
        "already merged",
        "already been merged",
        "merge queue",
        "auto-merge",
        "queued for merge",
        "is queued",
    )
    if any(marker in normalized for marker in already_handled_markers):
        return True, "already-merged-or-queued"
    return False, (out.strip() or f"gh-pr-merge-failed-rc={rc}")


def create_or_update_github_issue(
    repo_slug: str,
    finding: Finding,
    dry_run: bool,
    log_file: Path,
    cwd: Path,
) -> Dict[str, Any]:
    """Create a GitHub issue for a finding, or comment on an existing one.

    Args:
        repo_slug: GitHub repo in ``owner/repo`` format.
        finding: The :class:`Finding` to file an issue for.
        dry_run: If ``True``, log actions without executing.
        log_file: Path to append log entries to.
        cwd: Working directory for the ``gh`` subprocess.

    Returns:
        Dict with ``number``, ``url``, ``created`` (bool), and optionally ``error``.
    """
    from bluei.engine.models import now_iso
    from bluei.engine.state import _append_text

    marker = finding_dedupe_marker(finding.finding_id)
    existing = find_existing_github_issue(repo_slug, finding.finding_id, cwd=cwd)
    sync_note = (
        f"Sandbox runner sync at {now_iso()}\\n"
        f"- rule: {finding.rule}\\n"
        f"- path: {finding.path}:{finding.line}\\n"
        f"- confidence: {finding.confidence}"
    )

    if existing:
        number = int(existing["number"])
        url = str(existing.get("url") or "")
        if dry_run:
            _append_text(
                log_file,
                f"dry-run-live: would comment existing GitHub issue #{number} finding_id={finding.finding_id}",
            )
        else:
            gh_issue_comment(repo_slug, number, sync_note, cwd=cwd)
            _append_text(
                log_file,
                f"live: commented existing GitHub issue #{number} finding_id={finding.finding_id}",
            )
        return {"number": number, "url": url, "created": False}

    title = f"[bluei] {finding.rule} in {finding.path}:{finding.line}"
    body = "\n".join(
        [
            "Automated speck found by bluei.",
            "",
            marker,
            f"- dedupe_key: {finding.finding_id}",
            f"- repo: {finding.repo}",
            f"- file: {finding.path}:{finding.line}",
            f"- rule: {finding.rule}",
            f"- confidence: {finding.confidence}",
            f"- snippet: `{finding.snippet}`",
        ]
    )

    if dry_run:
        _append_text(
            log_file,
            f"dry-run-live: would create GitHub issue for finding_id={finding.finding_id}",
        )
        return {"number": None, "url": "", "created": True}

    rc, out = run_capture(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            repo_slug,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=cwd,
    )
    if rc != 0:
        _append_text(
            log_file, f"error: gh issue create failed finding_id={finding.finding_id}"
        )
        return {
            "number": None,
            "url": "",
            "created": False,
            "error": "issue-create-failed",
        }
    url = out.strip().splitlines()[-1] if out.strip() else ""
    number = parse_issue_number_from_url(url)
    if number is None:
        existing_after_create = find_existing_github_issue(
            repo_slug, finding.finding_id, cwd=cwd
        )
        if existing_after_create:
            number = int(existing_after_create["number"])
            url = str(existing_after_create.get("url") or url)
    _append_text(
        log_file,
        f"live: created GitHub issue url={url} finding_id={finding.finding_id}",
    )
    return {"number": number, "url": url, "created": True}


def create_or_update_github_pr(
    repo_slug: str,
    finding: Finding,
    branch: str,
    issue_number: Optional[int],
    dry_run: bool,
    log_file: Path,
    cwd: Path,
    safety_config: Optional[Dict[str, Any]] = None,
    repo_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a GitHub PR for a finding, or reuse an existing one.

    Args:
        repo_slug: GitHub repo in ``owner/repo`` format.
        finding: The :class:`Finding` to open a PR for.
        branch: Head branch name for the PR.
        issue_number: Optional issue number to link via ``Fixes #N``.
        dry_run: If ``True``, log actions without executing.
        log_file: Path to append log entries to.
        cwd: Working directory for the ``gh`` subprocess.
        safety_config: Optional safety policy dict. When provided, PR creation
            is blocked unless the safety mode allows it (``pr`` or ``merge``).
            When omitted/empty, no enforcement is applied (backward compat).
        repo_config: Optional repo config dict used to resolve the PR base
            branch (``base_branch`` / ``default_branch`` override). When
            omitted, the base falls back to ``protected_branches[0]`` then
            ``"main"``.

    Returns:
        Dict with ``number``, ``url``, ``created`` (bool), and optionally ``error``.
    """
    from bluei.engine.models import now_iso
    from bluei.engine.state import _append_text

    marker = finding_dedupe_marker(finding.finding_id)
    existing = find_existing_github_pr(repo_slug, finding.finding_id, cwd=cwd)
    if existing:
        number = int(existing["number"])
        url = str(existing.get("url") or "")
        _append_text(
            log_file,
            f"live-idempotent: reuse existing PR #{number} finding_id={finding.finding_id}",
        )
        return {"number": number, "url": url, "created": False}

    body_lines = [
        "Automated fix from bluei.",
        "",
        marker,
        f"- dedupe_key: {finding.finding_id}",
        f"- rule: {finding.rule}",
        f"- file: {finding.path}:{finding.line}",
    ]
    if issue_number is not None:
        body_lines.append(f"Fixes #{issue_number}")
    body = "\n".join(body_lines)
    title = f"fix(bluei): {finding.rule} [{finding.path}]"

    base_branch = resolve_base_branch(safety_config, repo_config)

    if dry_run:
        _append_text(
            log_file,
            f"dry-run-live: would open PR from branch={branch} base={base_branch} finding_id={finding.finding_id}",
        )
        return {"number": None, "url": "", "created": True}

    if safety_config:
        allowed, reason = check_pr_creation_allowed(base_branch, safety_config)
        if not allowed:
            _append_text(
                log_file,
                f"safety-block: PR creation blocked finding_id={finding.finding_id} reason={reason}",
            )
            return {
                "number": None,
                "url": "",
                "created": False,
                "error": "blocked-by-safety-mode",
            }

    rc, out = run_capture(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo_slug,
            "--base",
            base_branch,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=cwd,
    )
    if rc != 0:
        _append_text(
            log_file,
            f"error: gh pr create failed branch={branch} finding_id={finding.finding_id}",
        )
        return {
            "number": None,
            "url": "",
            "created": False,
            "error": "pr-create-failed",
        }
    url = out.strip().splitlines()[-1] if out.strip() else ""
    number = parse_pr_number_from_url(url)
    if number is None:
        existing_after_create = find_existing_github_pr(
            repo_slug, finding.finding_id, cwd=cwd
        )
        if existing_after_create:
            number = int(existing_after_create["number"])
            url = str(existing_after_create.get("url") or url)
    _append_text(
        log_file, f"live: created PR url={url} finding_id={finding.finding_id}"
    )
    return {"number": number, "url": url, "created": True}


def fetch_github_live_counts(repo_path: Path) -> tuple[Optional[Dict[str, int]], str]:
    """Fetch live open issue and PR counts from GitHub via GraphQL.

    Args:
        repo_path: Local repo root (used to resolve remote URL and ``gh`` cwd).

    Returns:
        ``(counts, status)`` where *counts* is ``{'open_issues': N, 'open_prs': N}``
        or ``None`` on failure, and *status* is a human-readable label.
    """
    origin_url = get_origin_url(repo_path)
    if "github.com" not in origin_url:
        return None, "non-github-origin"

    owner, name = parse_github_repo(origin_url)
    if not owner or not name:
        return None, "github-origin-parse-failed"

    query = (
        "query($owner:String!, $name:String!) { "
        "repository(owner:$owner, name:$name) { "
        "issues(states: OPEN) { totalCount } "
        "pullRequests(states: OPEN) { totalCount } "
        "} }"
    )
    rc, out = run_capture(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
        ],
        cwd=repo_path,
    )
    if rc != 0:
        return None, f"github-origin-live-state-unavailable rc={rc}"

    try:
        payload = json.loads(out)
        repo_data = payload["data"]["repository"]
        return (
            {
                "open_issues": int(repo_data["issues"]["totalCount"]),
                "open_prs": int(repo_data["pullRequests"]["totalCount"]),
            },
            "github-origin-live-state",
        )
    except Exception:
        return None, "github-origin-live-state-invalid-response"


def evaluate_pr_regression(
    repo_slug: str,
    pr_number: int,
    cwd: Path,
) -> Dict[str, Any]:
    """Evaluate a PR for regressions by inspecting its diff and running checks.

    Args:
        repo_slug: GitHub repo in ``owner/repo`` format.
        pr_number: PR number.
        cwd: Working directory (must contain a clone of the repo).

    Returns:
        Dict with ``score`` (float 0.0–1.0), ``has_regressions`` (bool),
        ``findings``, ``removed_tests``, ``export_changes``, ``lint_regressions``,
        ``pr_info``, ``check_health``, ``has_diff``, and ``action``
        (one of ``safe-to-merge``, ``review-required``, ``block-merge``).
    """
    # 1. Fetch PR metadata
    pr_info_payload = gh_json(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo_slug,
            "--json",
            "number,headRefName,baseRefName,title",
        ],
        cwd=cwd,
    )
    if not isinstance(pr_info_payload, dict):
        return {
            "score": 0.0,
            "has_regressions": False,
            "findings": [],
            "removed_tests": [],
            "export_changes": [],
            "lint_regressions": [],
            "pr_info": None,
            "action": "safe-to-merge",
            "error": "pr-info-unavailable",
        }

    pr_info = {
        "number": pr_info_payload.get("number"),
        "head_ref": pr_info_payload.get("headRefName"),
        "base_ref": pr_info_payload.get("baseRefName"),
        "title": pr_info_payload.get("title"),
    }

    head_ref = pr_info.get("head_ref") or "HEAD"
    base_ref = pr_info.get("base_ref") or "main"

    # 2. Fetch PR diff
    rc, diff_out = run_capture(
        ["gh", "pr", "diff", str(pr_number), "--repo", repo_slug],
        cwd=cwd,
    )
    has_diff = rc == 0 and bool(diff_out.strip())

    # 3. Fetch status checks for context
    check_health = evaluate_pr_check_health(repo_slug, pr_number, cwd)

    # 4. Run regression checks locally
    from bluei.engine.regression import (
        _find_test_deletions,
        _find_export_changes,
        _find_lint_regressions,
        _git_diff_names,
    )

    # Build a changes list from the PR diff
    changes: List[Dict[str, str]] = []
    if has_diff:
        # Parse the unified diff to extract file statuses
        for line in diff_out.strip().splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                if len(parts) >= 4:
                    b_path = parts[3].lstrip("b/") if len(parts) > 3 else ""
                    # Default to modified (will refine below)
                    changes.append({"status": "M", "path": b_path})
            elif line.startswith("--- /dev/null"):
                if changes:
                    changes[-1]["status"] = "A"
            elif line.startswith("+++ /dev/null"):
                if changes:
                    changes[-1]["status"] = "D"

    # Deduplicate by path, keeping latest status
    deduped: Dict[str, str] = {}
    for change in changes:
        deduped[change["path"]] = change["status"]
    changes = [{"status": v, "path": k} for k, v in deduped.items()]

    # 5. Run regression check functions
    removed_tests = [
        c["path"]
        for c in changes
        if c["status"] == "D"
        and c["path"].startswith(("tests/", "test_", "spec/", "__tests__/"))
    ]

    export_changes_list: List[str] = []
    export_findings = _find_export_changes(changes, cwd)
    for entry in export_findings:
        etype = entry["type"]
        epath = entry["path"]
        if etype == "module_init_deleted":
            export_changes_list.append(f"Init module deleted: {epath}")
        elif etype == "export_module_deleted":
            export_changes_list.append(f"Export module deleted: {epath}")
        else:
            export_changes_list.append(f"Export change detected: {epath}")

    # Lint regressions — run on the repo checkout with the head branch
    lint_regs = _find_lint_regressions(cwd, base_ref, head_ref)
    lint_lines = []
    for r in lint_regs:
        lint_lines.append(
            f"{r.get('file', '?')}:{r.get('line', 0)} {r.get('rule', '?')}: {r.get('message', '')}"
        )

    # 6. Compute regression score
    from bluei.engine.regression import compute_regression_score

    score = compute_regression_score(cwd, base_ref)

    # 7. Assemble findings
    all_findings: List[Dict[str, Any]] = []
    for tp in removed_tests:
        all_findings.append({"type": "test_file_deleted", "path": tp})
    for ep in export_changes_list:
        all_findings.append({"type": "export_change", "description": ep})
    for lr in lint_regs:
        all_findings.append({"type": "lint_regression", **lr})

    has_regressions = len(all_findings) > 0

    # 8. Determine action
    if score > 0.5:
        action = "block-merge"
    elif score > 0.3:
        action = "review-required"
    else:
        action = "safe-to-merge"

    return {
        "score": round(score, 4),
        "has_regressions": has_regressions,
        "findings": all_findings,
        "removed_tests": removed_tests,
        "export_changes": export_changes_list,
        "lint_regressions": lint_lines,
        "pr_info": pr_info,
        "check_health": check_health,
        "has_diff": has_diff,
        "action": action,
    }


__all__ = [
    "gh_json",
    "finding_dedupe_marker",
    "run_capture",
    "time",
    "get_origin_url",
    "parse_github_repo",
    "parse_issue_number_from_url",
    "parse_pr_number_from_url",
    "repo_is_sandbox",
    "create_or_update_github_issue",
    "find_existing_github_issue",
    "finding_from_issue_record",
    "gh_issue_close",
    "gh_issue_comment",
    "create_or_update_github_pr",
    "find_batch_pr_by_rule",
    "find_existing_github_pr",
    "gh_pr_comment",
    "merge_failure_requires_pr_fix",
    "merge_pr",
    "evaluate_pr_check_health",
    "evaluate_pr_mergeability",
    "evaluate_pr_reviews",
    "fetch_open_prs_for_merge",
    "evaluate_pr_regression",
    "fetch_github_live_counts",
]
