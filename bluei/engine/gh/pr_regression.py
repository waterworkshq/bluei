"""PR regression audit for ``bluei.engine.gh``.

Extracted in Step 7 of the god-module decomposition (see
``docs/plans/god-module-decomp/gh.md``).  One large read-only evaluator that
inspects a PR's diff + local repo state to score regression risk.

Patch-surface note (plan Risk #1, section 4.1): every call to a gh-internal
helper is routed through the ``_facade`` reference (the parent package)
rather than imported into this module's globals:

* ``gh_json``              -> ``_facade.gh_json(...)``
* ``run_capture``          -> ``_facade.run_capture(...)``
* ``evaluate_pr_check_health`` -> ``_facade.evaluate_pr_check_health(...)``

This last one matters: ``evaluate_pr_check_health`` moved into
:mod:`bluei.engine.gh.merge_eval` in Step 6, so a bare-name call inside
this module would raise ``NameError``.  Routing it through ``_facade``
preserves ``patch("bluei.engine.gh.evaluate_pr_check_health")`` reaching
the call site here.  Pattern mirrors :mod:`bluei.engine.gh._core`,
:mod:`bluei.engine.gh.repo`, :mod:`bluei.engine.gh.sandbox`,
:mod:`bluei.engine.gh.issue_ops`, :mod:`bluei.engine.gh.pr_ops`, and
:mod:`bluei.engine.gh.merge_eval`.

Module-name note: this file is ``pr_regression.py`` (NOT ``regression.py``)
to avoid colliding with the existing top-level
:mod:`bluei.engine.regression`, which is the lazy-imported regression
engine this function calls into.

Lazy imports from :mod:`bluei.engine.regression` (``_find_test_deletions``,
``_find_export_changes``, ``_find_lint_regressions``, ``_git_diff_names``,
``compute_regression_score``) are preserved verbatim from the original
monolith -- including the ones that are currently dead -- so behaviour
around import-time failures and name-resolution order is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

# Call-time resolution of gh-internal helpers through the facade.  See
# bluei.engine.gh._core for the same pattern and its rationale.
import bluei.engine.gh as _facade


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
    pr_info_payload = _facade.gh_json(
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
    rc, diff_out = _facade.run_capture(
        ["gh", "pr", "diff", str(pr_number), "--repo", repo_slug],
        cwd=cwd,
    )
    has_diff = rc == 0 and bool(diff_out.strip())

    # 3. Fetch status checks for context
    # evaluate_pr_check_health moved to merge_eval.py in Step 6 — resolve
    # through the facade so patch("bluei.engine.gh.evaluate_pr_check_health")
    # continues to reach this call site.
    check_health = _facade.evaluate_pr_check_health(repo_slug, pr_number, cwd)

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
