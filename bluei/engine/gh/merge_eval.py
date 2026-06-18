"""Merge-gate evaluators for ``bluei.engine.gh``.

Extracted in Step 6 of the god-module decomposition (see
``docs/plans/god-module-decomp/gh.md``).  Four pure-ish read-only evaluators
that the merge orchestrator consults before deciding whether a PR may be
merged:

* :func:`fetch_open_prs_for_merge` -- list open PRs sorted for merge ordering.
* :func:`evaluate_pr_check_health` -- CI status rollup classifier.
* :func:`evaluate_pr_reviews` -- review state + branch-protection classifier.
* :func:`evaluate_pr_mergeability` -- ``mergeStateStatus`` classifier; calls
  :func:`evaluate_pr_check_health` for the ``UNSTABLE`` branch.

Patch-surface note (plan Risk #1, section 4.1): every ``gh_json`` call is
routed through the ``_facade`` reference (the parent package) rather than
imported into this module's globals.  This preserves
``patch("bluei.engine.gh.gh_json")`` reaching the call sites here -- the
patch replaces the attribute on the facade module and ``_facade.gh_json``
looks it up at call time.  Pattern mirrors :mod:`bluei.engine.gh._core`,
:mod:`bluei.engine.gh.repo`, :mod:`bluei.engine.gh.sandbox`,
:mod:`bluei.engine.gh.issue_ops`, and :mod:`bluei.engine.gh.pr_ops`.

Intra-module call note: ``evaluate_pr_mergeability`` invokes
``evaluate_pr_check_health`` by bare name (direct call), not through the
facade -- both live in this module after the extraction, so name resolution
finds the local definition.  The test patch surface for
``evaluate_pr_check_health`` itself is preserved by the facade re-export
from ``__init__``; consumers that patch ``bluei.engine.gh.
evaluate_pr_check_health`` (rather than the local reference inside this
module) still observe the public symbol.  This mirrors how the original
monolith invoked itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

# Call-time resolution of gh_json through the facade.  See
# bluei.engine.gh._core for the same pattern and its rationale.
import bluei.engine.gh as _facade


def fetch_open_prs_for_merge(repo_slug: str, cwd: Path) -> List[Dict[str, Any]]:
    """Fetch open PRs sorted for merge ordering (non-drafts first, oldest first).

    Args:
        repo_slug: GitHub repo in ``owner/repo`` format.
        cwd: Working directory for the ``gh`` subprocess.

    Returns:
        List of PR dicts sorted by ``(isDraft, createdAt, number)``.
    """
    payload = _facade.gh_json(
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
    payload = _facade.gh_json(
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
    payload = _facade.gh_json(
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
    protection = _facade.gh_json(
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
    payload = _facade.gh_json(
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
        # Same-module direct call -- evaluate_pr_check_health lives in this
        # module after the Step 6 extraction, so bare-name resolution finds
        # the local definition (mirrors the original monolith's self-call).
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
