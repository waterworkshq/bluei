"""PR lifecycle ops for ``bluei.engine.gh``.

Extracted in Step 5 of the god-module decomposition (see
``docs/plans/god-module-decomp/gh.md``).  Six functions covering the GitHub
PR lifecycle: dedup lookup, batch-PR discovery, comment, merge-readiness
classifier, merge hub, and the create-or-update hub.

Patch-surface note (plan Risk #1, section 4.1): every call to a gh-internal
helper (``gh_json``, ``run_capture``, ``finding_dedupe_marker``,
``find_existing_github_pr``, ``parse_pr_number_from_url``) is routed through
the ``_facade`` reference (the parent package) rather than imported into
this module's globals.  This preserves ``patch("bluei.engine.gh.<name>")``
reaching the call sites here -- the patch replaces the attribute on the
facade module and ``_facade.<name>`` looks it up at call time.  Pattern
mirrors :mod:`bluei.engine.gh._core`, :mod:`bluei.engine.gh.repo`, and
:mod:`bluei.engine.gh.issue_ops`.

Non-gh imports stay at their canonical locations: ``check_merge_allowed``,
``check_pr_creation_allowed``, ``resolve_base_branch`` from
``bluei.engine.safety_gates``; ``Finding`` from ``bluei.engine.models``;
``datetime``/``timezone`` from the stdlib; and the lazy function-local
imports of ``now_iso`` / ``_append_text`` are preserved verbatim.

``merge_failure_requires_pr_fix`` is pure and needs no facade indirection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from bluei.engine.models import Finding
from bluei.engine.safety_gates import (
    check_merge_allowed,
    check_pr_creation_allowed,
    resolve_base_branch,
)

# Call-time resolution of gh-internal helpers through the facade.  See
# bluei.engine.gh._core for the same pattern and its rationale.
import bluei.engine.gh as _facade


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
    marker = _facade.finding_dedupe_marker(finding_id)
    payload = _facade.gh_json(
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


def gh_pr_comment(repo_slug: str, pr_number: int, body: str, cwd: Path) -> bool:
    rc, _ = _facade.run_capture(
        ["gh", "pr", "comment", str(pr_number), "--repo", repo_slug, "--body", body],
        cwd=cwd,
    )
    return rc == 0


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
    rc, out = _facade.run_capture(
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

    marker = _facade.finding_dedupe_marker(finding.finding_id)
    existing = _facade.find_existing_github_pr(repo_slug, finding.finding_id, cwd=cwd)
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

    rc, out = _facade.run_capture(
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
    number = _facade.parse_pr_number_from_url(url)
    if number is None:
        existing_after_create = _facade.find_existing_github_pr(
            repo_slug, finding.finding_id, cwd=cwd
        )
        if existing_after_create:
            number = int(existing_after_create["number"])
            url = str(existing_after_create.get("url") or url)
    _append_text(
        log_file, f"live: created PR url={url} finding_id={finding.finding_id}"
    )
    return {"number": number, "url": url, "created": True}
