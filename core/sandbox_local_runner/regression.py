"""regression.py — Detect regressions before merging a PR.

Compares the PR branch against main to catch:
- Deleted test files or test functions
- Removed public exports / API surface changes
- New lint violations introduced by the PR

Designed to hook into merge-cycle after mergeability checks pass
but before merge_pr() is called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .utils import run_capture


def _git_diff_names(repo_path: Path, base: str, head: str) -> List[Dict[str, str]]:
    """Run git diff --name-status between base and head.

    Returns list of {status, path} dicts.
    """
    rc, out = run_capture(
        ['git', 'diff', '--name-status', f'{base}...{head}'],
        cwd=str(repo_path),
    )
    if rc != 0 or not out.strip():
        return []

    changes: List[Dict[str, str]] = []
    for line in out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split('\t', 1)
        if len(parts) == 2:
            changes.append({'status': parts[0], 'path': parts[1]})
    return changes


def _find_test_deletions(
    changes: List[Dict[str, str]],
    repo_path: Path,
    base: str,
) -> List[Dict[str, Any]]:
    """Find deleted test files and estimate test function loss.

    Note: A rename (D + A of matching content on different paths) will
    fire a false positive for the D side.  The A side (new path) is not
    flagged.  A future enhancement could pair D+A entries by content
    hash to suppress rename false positives.
    """
    deletions: List[Dict[str, Any]] = []
    test_prefixes = ('tests/', 'test_', 'spec/', '__tests__/')

    for change in changes:
        if change['status'] != 'D':
            continue
        fpath = change['path']
        if any(fpath.startswith(p) for p in test_prefixes):
            # Estimate test functions from the base-branch version
            rc, content = run_capture(
                ['git', 'show', f'{base}:{fpath}'],
                cwd=str(repo_path),
            )
            func_count = 0
            if rc == 0 and content:
                func_count = len([l for l in content.splitlines()
                                  if l.strip().startswith(('def test_', 'async def test_',
                                                            'it(', 'describe(', 'test('))])
            deletions.append({
                'type': 'test_file_deleted',
                'path': fpath,
                'estimated_functions': func_count,
            })

    return deletions


def _find_export_changes(
    changes: List[Dict[str, str]],
    repo_path: Path,
) -> List[Dict[str, Any]]:
    """Find removed public exports from __init__.py or export modules."""
    exports: List[Dict[str, Any]] = []

    for change in changes:
        if change['status'] != 'D':
            continue
        fpath = change['path']
        # __init__.py deletions — strong signal of export removal
        if fpath.endswith('/__init__.py') or fpath == '__init__.py':
            exports.append({
                'type': 'module_init_deleted',
                'path': fpath,
            })
        # Direct export modules
        if '/exports/' in fpath or fpath.endswith('/index.ts') or fpath.endswith('/index.js'):
            exports.append({
                'type': 'export_module_deleted',
                'path': fpath,
            })

    return exports


def _find_lint_regressions(
    repo_path: Path,
    base: str,
    head: str,
) -> List[Dict[str, Any]]:
    """Compare lint findings between base and head branches.

    Checks if the PR branch introduces NEW lint violations that don't
    exist on main. Only flags regressions (new violations), not existing ones.

    Works with ruff (Python) — other linters can be added.
    """
    regressions: List[Dict[str, Any]] = []

    # Check for ruff
    ruff_config = repo_path / 'ruff.toml'
    pyproject = repo_path / 'pyproject.toml'

    if not (ruff_config.exists() or (pyproject.exists() and '[tool.ruff]' in pyproject.read_text())):
        return regressions  # Not a ruff project

    # Run ruff on the PR branch diff only
    rc, out = run_capture(
        ['git', 'diff', f'{base}...{head}', '--', '*.py'],
        cwd=str(repo_path),
    )
    if rc != 0 or not out.strip():
        return regressions

    # Run ruff check on the diffed files only
    # Collect files that changed
    changed_py_files: List[str] = []
    for line in out.strip().splitlines():
        if line.startswith('+++ b/'):
            fpath = line[6:]
            if fpath.endswith('.py'):
                changed_py_files.append(fpath)

    if not changed_py_files:
        return regressions

    # Run ruff check ONLY on changed files, filtering to new violations
    rc, out = run_capture(
        ['ruff', 'check', '--select', 'ALL', '--output-format', 'concise', '--quiet'] + changed_py_files,
        cwd=str(repo_path),
    )
    if rc != 0 and out.strip():
        for line in out.strip().splitlines():
            parts = line.split(':', 3)
            if len(parts) >= 3:
                regressions.append({
                    'type': 'lint_regression',
                    'file': parts[0],
                    'line': int(parts[1]) if parts[1].isdigit() else 0,
                    'rule': parts[2].strip() if len(parts) >= 3 else '',
                    'message': parts[3].strip() if len(parts) >= 4 else '',
                })

    return regressions


def check_regressions(
    repo_path: Path,
    base_branch: str,
    head_branch: str,
    log_file: Path,
) -> List[Dict[str, Any]]:
    """Run regression checks comparing PR branch against base.

    Args:
        repo_path: Local repository path.
        base_branch: Base branch (e.g. 'main', 'origin/main').
        head_branch: PR branch to check.
        log_file: Path to the run log.

    Returns:
        List of regression findings. Empty list = clean.
    """
    from .state import _append_text

    findings: List[Dict[str, Any]] = []

    _append_text(log_file, f'regression-check: comparing {head_branch} against {base_branch}')

    # Get diff
    changes = _git_diff_names(repo_path, base_branch, head_branch)

    if not changes:
        _append_text(log_file, 'regression-check: no diff to compare')
        return findings

    # 1. Test deletions
    test_dels = _find_test_deletions(changes, repo_path, base_branch)
    findings.extend(test_dels)

    # 2. Export changes
    export_changes = _find_export_changes(changes, repo_path)
    findings.extend(export_changes)

    # 3. Lint regressions
    lint_regs = _find_lint_regressions(repo_path, base_branch, head_branch)
    findings.extend(lint_regs)

    if findings:
        _append_text(
            log_file,
            f'regression-check: {len(findings)} finding(s) — '
            f'test_dels={len(test_dels)} '
            f'export_changes={len(export_changes)} '
            f'lint_regressions={len(lint_regs)}',
        )
        for f in findings:
            _append_text(
                log_file,
                f'  regression: [{f["type"]}] {f.get("path", f.get("file", "?"))}',
            )
    else:
        _append_text(log_file, 'regression-check: clean — no regressions detected')

    return findings
