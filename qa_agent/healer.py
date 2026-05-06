#!/usr/bin/env python3
"""Transient artifact self-heal module.

Provides automatic .gitignore healing and cleanup for common generated
artifacts (coverage reports, test outputs, cache dirs) that cause dirty
worktrees but are safe to ignore.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from typing import List, Optional, Set


# Common transient artifact patterns: (pattern, is_dir)
# These are widely recognized generated directories/files that should NEVER
# be committed and are safe to auto-ignore.
TRANSIENT_ARTIFACTS: List[tuple[str, bool]] = [
    # Python
    ('__pycache__', True),
    ('*.pyc', False),
    ('*.pyo', False),
    ('*.pyd', False),
    ('.Python', False),
    ('pip-log.txt', False),
    ('pip-delete-this-dir.txt', False),
    ('.pytest_cache', True),
    ('.mypy_cache', True),
    ('.ruff_cache', True),
    ('htmlcov', True),
    ('.coverage', False),
    ('.coverage.*', False),
    ('.tox', True),
    ('.venv', True),
    ('venv', True),
    ('ENV', True),
    ('env', True),
    ('*.egg-info', False),
    ('dist', True),
    ('build', True),
    ('*.whl', False),

    # Node.js / JavaScript / TypeScript
    ('node_modules', True),
    ('.npm', True),
    ('.yarn', True),
    ('.pnpm-store', True),
    ('package-lock.json', False),  # sometimes generated
    ('.cache', True),
    ('.parcel-cache', True),
    ('.next', True),
    ('out', True),
    ('dist', True),
    ('build', True),
    ('coverage', True),
    ('.nyc_output', True),
    ('.eslintcache', False),
    ('.prettiercache', False),

    # Rust
    ('target', True),

    # Go
    ('vendor', True),

    # Java / Gradle / Maven
    ('target', True),
    ('build', True),
    ('.gradle', True),
    ('.idea', True),
    ('*.iml', False),

    # Coverage
    ('coverage', True),
    ('lcov-report', True),
    ('*.profraw', False),

    # Misc editor/OS artifacts (also transient)
    ('.DS_Store', False),
    ('Thumbs.db', False),
    ('*.swp', False),
    ('*.swo', False),
    ('.*.swp', False),
    ('*~', False),
    ('.vscode', True),
    ('.idea', True),
]


def _git_status_porcelain(repo_path: Path) -> List[str]:
    """Return list of porcelain dirty paths relative to repo root."""
    proc = subprocess.run(
        ['bash', '-lc', 'git status --porcelain'],
        cwd=str(repo_path),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return []
    lines = proc.stdout.strip().splitlines()
    # Each line: "XY filename" where XY is status, filename is the path
    return [line[3:].strip() for line in lines if line.strip()]


def _git_is_dirty(repo_path: Path) -> bool:
    """Return True if the working tree has any uncommitted changes."""
    proc = subprocess.run(
        ['bash', '-lc', 'git status --porcelain'],
        cwd=str(repo_path),
        text=True,
        capture_output=True,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _transient_matcher(name: str, is_dir: bool) -> bool:
    """Return True if a path name matches a transient artifact pattern."""
    for pattern, is_path_dir in TRANSIENT_ARTIFACTS:
        if is_path_dir != is_dir:
            continue
        if fnmatch.fnmatch(name, pattern):
            return True
        # Handle "dirname/" prefix patterns for dirs
        if is_path_dir and name == pattern.rstrip('/'):
            return True
    return False


def _parse_gitignore(repo_path: Path) -> Set[str]:
    """Parse .gitignore and return the set of entries."""
    gitignore = repo_path / '.gitignore'
    if not gitignore.exists():
        return set()
    entries = set()
    for line in gitignore.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            entries.add(line)
    return entries


def _write_gitignore_entries(gitignore_path: Path, new_entries: Set[str]) -> None:
    """Append new .gitignore entries (deduplicated) to .gitignore file."""
    existing = set()
    if gitignore_path.exists():
        for line in gitignore_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                existing.add(line)

    merged = existing | new_entries
    if merged == existing:
        return  # nothing to add

    # Append new entries under a marker
    lines = []
    if gitignore_path.exists():
        content = gitignore_path.read_text()
        if content.strip():
            lines = content.rstrip('\n').splitlines()
            if lines and lines[-1].strip():
                lines.append('')

    lines.append('# QA Agent transient artifacts (auto-added)')
    for entry in sorted(new_entries - existing):
        lines.append(entry)
    lines.append('# End QA Agent transient artifacts')

    gitignore_path.write_text('\n'.join(lines) + '\n')


class TransientArtifactHealer:
    """Heals dirty worktrees caused by transient generated artifacts.

    This class provides safe, reversible remediation for dirty worktrees
    by adding transient artifact patterns to .gitignore and optionally
    removing the artifacts themselves.

    Auto-healing is only applied when:
    - The dirty paths are EXCLUSIVELY transient artifacts
    - The user has not set `require_clean_worktree=False`
    - The repo is NOT in a state that would make removal dangerous
    """

    # Marker used in .gitignore to identify auto-added entries
    MARKER = '# QA Agent transient artifacts (auto-added)'

    def __init__(self, repo_path: Path):
        self.repo_path = Path(repo_path).resolve()

    def is_dirty(self) -> bool:
        """Return True if the working tree has any uncommitted changes."""
        return _git_is_dirty(self.repo_path)

    def get_dirty_paths(self) -> List[str]:
        """Return list of uncommitted file/dir paths relative to repo root."""
        return _git_status_porcelain(self.repo_path)

    def get_transient_dirty_paths(self) -> List[str]:
        """Return dirty paths that are known transient artifacts."""
        dirty = self.get_dirty_paths()
        transient: List[str] = []

        for path in dirty:
            name = Path(path).name
            # Check if the basename matches any transient pattern
            if _transient_matcher(name, True) or _transient_matcher(name, False):
                transient.append(path)
                continue
            # Also check the whole path for dir patterns like "coverage/report.html"
            # Strip to just the top-level name for dir patterns
            parts = Path(path).parts
            if parts:
                top = parts[0]
                if _transient_matcher(top, True) or _transient_matcher(top, False):
                    transient.append(path)
                    continue

        return transient

    def is_exclusively_transient_dirty(self) -> bool:
        """Return True if dirty paths are ONLY transient artifacts."""
        dirty = self.get_dirty_paths()
        if not dirty:
            return False
        transient = set(self.get_transient_dirty_paths())
        return len(transient) == len(dirty) and transient == set(dirty)

    def get_missing_gitignore_entries(self) -> set[str]:
        """Return transient patterns that exist in the repo but are NOT in .gitignore.

        Checks the actual filesystem for transient artifact paths and compares
        them against .gitignore entries. Unlike get_transient_dirty_paths(), this
        does NOT depend on git status and works even after artifacts have been
        removed (or before they are created).
        """
        gitignore = _parse_gitignore(self.repo_path)
        missing: set[str] = set()

        # Scan the repo for transient artifact paths
        # For directories: check directory names at top level
        # For files: check file names at all levels
        try:
            for entry in self.repo_path.iterdir():
                name = entry.name
                is_dir = entry.is_dir()

                # Check against known transient patterns
                for pattern, is_path_dir in TRANSIENT_ARTIFACTS:
                    if is_path_dir != is_dir:
                        continue
                    if fnmatch.fnmatch(name, pattern) or (is_path_dir and name == pattern.rstrip('/')):
                        if pattern not in gitignore:
                            missing.add(pattern)
                        break  # found a match for this entry
        except OSError:
            pass

        return missing

    def heal_gitignore(self, dry_run: bool = True) -> tuple[bool, set[str]]:
        """Add missing transient artifact patterns to .gitignore.

        Args:
            dry_run: If True, compute but don't write anything.

        Returns:
            (changed, missing_entries): changed=True if .gitignore was modified.
        """
        missing = self.get_missing_gitignore_entries()
        if not missing or dry_run:
            return bool(missing) if dry_run else False, missing

        gitignore_path = self.repo_path / '.gitignore'
        _write_gitignore_entries(gitignore_path, missing)
        return True, missing

    def heal_remove_artifacts(self, dry_run: bool = True) -> tuple[bool, List[str]]:
        """Remove transient artifact directories/files from working tree.

        This is a destructive operation but ONLY removes paths that are
        already covered by .gitignore or will be added to .gitignore.
        Commits/stashes are NOT modified.

        Args:
            dry_run: If True, compute but don't remove anything.

        Returns:
            (changed, removed_paths): changed=True if any paths were removed.
        """
        transient = self.get_transient_dirty_paths()
        if not transient:
            return False, []

        removed: List[str] = []
        for path_str in transient:
            path = self.repo_path / path_str
            if not path.exists():
                continue
            if dry_run:
                removed.append(path_str)
            else:
                try:
                    if path.is_dir():
                        import shutil
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                    removed.append(path_str)
                except OSError:
                    pass

        return bool(removed) if dry_run else bool(removed), removed

    def heal(self, remove_artifacts: bool = False, dry_run: bool = True) -> dict:
        """Full self-heal: update .gitignore and optionally remove artifacts.

        Args:
            remove_artifacts: Also delete the transient directories/files.
            dry_run: If True, compute but don't make changes.

        Returns:
            dict with keys: gitignore_changed, gitignore_entries_added,
                           artifacts_changed, artifacts_removed,
                           is_exclusively_transient, dry_run
        """
        # Capture state BEFORE any modifications so we know the true pre-heal condition
        pre_heal_exclusively_transient = self.is_exclusively_transient_dirty()
        pre_heal_dirty_paths = self.get_dirty_paths()
        pre_heal_transient_paths = self.get_transient_dirty_paths()

        # CRITICAL: compute missing .gitignore entries BEFORE removing artifacts,
        # because get_missing_gitignore_entries() scans the filesystem and would
        # find nothing after artifacts are removed.
        pre_heal_missing_entries = self.get_missing_gitignore_entries()

        # Remove artifacts first (before writing .gitignore, which would change git state)
        artifacts_changed = False
        artifacts_removed: List[str] = []
        if remove_artifacts:
            artifacts_changed, artifacts_removed = self.heal_remove_artifacts(dry_run=dry_run)

        # Then update .gitignore using the captured pre-heal entries
        gitignore_changed = False
        gitignore_entries: List[str] = []
        if pre_heal_missing_entries:
            if not dry_run:
                gitignore_path = self.repo_path / '.gitignore'
                _write_gitignore_entries(gitignore_path, pre_heal_missing_entries)
            gitignore_changed = True
            gitignore_entries = list(pre_heal_missing_entries)

        return {
            'gitignore_changed': gitignore_changed,
            'gitignore_entries_added': sorted(gitignore_entries),
            'artifacts_changed': artifacts_changed,
            'artifacts_removed': artifacts_removed,
            'is_exclusively_transient': pre_heal_exclusively_transient,
            'dirty_paths': pre_heal_dirty_paths,
            'transient_dirty_paths': pre_heal_transient_paths,
            'dry_run': dry_run,
        }

    def safe_to_autoheal(self, require_clean_worktree: bool = True) -> tuple[bool, str]:
        """Determine if auto-healing should be applied silently.

        Args:
            require_clean_worktree: The repo's safety setting.

        Returns:
            (safe, reason): safe=True means silent auto-heal is appropriate.
        """
        if not require_clean_worktree:
            return False, 'require_clean_worktree is False; auto-heal skipped'

        if not self.is_dirty():
            return True, 'working tree is already clean'

        if not self.is_exclusively_transient_dirty():
            return False, 'dirty paths include non-transient files; manual review required'

        return True, 'dirty paths are exclusively transient artifacts; safe to auto-heal'
