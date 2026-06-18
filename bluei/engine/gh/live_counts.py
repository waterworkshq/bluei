"""GitHub live counts (open issues / open PRs) for ``bluei.engine.gh``.

Extracted in Step 8 of the god-module decomposition (see
``docs/plans/god-module-decomp/gh.md``).  One read-only function that fires
a raw GraphQL query through the ``gh`` CLI to fetch the live open-issue and
open-PR counts for a repo.  Used by health/reporting code that wants the
realtime numbers rather than the locally-cached scan state.

Patch-surface note (plan Risk #1, section 4.1): every call to a gh-internal
helper is routed through the ``_facade`` reference (the parent package)
rather than imported into this module's globals:

* ``get_origin_url``     -> ``_facade.get_origin_url(...)``
* ``parse_github_repo``  -> ``_facade.parse_github_repo(...)``
* ``run_capture``        -> ``_facade.run_capture(...)``

The first two moved into :mod:`bluei.engine.gh.repo` in Step 2; routing
them through ``_facade`` preserves ``patch("bluei.engine.gh.<name>")``
reaching the call sites here.  Pattern mirrors :mod:`bluei.engine.gh._core`,
:mod:`bluei.engine.gh.repo`, :mod:`bluei.engine.gh.sandbox`,
:mod:`bluei.engine.gh.issue_ops`, :mod:`bluei.engine.gh.pr_ops`,
:mod:`bluei.engine.gh.merge_eval`, and :mod:`bluei.engine.gh.pr_regression`.

``json.loads`` stays a bare stdlib call -- it is not part of the gh patch
surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

# Call-time resolution of gh-internal helpers through the facade.  See
# bluei.engine.gh._core for the same pattern and its rationale.
import bluei.engine.gh as _facade


def fetch_github_live_counts(repo_path: Path) -> tuple[Optional[Dict[str, int]], str]:
    """Fetch live open issue and PR counts from GitHub via GraphQL.

    Args:
        repo_path: Local repo root (used to resolve remote URL and ``gh`` cwd).

    Returns:
        ``(counts, status)`` where *counts* is ``{'open_issues': N, 'open_prs': N}``
        or ``None`` on failure, and *status* is a human-readable label.
    """
    origin_url = _facade.get_origin_url(repo_path)
    if "github.com" not in origin_url:
        return None, "non-github-origin"

    owner, name = _facade.parse_github_repo(origin_url)
    if not owner or not name:
        return None, "github-origin-parse-failed"

    query = (
        "query($owner:String!, $name:String!) { "
        "repository(owner:$owner, name:$name) { "
        "issues(states: OPEN) { totalCount } "
        "pullRequests(states: OPEN) { totalCount } "
        "} }"
    )
    rc, out = _facade.run_capture(
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
