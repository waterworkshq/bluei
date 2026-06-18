"""Repo identity & slug helpers for ``bluei.engine.gh``.

Extracted in Step 2 of the god-module decomposition (see
``docs/plans/god-module-decomp/gh.md``).  Four pure-ish helpers that turn
git remote URLs and issue/PR web URLs into structured slugs/numbers.

Patch-surface note: :func:`get_origin_url` resolves ``run_capture`` through
the ``_facade`` reference (the parent package) rather than importing it
into this module's globals.  This is what keeps
``patch("bluei.engine.gh.run_capture")`` reaching the call site after the
move -- the patch replaces the attribute on the facade module, and
``_facade.run_capture`` looks it up at call time.  Pattern mirrors
:mod:`bluei.engine.gh._core`.

The other three helpers (``parse_github_repo``, ``parse_issue_number_from_url``,
``parse_pr_number_from_url``) are pure regex/string functions and need no
facade indirection.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# Call-time resolution of run_capture through the facade.  See
# bluei.engine.gh._core for the same pattern and its rationale.
import bluei.engine.gh as _facade


def get_origin_url(repo_path: Path) -> str:
    """Return the git remote ``origin`` URL, or empty string on failure."""
    rc, out = _facade.run_capture(["git", "remote", "get-url", "origin"], cwd=repo_path)
    if rc != 0:
        return ""
    return out.strip()


def parse_github_repo(origin_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub origin URL.

    Args:
        origin_url: A git remote URL (HTTPS or SSH).

    Returns:
        ``(owner, repo)`` tuple, or ``('', '')`` if not parseable.
    """
    normalized = origin_url.strip()
    if normalized.endswith(".git"):
        normalized = normalized[:-4]

    marker = "github.com/"
    if marker in normalized:
        slug = normalized.split(marker, 1)[1]
    elif normalized.startswith("git@github.com:"):
        slug = normalized.split(":", 1)[1]
    else:
        return "", ""

    parts = [part for part in slug.strip("/").split("/") if part]
    if len(parts) != 2:
        return "", ""
    return parts[0], parts[1]


def parse_issue_number_from_url(url: Optional[str]) -> Optional[int]:
    if not url:
        return None
    match = re.search(r"/issues/(\d+)$", url)
    return int(match.group(1)) if match else None


def parse_pr_number_from_url(url: Optional[str]) -> Optional[int]:
    if not url:
        return None
    match = re.search(r"/pull/(\d+)$", url)
    return int(match.group(1)) if match else None
