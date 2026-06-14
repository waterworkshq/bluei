"""Runtime safety gates for protected-branch and safety-mode enforcement.

These pure functions check whether a git/GitHub mutating operation is allowed
given the current safety configuration. They are invoked before:

- ``git push`` — to block direct pushes to protected branches (main/master)
- ``gh pr create`` — to honour the configured safety mode
- ``gh pr merge`` — to honour the configured safety mode

Backward-compatible contract
----------------------------
Every gate treats a falsy ``safety_config`` (``None`` or ``{}``) as
"no policy provided" and ALLOWS the operation. Enforcement only activates
when a non-empty safety config dict is explicitly supplied. This means
existing callers and tests that do not pass ``safety_config`` are unaffected.

The expected shape of ``safety_config`` mirrors ``RepoConfig.safety``::

    {
        "mode": "observe" | "issue-only" | "pr" | "merge",
        "protected_branches": ["main", "master"],
        ...
    }

``repo_config`` (for :func:`resolve_base_branch`) mirrors the flat
``RepoConfig`` dict and may carry an explicit ``base_branch`` /
``default_branch`` override.
"""

from typing import Any, Dict, List, Optional, Tuple

# Canonical fallback when nothing else is configured. This preserves the
# pre-enforcement behaviour where ``--base main`` was hardcoded.
DEFAULT_BASE_BRANCH = "main"

# Safety mode values (hyphen-form is canonical per ``SafetyMode`` enum, but we
# accept underscore-form too for robustness against normalisation differences).
_BLOCKING_PR_MODES = ("observe",)
_BLOCKING_PR_MODES_ISSUE = ("issue-only", "issue_only")
_BLOCKING_MERGE_MODES = ("observe", "issue-only", "issue_only", "pr")


def is_protected_branch(branch: str, protected_branches: List[str]) -> bool:
    """Return True if ``branch`` appears in ``protected_branches``.

    Comparison is case-insensitive and whitespace-trimmed so that minor config
    formatting differences do not create accidental bypasses. An empty/None
    branch or empty list never matches.
    """
    if not branch or not protected_branches:
        return False
    needle = branch.strip().lower()
    haystack = {str(b).strip().lower() for b in protected_branches if b}
    return needle in haystack


def _get_protected_branches(safety_config: Optional[Dict[str, Any]]) -> List[str]:
    """Extract ``protected_branches`` from ``safety_config``.

    Falls back to ``["main", "master"]`` when absent so that push protection
    is meaningful even with a minimal config.
    """
    if not safety_config:
        return ["main", "master"]
    branches = safety_config.get("protected_branches")
    if branches and isinstance(branches, list):
        return [str(b) for b in branches if b]
    return ["main", "master"]


def _normalise_mode(safety_config: Optional[Dict[str, Any]]) -> str:
    """Return the lowercased, stripped safety mode (empty string if absent)."""
    if not safety_config:
        return ""
    return str(safety_config.get("mode", "")).strip().lower()


def check_push_allowed(
    branch: str, safety_config: Optional[Dict[str, Any]]
) -> Tuple[bool, str]:
    """Gate for ``git push``.

    bluei must NEVER push directly to a protected branch (main/master by
    default) — it only pushes feature branches that become PR heads. Pushing
    to a protected branch would bypass the review workflow.

    Returns:
        ``(allowed, reason)``. ``reason`` is safe to surface in logs. When
        ``safety_config`` is falsy the operation is always allowed
        (``"no-safety-config"``).
    """
    if not safety_config:
        return True, "no-safety-config"
    protected = _get_protected_branches(safety_config)
    if is_protected_branch(branch, protected):
        return (
            False,
            f"branch '{branch}' is protected (protected_branches={protected})",
        )
    return True, "ok"


def check_pr_creation_allowed(
    base_branch: str, safety_config: Optional[Dict[str, Any]]
) -> Tuple[bool, str]:
    """Gate for ``gh pr create``.

    Honours the configured safety mode:

    - ``observe``     → blocked ("observe mode forbids PR creation")
    - ``issue-only``  → blocked ("issue-only mode forbids PR creation")
    - ``pr``/``merge`` → allowed

    ``base_branch`` is accepted for symmetry and future checks (e.g. ensuring
    the PR targets a known protected branch) but does not itself gate
    creation — PRs are *expected* to target protected branches.

    Returns:
        ``(allowed, reason)``. Falsy ``safety_config`` always allows.
    """
    if not safety_config:
        return True, "no-safety-config"
    mode = _normalise_mode(safety_config)
    if mode in _BLOCKING_PR_MODES:
        return False, "observe mode forbids PR creation"
    if mode in _BLOCKING_PR_MODES_ISSUE:
        return False, "issue-only mode forbids PR creation"
    if mode in ("pr", "merge"):
        return True, "ok"
    # Unknown / empty mode: allow but surface in reason for traceability.
    return True, f"ok (unrecognized mode '{mode}' allowed by default)"


def check_merge_allowed(
    safety_config: Optional[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Gate for ``gh pr merge``.

    Honours the configured safety mode:

    - ``observe``     → blocked ("observe mode forbids merge")
    - ``issue-only``  → blocked ("issue-only mode forbids merge")
    - ``pr``          → blocked ("pr mode forbids auto-merge — use merge mode")
    - ``merge``       → allowed

    Returns:
        ``(allowed, reason)``. Falsy ``safety_config`` always allows.
    """
    if not safety_config:
        return True, "no-safety-config"
    mode = _normalise_mode(safety_config)
    if mode in _BLOCKING_PR_MODES:
        return False, "observe mode forbids merge"
    if mode in _BLOCKING_PR_MODES_ISSUE:
        return False, "issue-only mode forbids merge"
    if mode == "pr":
        return False, "pr mode forbids auto-merge — use merge mode"
    if mode == "merge":
        return True, "ok"
    # Unknown mode: fail closed for merges (higher stakes than PR creation).
    return False, f"unrecognized safety mode '{mode}' forbids merge"


def resolve_base_branch(
    safety_config: Optional[Dict[str, Any]],
    repo_config: Optional[Dict[str, Any]],
) -> str:
    """Determine the PR base branch without ever hardcoding ``"main"``.

    Resolution order:

    1. ``repo_config['base_branch']``     — explicit override
    2. ``repo_config['default_branch']``  — explicit override
    3. ``safety_config['protected_branches'][0]`` — typically ``"main"``
    4. ``DEFAULT_BASE_BRANCH`` (``"main"``) — final fallback for backward compat

    Returns:
        The resolved base branch name (trimmed, non-empty).
    """
    if repo_config:
        for key in ("base_branch", "default_branch"):
            val = repo_config.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    if safety_config:
        protected = safety_config.get("protected_branches")
        if isinstance(protected, list) and protected:
            first = protected[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
    return DEFAULT_BASE_BRANCH
