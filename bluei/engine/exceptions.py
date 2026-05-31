"""Bluei exception hierarchy.

Provides a minimal typed exception tree so callers can distinguish
failure modes without string-matching on error messages.
"""


class BlueiError(Exception):
    """Base exception for all bluei-originated failures."""


class BlueiConfigError(BlueiError):
    """Configuration is missing, invalid, or inaccessible."""


class BlueiGitError(BlueiError):
    """A git operation failed (clone, worktree, branch, commit, push)."""


class BlueiGitHubError(BlueiError):
    """A GitHub API call failed (rate limit, auth, network, 5xx)."""


class BlueiFixError(BlueiError):
    """A fix engine attempt failed (autofix no-op, cascade exhausted, LLM error)."""


class BlueiDiscoveryError(BlueiError):
    """Finding discovery could not complete (linter crash, scan timeout)."""
