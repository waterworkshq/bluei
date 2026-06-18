"""Sandbox & self-merge policy for ``bluei.engine.gh``.

Extracted in Step 3 of the god-module decomposition (see
``docs/plans/god-module-decomp/gh.md``).  Two functions that decide whether
a given repo slug should be treated as a sandbox (auto-mergeable) or as a
self-merge repo (merged into its own trunk).

CRITICAL (plan Risk #2, section 4.2): ``_load_self_merge_repos_from_global_config``
computes the workspace root from ``Path(__file__).resolve().parents[N]``.
When the function lived in ``bluei/engine/gh.py`` (depth: 2 dir levels from
the repo root -- ``gh.py`` -> ``engine/`` -> ``bluei/`` -> repo root), the
correct index was ``parents[2]``.

After the move into ``bluei/engine/gh/sandbox.py`` the file is ONE LEVEL
DEEPER (``sandbox.py`` -> ``gh/`` -> ``engine/`` -> ``bluei/`` -> repo root),
so the index MUST be ``parents[3]``.  Leaving it at ``parents[2]`` would
silently resolve to ``bluei/`` instead of the workspace root, and
``repo_is_sandbox`` would never see ``config.yaml`` -- mis-classifying
self-merge repos as regular ones.

The scaffolding test ``test_workspace_root_resolution`` and the new
``test_workspace_root_resolution_still_correct`` guard this invariant.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import yaml


def _load_self_merge_repos_from_global_config() -> List[str]:
    """Read ``github.self_merge_repos`` from the workspace ``config.yaml``
    without going through the app layer.

    Mirrors the path resolution of ``bluei.app.config.load_global_config``
    (``QA_AGENT_WORKSPACE`` env var or repo root) but reads only the specific
    key we need. Avoids an engine->app import (rec-08).
    """
    workspace = Path(
        os.environ.get("QA_AGENT_WORKSPACE", Path(__file__).resolve().parents[3])
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
