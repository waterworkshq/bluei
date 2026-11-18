"""Runtime safety gates for protected-branch and safety-mode enforcement.

These pure functions check whether a git/GitHub mutating operation is allowed
given the current safety configuration. They are invoked before:

- ``git push`` — to block direct pushes to protected branches (main/master)
- ``gh pr create`` — to honour the configured safety mode
- ``gh pr merge`` — to honour the configured safety mode

Fail-closed contract
--------------------
Every gate FAILS CLOSED: a missing, empty (``None`` / ``{}``), or invalid
runtime policy — including an absent, non-string, or unrecognized ``mode`` —
BLOCKS the operation. Non-string modes are rejected outright (never coerced
via ``str()``). Restrictive modes block both ``git push`` (including pushes
that would update an existing PR's head branch) and PR creation; only an
explicitly permissive mode allows the operation.

The mode is normalized through :meth:`SafetyMode.from_brand`, so canonical
(``observe``, ``issue-only``, ``pr``, ``merge``), brand (``watch-only``,
``note only``, ``offer-fixes``, ``full care``, ...), and underscore
(``issue_only``) spellings of the same care level behave identically.

The expected shape of ``safety_config`` mirrors ``RepoConfig.safety``::

    {
        "mode": "observe" | "issue-only" | "pr" | "merge",
        "protected_branches": ["main", "master"],
        "block_push": false,
        ...
    }

``repo_config`` (for :func:`resolve_base_branch`) mirrors the flat
``RepoConfig`` dict and may carry an explicit ``base_branch`` /
``default_branch`` override. It is accepted in BOTH shapes: a plain dict
(the historical/test shape) and a ``RepoConfig`` dataclass (what the cycle
actually stores on ``RunContext.repo_config`` — D7-C-1: the dataclass has
no ``.get`` and previously crashed ``resolve_base_branch`` with an
AttributeError at first live PR creation).
"""

from typing import Any, Dict, List, Optional, Tuple

from bluei.common.models import SafetyMode


def _cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict-shaped OR attribute-shaped config object.

    Production callers pass a ``RepoConfig`` dataclass (attribute access);
    tests and older call sites pass plain dicts (``.get``). Both carry the
    same field names, so both shapes are accepted here. A missing key on
    either shape yields ``default``.
    """
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)

# Canonical fallback when nothing else is configured. This preserves the
# pre-enforcement behaviour where ``--base main`` was hardcoded.
DEFAULT_BASE_BRANCH = "main"

# Safety mode values are normalized to the ``SafetyMode`` enum (see
# ``_resolve_mode``); care levels gate pushes/PRs/merges below.


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


def _resolve_mode(
    safety_config: Optional[Dict[str, Any]],
) -> Tuple[Optional[SafetyMode], str]:
    """Normalize the configured care mode to a :class:`SafetyMode`.

    Reuses ``SafetyMode.from_brand`` so canonical (``pr``), brand
    (``offer fixes``), and underscore (``issue_only``) spellings of the same
    care level resolve identically (underscores map to hyphens first).

    Returns ``(mode, denial_reason)``. ``mode`` is ``None`` when the policy
    is missing/empty or the mode is absent, non-string (never ``str()``
    coerced — a list/int/bool mode is invalid), or unrecognized; the
    accompanying ``denial_reason`` explains the failure and callers MUST
    fail closed on it. ``denial_reason`` is empty when a mode resolved.
    """
    if not isinstance(safety_config, dict) or not safety_config:
        return None, "no safety policy provided (missing/empty) — fail closed"
    raw = safety_config.get("mode")
    if not isinstance(raw, str) or not raw.strip():
        return None, (
            f"safety mode missing or non-string ({type(raw).__name__}) — fail closed"
        )
    normalized = raw.strip().lower().replace("_", "-")
    try:
        return SafetyMode.from_brand(normalized), ""
    except ValueError:
        return None, f"unrecognized safety mode '{raw.strip()}' — fail closed"


def check_push_allowed(
    branch: str, safety_config: Optional[Dict[str, Any]]
) -> Tuple[bool, str]:
    """Gate for ``git push``.

    Fails closed: a missing/empty/invalid policy (including an absent,
    non-string, or unrecognized mode) blocks the push. With a valid policy:

    - ``block_push``      → blocked (registration lookup failed closed)
    - ``observe``         → blocked ("observe mode forbids push")
    - ``issue-only``      → blocked ("issue-only mode forbids push")
    - protected branch    → blocked (main/master by default)

    bluei must NEVER push directly to a protected branch — only feature
    branches that become PR heads. Restrictive modes additionally block
    pushes to feature branches, which covers pushes that would update an
    existing PR's head branch.

    Returns:
        ``(allowed, reason)``. ``reason`` is safe to surface in logs.
    """
    mode, denial = _resolve_mode(safety_config)
    if mode is None:
        return False, denial
    if safety_config.get("block_push"):
        return False, "fail-closed safety policy forbids push"
    if mode is SafetyMode.OBSERVE:
        return False, "observe mode forbids push"
    if mode is SafetyMode.ISSUE_ONLY:
        return False, "issue-only mode forbids push"
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
    """Gate for ``gh pr create`` (and updates to existing PRs).

    Honours the configured safety mode — canonical, brand, and underscore
    spellings behave identically via ``SafetyMode.from_brand``:

    - ``observe``/``watch-only``            → blocked ("observe mode forbids PR creation")
    - ``issue-only``/``issue_only``/``note-only`` → blocked ("issue-only mode forbids PR creation")
    - ``pr``/``offer-fixes``/``merge``/``full-care`` → allowed

    Fails closed: a missing/empty/invalid policy (including an absent,
    non-string, or unrecognized mode) blocks PR creation.

    ``base_branch`` is accepted for symmetry and future checks (e.g. ensuring
    the PR targets a known protected branch) but does not itself gate
    creation — PRs are *expected* to target protected branches.

    Returns:
        ``(allowed, reason)``. ``reason`` is safe to surface in logs.
    """
    mode, denial = _resolve_mode(safety_config)
    if mode is None:
        return False, denial
    if mode is SafetyMode.OBSERVE:
        return False, "observe mode forbids PR creation"
    if mode is SafetyMode.ISSUE_ONLY:
        return False, "issue-only mode forbids PR creation"
    return True, "ok"  # SafetyMode.PR / SafetyMode.MERGE


def check_merge_allowed(
    safety_config: Optional[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Gate for ``gh pr merge``.

    Honours the configured safety mode — canonical, brand, and underscore
    spellings behave identically via ``SafetyMode.from_brand``:

    - ``observe``            → blocked ("observe mode forbids merge")
    - ``issue-only``         → blocked ("issue-only mode forbids merge")
    - ``pr``                 → blocked ("pr mode forbids auto-merge — use merge mode")
    - ``merge``/``full-care`` → allowed

    Fails closed: a missing/empty/invalid policy (including an absent,
    non-string, or unrecognized mode) blocks the merge — higher stakes than
    PR creation.

    Returns:
        ``(allowed, reason)``. ``reason`` is safe to surface in logs.
    """
    mode, denial = _resolve_mode(safety_config)
    if mode is None:
        return False, denial
    if mode is SafetyMode.OBSERVE:
        return False, "observe mode forbids merge"
    if mode is SafetyMode.ISSUE_ONLY:
        return False, "issue-only mode forbids merge"
    if mode is SafetyMode.PR:
        return False, "pr mode forbids auto-merge — use merge mode"
    return True, "ok"  # SafetyMode.MERGE


def resolve_base_branch(
    safety_config: Optional[Dict[str, Any]],
    repo_config: Any = None,
) -> str:
    """Determine the PR base branch without ever hardcoding ``"main"``.

    Resolution order:

    1. ``repo_config['base_branch']``     — explicit override
    2. ``repo_config['default_branch']``  — explicit override
    3. ``safety_config['protected_branches'][0]`` — typically ``"main"``
    4. ``DEFAULT_BASE_BRANCH`` (``"main"``) — final fallback for backward compat

    ``repo_config`` may be a plain dict OR a ``RepoConfig`` dataclass
    (D7-C-1: the cycle stores the dataclass on ``RunContext.repo_config``;
    the dataclass carries no ``base_branch``/``default_branch`` fields, so
    it falls through to the safety/protected-branches order above instead
    of raising AttributeError).

    Returns:
        The resolved base branch name (trimmed, non-empty).
    """
    if repo_config:
        for key in ("base_branch", "default_branch"):
            val = _cfg_get(repo_config, key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    if safety_config:
        protected = safety_config.get("protected_branches")
        if isinstance(protected, list) and protected:
            first = protected[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
    return DEFAULT_BASE_BRANCH
