"""Pattern replay engine — match findings to stored patterns and apply deterministically."""

from __future__ import annotations

import logging
import fnmatch
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

from bluei.engine.models import Finding
from bluei.engine.pattern_store import FixPattern, FixPatternStore, normalize_snippet
from bluei.engine.report import infer_language_from_path
from bluei.engine.state import append_log
from bluei.engine.utils import run_capture


AUTO_REPLAY_THRESHOLD = 0.9
PROMPT_HINT_THRESHOLD = 0.5
_PATTERN_CACHE: Dict[str, Tuple[float, Optional[FixPattern]]] = {}
_PATTERN_CACHE_TTL = 300.0


def format_pattern_hint(pattern: "FixPattern") -> str:
    """Format a mid-confidence pattern as a prompt hint for the LLM."""
    lines = [
        f"A previous fix for rule `{pattern.rule}` succeeded with a similar code pattern (confidence: {pattern.confidence:.0%}):",
        "",
        "**Before:**",
        "```",
        pattern.before_snippet,
        "```",
        "",
        "**After:**",
        "```",
        pattern.after_snippet,
        "```",
        "",
        "Consider applying a similar transformation.",
    ]
    return "\n".join(lines)


def try_replay(
    worktree_path: Path,
    finding: Finding,
    store: FixPatternStore,
    baseline_checks: Dict[str, List[str]],
    log_file: Path,
    shared_library: Any = None,
) -> Tuple[bool, Optional[str]]:
    try:
        return _try_replay_inner(
            worktree_path, finding, store, baseline_checks, log_file, shared_library
        )
    except Exception as exc:
        try:
            append_log(
                log_file,
                (
                    f"pattern-replay-error: rule={finding.rule} "
                    f"reason=exception error={type(exc).__name__}"
                ),
            )
        except Exception:
            _logger.debug("Failed to log pattern-replay-error")
        return (False, None)


def _resolve_pattern(
    finding: Finding,
    store: FixPatternStore,
    log_file: Path,
    shared_library: Any = None,
) -> Optional[FixPattern]:
    cache_key = (
        f"{finding.rule}::{finding.path}::{normalize_snippet(finding.snippet)[:64]}"
    )
    now = time.monotonic()
    cached = _PATTERN_CACHE.get(cache_key)
    if cached is not None and (now - cached[0]) < _PATTERN_CACHE_TTL:
        return cached[1]

    normalized = normalize_snippet(finding.snippet)
    language = getattr(finding, "language", None) or infer_language_from_path(
        finding.path, fallback="python"
    )

    result: Optional[FixPattern] = None

    pattern = store.lookup(finding.rule, normalized)
    if pattern is not None:
        result = pattern

    if result is None:
        pattern = store.lookup_structural(finding.rule, normalized, language)
        if pattern is not None:
            append_log(
                log_file,
                (
                    f"pattern-replay-structural-hit: rule={finding.rule} "
                    f"pattern_id={pattern.pattern_id}"
                ),
            )
            result = pattern

    if result is None:
        from bluei.engine.constants import DETECTOR_CATALOG

        pattern = store.lookup_fuzzy(
            finding.rule,
            normalized,
            language,
            catalog=DETECTOR_CATALOG,
        )
        if pattern is not None:
            append_log(
                log_file,
                (
                    f"pattern-replay-fuzzy-hit: rule={finding.rule} "
                    f"pattern_id={pattern.pattern_id}"
                ),
            )
            result = pattern

    if result is None and shared_library is not None:
        from bluei.engine.constants import DETECTOR_CATALOG

        xrepo_pattern = shared_library.lookup(
            finding.rule,
            normalized,
            language,
            catalog=DETECTOR_CATALOG,
        )
        if xrepo_pattern is not None:
            append_log(
                log_file,
                (
                    f"pattern-replay-xrepo-hit: rule={finding.rule} "
                    f"pattern_id={xrepo_pattern.pattern_id} "
                    f"source_repos={len(xrepo_pattern.source_repos)}"
                ),
            )
            result = FixPattern(
                pattern_id=xrepo_pattern.pattern_id,
                rule=xrepo_pattern.rule,
                language=xrepo_pattern.language,
                file_path=finding.path,
                before_snippet=xrepo_pattern.before_snippet,
                after_snippet=xrepo_pattern.after_snippet,
                diff_patch="",
                confidence=xrepo_pattern.confidence,
                success_count=xrepo_pattern.success_count,
                failure_count=xrepo_pattern.failure_count,
            )

    _PATTERN_CACHE[cache_key] = (time.monotonic(), result)
    if len(_PATTERN_CACHE) > 512:
        oldest = sorted(_PATTERN_CACHE.items(), key=lambda x: x[1][0])[:256]
        for k, _ in oldest:
            _PATTERN_CACHE.pop(k, None)
    return result


def _clear_pattern_cache() -> None:
    _PATTERN_CACHE.clear()


def _try_replay_inner(
    worktree_path: Path,
    finding: Finding,
    store: FixPatternStore,
    baseline_checks: Dict[str, List[str]],
    log_file: Path,
    shared_library: Any = None,
) -> Tuple[bool, Optional[str]]:
    pattern = _resolve_pattern(finding, store, log_file, shared_library)

    if pattern is None:
        append_log(
            log_file,
            (f"pattern-replay-miss: rule={finding.rule} reason=no_matching_pattern"),
        )
        return (False, None)

    if not fnmatch.fnmatch(finding.path, pattern.file_pattern):
        append_log(
            log_file,
            (
                f"pattern-replay-skip: rule={finding.rule} pattern_id={pattern.pattern_id} reason=file_pattern_mismatch pattern={pattern.file_pattern} path={finding.path}"
            ),
        )
        return (False, None)

    pid = pattern.pattern_id
    confidence = pattern.confidence

    append_log(
        log_file,
        (
            f"pattern-replay-hit: rule={finding.rule} "
            f"pattern_id={pid} confidence={confidence:.2f}"
        ),
    )

    if confidence < PROMPT_HINT_THRESHOLD:
        return (False, None)

    if confidence < AUTO_REPLAY_THRESHOLD:
        return (False, pid)

    target_file = Path(worktree_path) / finding.path
    if not target_file.exists():
        append_log(
            log_file,
            (
                f"pattern-replay-fail: pattern_id={pid} reason=file_not_found "
                f"path={finding.path}"
            ),
        )
        return (False, None)

    file_text = target_file.read_text(encoding="utf-8")
    before_snippet = pattern.before_snippet
    after_snippet = pattern.after_snippet

    match_result = _find_snippet_in_file(file_text, before_snippet)
    if match_result is None:
        append_log(
            log_file,
            (f"pattern-replay-fail: pattern_id={pid} reason=snippet_not_found_in_file"),
        )
        return (False, None)

    match_pos, matched_length = match_result
    new_text = (
        file_text[:match_pos] + after_snippet + file_text[match_pos + matched_length :]
    )
    target_file.write_text(new_text, encoding="utf-8")

    validation_passed = _run_baseline_validation(
        worktree_path,
        baseline_checks,
        log_file,
    )

    if not validation_passed:
        run_capture(
            ["git", "checkout", "--", finding.path],
            cwd=Path(worktree_path),
            timeout=30,
        )
        store.record_replay(pid, success=False)
        append_log(
            log_file,
            (f"pattern-replay-fail: pattern_id={pid} reason=validation_failed"),
        )
        return (False, None)

    store.record_replay(pid, success=True)
    append_log(
        log_file, (f"pattern-replay-success: pattern_id={pid} validation=passed")
    )
    return (True, pid)


def _find_snippet_in_file(
    file_text: str, before_snippet: str
) -> Optional[Tuple[int, int]]:
    """Locate the normalized before-snippet within the file text.

    Uses normalized comparison so indentation differences don't block matching.
    Returns (character_offset, matched_length) into file_text, or None.

    matched_length is the length of the *original* text in the file (which may
    differ from len(before_snippet) when indentation was normalized).
    """
    if before_snippet in file_text:
        offset = file_text.index(before_snippet)
        return offset, len(before_snippet)

    normalized_before = normalize_snippet(before_snippet)
    file_lines = file_text.splitlines(True)

    snippet_line_count = normalized_before.count("\n") + 1
    if snippet_line_count == 0:
        return None

    for start_idx in range(len(file_lines)):
        end_idx = start_idx + snippet_line_count
        if end_idx > len(file_lines):
            break
        window = "".join(file_lines[start_idx:end_idx])
        if normalize_snippet(window) == normalized_before:
            offset = sum(len(file_lines[i]) for i in range(start_idx))
            matched_length = sum(len(file_lines[i]) for i in range(start_idx, end_idx))
            return offset, matched_length

    return None


def _run_baseline_validation(
    worktree_path: Path,
    baseline_checks: Dict[str, List[str]],
    log_file: Path,
) -> bool:
    """Run baseline validation checks from the worktree. Returns True if all pass."""
    if not baseline_checks:
        return True

    for name, cmd in baseline_checks.items():
        rc, out = run_capture(cmd, cwd=Path(worktree_path), timeout=120)
        if rc != 0:
            return False
    return True
