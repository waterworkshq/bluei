"""Batch PR creation and issue linking — gh subprocess wrappers.

Extracted from bluei/engine/batch_pr.py during god-module decomposition
(branch refactor/god-batch-pr-decomp, phase 2). These functions create
the PR for a batch and link the source issues via GitHub comments.

Public API (re-exported from bluei.engine.batch_pr for backward compat):
    create_batch_pr, link_issues_to_batch_pr
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from bluei.engine.models import (
    BatchGroup,
)

logger = logging.getLogger(__name__)


def create_batch_pr(
    batch: BatchGroup,
    repo_slug: str,
    log_file: Path,
    safety_config: Optional[dict] = None,
    repo_config: Optional[dict] = None,
) -> Dict[str, Any]:
    """Create a GitHub PR for the batch.

    Uses the standard `gh pr create` flow with batch-aware title and body.

    Returns dict with 'number' and 'url'.
    Raises RuntimeError on failure.
    """
    from bluei.engine.safety_gates import (
        check_pr_creation_allowed,
        resolve_base_branch,
    )
    from bluei.engine.utils import run_capture
    from bluei.engine.state import _append_text

    title = batch.pr_title()
    body = batch.pr_body()
    branch = batch.branch
    base_branch = resolve_base_branch(safety_config, repo_config)

    _append_text(log_file, f"batch-pr: creating PR for {batch.batch_id} title={title}")

    if safety_config:
        allowed, reason = check_pr_creation_allowed(base_branch, safety_config)
        if not allowed:
            _append_text(
                log_file,
                f"safety-block: batch PR creation blocked batch={batch.batch_id} reason={reason}",
            )
            raise RuntimeError(f"blocked-by-safety-mode: {reason}")

    rc, output = run_capture(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo_slug,
            "--title",
            title,
            "--body",
            body,
            "--head",
            branch,
            "--base",
            base_branch,
        ],
        cwd=batch.worktree_path,
    )

    if rc != 0:
        _append_text(
            log_file,
            f"batch-pr: gh pr create failed rc={rc} output={(output or '<empty>')[:300]}",
        )
        raise RuntimeError(f"Failed to create batch PR: {output}")

    # Find the line containing the PR URL (gh may output warnings before it)
    pr_url = ""
    for line in output.strip().splitlines():
        if "/pull/" in line:
            pr_url = line.strip()
            break
    pr_number = None
    if pr_url:
        try:
            pr_number = int(pr_url.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            logger.debug("Failed to parse PR number from URL")

    _append_text(log_file, f"batch-pr: created PR #{pr_number} url={pr_url}")
    return {"number": pr_number, "url": pr_url}


def link_issues_to_batch_pr(
    batch: BatchGroup,
    pr_number: int,
    pr_url: str,
    repo_slug: str,
    repo_path: Path,
    log_file: Path,
) -> None:
    """Update all issues in the batch to point to the shared PR.

    For each issue:
    - Set issue.github['pr_number'], ['pr_url'], ['batch_id']
    - Call set_issue_status(issue, 'pr_opened', ...)
    - Comment on the GitHub issue linking to the batch PR
    """
    from bluei.engine.orchestrator import set_issue_status
    from bluei.engine.gh import gh_issue_comment
    from bluei.engine.state import _append_text

    for issue in batch.issues:
        issue_github = issue.setdefault("github", {})
        issue_github["pr_number"] = pr_number
        issue_github["pr_url"] = pr_url
        issue_github["batch_id"] = batch.batch_id

        set_issue_status(issue, "pr_opened", f"batched in PR #{pr_number}")

        issue_number = issue_github.get("issue_number")
        if issue_number is not None:
            try:
                gh_issue_comment(
                    repo_slug,
                    issue_number,
                    f"This finding has been batched into PR #{pr_number}: {pr_url}",
                    cwd=repo_path,
                )
            except (subprocess.CalledProcessError, OSError) as exc:
                _append_text(
                    log_file,
                    f"batch-link: failed to comment on issue #{issue_number}: {exc}",
                )

        _append_text(
            log_file,
            f"batch-link: issue={issue.get('issue_id') or issue.get('id')} "
            f"linked to PR #{pr_number} batch={batch.batch_id}",
        )
