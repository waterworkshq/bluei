"""Issue lifecycle ops for ``bluei.engine.gh``.

Extracted in Step 4 of the god-module decomposition (see
``docs/plans/god-module-decomp/gh.md``).  Five functions covering the
GitHub issue lifecycle: dedup lookup, comment/close, finding
reconstruction from a persisted record, and the create-or-update hub.

Patch-surface note (plan Risk #1, section 4.1): every call to a gh-internal
helper (``gh_json``, ``run_capture``, ``finding_dedupe_marker``,
``find_existing_github_issue``, ``gh_issue_comment``,
``parse_issue_number_from_url``) is routed through the ``_facade`` reference
(the parent package) rather than imported into this module's globals.  This
preserves ``patch("bluei.engine.gh.<name>")`` reaching the call sites here --
the patch replaces the attribute on the facade module and ``_facade.<name>``
looks it up at call time.  Pattern mirrors :mod:`bluei.engine.gh._core` and
:mod:`bluei.engine.gh.repo`.

Non-gh imports stay at their canonical locations: ``DETECTOR_CATALOG`` from
``bluei.engine.constants``, ``Finding`` from ``bluei.engine.models``, and the
lazy function-local imports of ``now_iso`` / ``_append_text`` are preserved
verbatim from the original module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from bluei.engine.constants import DETECTOR_CATALOG
from bluei.engine.models import Finding

# Call-time resolution of gh-internal helpers through the facade.  See
# bluei.engine.gh._core for the same pattern and its rationale.
import bluei.engine.gh as _facade


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
    marker = _facade.finding_dedupe_marker(finding_id)
    payload = _facade.gh_json(
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


def gh_issue_comment(repo_slug: str, issue_number: int, body: str, cwd: Path) -> bool:
    rc, _ = _facade.run_capture(
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
    rc, _ = _facade.run_capture(
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

    marker = _facade.finding_dedupe_marker(finding.finding_id)
    existing = _facade.find_existing_github_issue(
        repo_slug, finding.finding_id, cwd=cwd
    )
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
            _facade.gh_issue_comment(repo_slug, number, sync_note, cwd=cwd)
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

    rc, out = _facade.run_capture(
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
    number = _facade.parse_issue_number_from_url(url)
    if number is None:
        existing_after_create = _facade.find_existing_github_issue(
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
