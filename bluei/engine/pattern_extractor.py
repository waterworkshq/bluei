"""Extract learned fix patterns from git diffs.

Runs ``git diff`` in a worktree, parses the unified diff into before/after
snippets, and stores them via ``FixPatternStore``.  Multi-file diffs are
delegated to the composite-pattern subsystem.  All extraction results are
appended to a log file for auditability.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

from bluei.engine.models import Finding
from bluei.engine.pattern_store import FixPattern, FixPatternStore, normalize_snippet
from bluei.engine.utils import run_capture


LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
}

VALID_SOURCES = {"autofix", "claude", "contextual"}


def extract(
    worktree_path: Path,
    finding: Finding,
    fix_source: str,
    store: FixPatternStore,
    log_file: Path,
) -> Optional[str]:
    """Capture a single-file git diff and store it as a learned fix pattern.

    Args:
        worktree_path: Root of the working tree where ``git diff`` is run.
        finding: The ``Finding`` that triggered the original fix.
        fix_source: Origin label (``autofix``, ``claude``, ``contextual``).
        store: The ``FixPatternStore`` to persist the extracted pattern into.
        log_file: Path to append extraction events to.

    Returns:
        The stored ``pattern_id``, or ``None`` if extraction was skipped.
    """
    try:
        source = fix_source if fix_source in VALID_SOURCES else str(fix_source)
        rc, diff_patch = run_capture(
            ["git", "diff"],
            cwd=Path(worktree_path),
            timeout=30,
        )
        if rc != 0:
            _append_log(
                log_file,
                f"pattern-extract-skip: rule={finding.rule} reason=git_diff_failed rc={rc}",
            )
            return None
        if not diff_patch.strip():
            _append_log(
                log_file, f"pattern-extract-skip: rule={finding.rule} reason=empty_diff"
            )
            return None

        changed_files = _changed_files(diff_patch)
        if len(changed_files) == 0:
            _append_log(
                log_file,
                f"pattern-extract-skip: rule={finding.rule} reason=no_files_in_diff",
            )
            return None

        if len(changed_files) > 1:
            comp_id = _extract_composite(
                worktree_path,
                finding,
                diff_patch,
                changed_files,
                source,
                log_file,
            )
            if comp_id:
                return comp_id
            return None

        before, after = _snippets_from_unified_diff(diff_patch)
        before_snippet = normalize_snippet(before)
        after_snippet = normalize_snippet(after)
        if not before_snippet or not after_snippet or before_snippet == after_snippet:
            _append_log(
                log_file,
                f"pattern-extract-skip: rule={finding.rule} reason=no_snippet_change",
            )
            return None

        pattern = FixPattern(
            pattern_id="",
            rule=finding.rule,
            language=infer_language(finding.path),
            file_path=finding.path,
            before_snippet=before_snippet,
            after_snippet=after_snippet,
            diff_patch=diff_patch,
            source=source,
            source_finding_ids=[finding.finding_id] if finding.finding_id else [],
            framework_constraint=_detect_framework(worktree_path),
            file_pattern=_compute_file_pattern(finding.path),
        )
        pattern_id = store.append(pattern)
        _append_log(
            log_file,
            f"pattern-extract: rule={finding.rule} pattern_id={pattern_id} source={source}",
        )
        return pattern_id
    except Exception as exc:
        try:
            _append_log(
                log_file,
                f"pattern-extract-skip: rule={finding.rule} reason=exception error={type(exc).__name__}",
            )
        except Exception:
            _logger.debug("Failed to log pattern-extract-skip")
        return None


def infer_language(file_path: str) -> str:
    """Infer a pattern language label from a file extension."""
    suffix = Path(file_path).suffix.lower()
    return LANGUAGE_BY_EXTENSION.get(suffix, suffix.lstrip(".") or "unknown")


def _changed_files(diff_patch: str) -> List[str]:
    """Parse file paths from ``diff --git`` header lines.

    Args:
        diff_patch: Full unified diff output.

    Returns:
        List of file paths with ``a/``/``b/`` prefixes stripped.
    """
    files: List[str] = []
    for line in diff_patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) >= 4:
            files.append(_strip_diff_prefix(parts[3]))
    return files


def _strip_diff_prefix(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _snippets_from_unified_diff(diff_patch: str) -> Tuple[str, str]:
    """Extract concatenated before/after source text from unified diff hunks.

    Context lines (`` `` prefix) are included in both sides.  ``---``/``+++``
    file headers and ``\\ No newline`` markers are skipped.

    Args:
        diff_patch: Unified diff output, possibly multi-hunk.

    Returns:
        Tuple of (before_text, after_text) with one joined string per side.
    """
    before_hunks: List[str] = []
    after_hunks: List[str] = []
    before_lines: List[str] = []
    after_lines: List[str] = []
    in_hunk = False

    def flush_hunk() -> None:
        if before_lines or after_lines:
            before_hunks.append("\n".join(before_lines))
            after_hunks.append("\n".join(after_lines))
            before_lines.clear()
            after_lines.clear()

    for line in diff_patch.splitlines():
        if line.startswith("@@ "):
            flush_hunk()
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("diff --git "):
            flush_hunk()
            in_hunk = False
            continue
        if line.startswith("\\ No newline at end of file"):
            continue
        if line.startswith("-") and not line.startswith("--- "):
            before_lines.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++ "):
            after_lines.append(line[1:])
        elif line.startswith(" "):
            text = line[1:]
            before_lines.append(text)
            after_lines.append(text)

    flush_hunk()
    return "\n".join(before_hunks), "\n".join(after_hunks)


def _append_log(log_file: Path, message: str) -> None:
    compact = re.sub(r"\s+", " ", message).strip()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(compact + "\n")


def _detect_framework(
    worktree_path: Path, detected_frameworks: Optional[List[str]] = None
) -> Optional[str]:
    """Return the primary framework detected in the worktree, or ``None``.

    Args:
        worktree_path: Root of the working tree to scan.
        detected_frameworks: Optional pre-detected framework list. If None,
            lazily imports ``detect_frameworks`` from ``bluei.app.onboarding``.
    """
    if detected_frameworks is not None:
        return detected_frameworks[0] if detected_frameworks else None
    try:
        from bluei.app.onboarding import detect_frameworks

        frameworks = detect_frameworks(worktree_path)
        return frameworks[0] if frameworks else None
    except Exception:
        return None


def _compute_file_pattern(file_path: str) -> str:
    """Derive a glob pattern scoped to the file's top-level directory.

    Args:
        file_path: Relative file path (e.g. ``src/utils/helpers.ts``).

    Returns:
        Glob like ``src/**/*.ts``; falls back to ``*.ts`` for shallow paths.
    """
    p = Path(file_path)
    if len(p.parts) > 1:
        return str(p.parts[0]) + "/**/*" + p.suffix
    return "*" + p.suffix


def _per_file_snippets(diff_patch: str) -> Dict[str, Tuple[str, str]]:
    """Split a multi-file diff into per-file before/after snippet pairs.

    Args:
        diff_patch: Full unified diff spanning one or more files.

    Returns:
        Mapping of file path → (before_text, after_text).
    """
    file_diffs: Dict[str, Tuple[str, str]] = {}
    current_file = None
    current_lines: List[str] = []
    for line in diff_patch.splitlines():
        if line.startswith("diff --git "):
            if current_file is not None and current_lines:
                combined = "\n".join(current_lines)
                before, after = _snippets_from_unified_diff(combined)
                file_diffs[current_file] = (before, after)
            parts = line.split()
            current_file = _strip_diff_prefix(parts[3]) if len(parts) >= 4 else None
            current_lines = [line]
            continue
        if current_file is not None:
            current_lines.append(line)
    if current_file is not None and current_lines:
        combined = "\n".join(current_lines)
        before, after = _snippets_from_unified_diff(combined)
        file_diffs[current_file] = (before, after)
    return file_diffs


def _extract_composite(
    worktree_path: Path,
    finding: Finding,
    diff_patch: str,
    changed_files: List[str],
    source: str,
    log_file: Path,
) -> Optional[str]:
    """Store a multi-file diff as a composite pattern with ordered steps.

    Args:
        worktree_path: Root of the working tree.
        finding: The ``Finding`` that triggered the fix.
        diff_patch: Full unified diff output.
        changed_files: List of file paths touched in the diff.
        source: Origin label for the pattern.
        log_file: Path to append extraction events to.

    Returns:
        The composite ``pattern_id``, or ``None`` on failure.
    """
    try:
        from bluei.engine.composite_pattern import (
            CompositePattern,
            CompositePatternStep,
            CompositePatternStore,
        )

        file_snippets = _per_file_snippets(diff_patch)
        if not file_snippets:
            return None

        steps: List[CompositePatternStep] = []
        for order, fpath in enumerate(changed_files):
            snippets = file_snippets.get(fpath)
            if snippets is None:
                continue
            before, after = snippets
            before_norm = normalize_snippet(before)
            after_norm = normalize_snippet(after)
            if not before_norm or not after_norm or before_norm == after_norm:
                continue

            if fpath == finding.path:
                scope = "finding_location"
                fp = None
            else:
                scope = "related_file"
                fp = _compute_file_pattern(fpath)

            steps.append(
                CompositePatternStep(
                    order=order,
                    description=f"fix in {fpath}",
                    match=re.escape(before_norm),
                    replacement=after_norm,
                    scope=scope,
                    file_pattern=fp,
                )
            )

        if not steps:
            return None

        language = infer_language(finding.path)
        pattern = CompositePattern(
            pattern_id="",
            rule=finding.rule,
            language=language,
            steps=steps,
            confidence=0.6,
            source=source,
        )

        store_path = Path(worktree_path) / "state" / "composite_patterns.jsonl"
        comp_store = CompositePatternStore(store_path)
        pattern_id = comp_store.append(pattern)
        _append_log(
            log_file,
            f"composite-extract: rule={finding.rule} pattern_id={pattern_id} "
            f"steps={len(steps)} files={len(changed_files)}",
        )
        return pattern_id
    except Exception as exc:
        try:
            _append_log(
                log_file,
                f"composite-extract-skip: rule={finding.rule} "
                f"reason=exception error={type(exc).__name__}",
            )
        except Exception:
            _logger.debug("Failed to log composite-extract-skip")
        return None
