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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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

# Step 3: sandbox & self-merge policy moved to bluei.engine.gh.sandbox.
# repo_is_sandbox is re-exported here to preserve the public surface; the
# private _load_self_merge_repos_from_global_config is module-local and not
# re-exported.  Note the parents[3] fix documented in sandbox.py.
from bluei.engine.gh.sandbox import repo_is_sandbox  # noqa: F401

# Step 4: issue lifecycle moved to bluei.engine.gh.issue_ops.  All five
# functions route their gh-internal calls (gh_json, run_capture,
# finding_dedupe_marker, find_existing_github_issue, gh_issue_comment,
# parse_issue_number_from_url) through this facade via ``_facade.X(...)`` --
# so patches on ``bluei.engine.gh.<name>`` continue to reach the call sites
# in issue_ops.py at call time.
from bluei.engine.gh.issue_ops import (  # noqa: F401
    create_or_update_github_issue,
    find_existing_github_issue,
    finding_from_issue_record,
    gh_issue_close,
    gh_issue_comment,
)

# Step 5: PR lifecycle moved to bluei.engine.gh.pr_ops.  All six functions
# route their gh-internal calls (gh_json, run_capture, finding_dedupe_marker,
# find_existing_github_pr, parse_pr_number_from_url) through this facade via
# ``_facade.X(...)``.  safety_gates imports (check_merge_allowed,
# check_pr_creation_allowed, resolve_base_branch) stay at their canonical
# location inside pr_ops.py -- they are not part of the gh patch surface.
from bluei.engine.gh.pr_ops import (  # noqa: F401
    create_or_update_github_pr,
    find_batch_pr_by_rule,
    find_existing_github_pr,
    gh_pr_comment,
    merge_failure_requires_pr_fix,
    merge_pr,
)

# Step 6: merge-gate evaluators moved to bluei.engine.gh.merge_eval.  All
# four functions route their ``gh_json`` calls through this facade via
# ``_facade.gh_json(...)``.  ``evaluate_pr_mergeability`` calls
# ``evaluate_pr_check_health`` by bare name (direct, intra-module call) --
# both live in merge_eval.py after the extraction, mirroring the original
# monolith's self-call.  This direct call is correct: it preserves the
# pre-extraction resolution behaviour (the function invoked is the one
# defined alongside the caller).
from bluei.engine.gh.merge_eval import (  # noqa: F401
    evaluate_pr_check_health,
    evaluate_pr_mergeability,
    evaluate_pr_reviews,
    fetch_open_prs_for_merge,
)

# Step 7: PR regression audit moved to bluei.engine.gh.pr_regression.
# Module name is ``pr_regression`` (NOT ``regression``) -- the latter would
# collide with the top-level ``bluei.engine.regression`` that this function
# lazy-imports.  All three gh-internal calls (``gh_json``, ``run_capture``,
# ``evaluate_pr_check_health``) route through this facade via ``_facade.X(...)``.
# The ``evaluate_pr_check_health`` indirection matters here: that helper moved
# into merge_eval.py in Step 6, so a bare-name call would ``NameError``;
# resolving via ``_facade`` preserves the patch surface AND keeps the cross-
# module reference correct.
from bluei.engine.gh.pr_regression import evaluate_pr_regression  # noqa: F401

logger = logging.getLogger(__name__)


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
