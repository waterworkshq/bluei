"""Pattern replay engine — match findings to stored patterns and apply deterministically."""

from __future__ import annotations

import logging
import fnmatch
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

from bluei.engine.models import Finding
from bluei.engine.pattern_store import (
    FixPattern,
    FixPatternStore,
    ReplayOutcome,
    normalize_snippet,
)
from bluei.engine.report import infer_language_from_path


def _matches_file_pattern(path: str, pattern: str) -> bool:
    """Match a file path against a glob pattern with ** support.

    Python's fnmatch doesn't support globstar — fnmatch('app.py', '**/*')
    returns False because ** is treated as two literal * chars requiring a /.
    This helper handles ** as 'match any path segments'.
    """
    if pattern in ("**/*", "**"):
        return True
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.startswith("**/"):
        suffix = pattern[3:]
        return (
            path == suffix
            or path.endswith("/" + suffix)
            or fnmatch.fnmatch(path, suffix)
        )
    return False


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
    record_outcome: bool = True,
    exclude_pattern_ids: Optional[set] = None,
) -> Tuple[bool, Optional[str]]:
    try:
        return _try_replay_inner(
            worktree_path,
            finding,
            store,
            baseline_checks,
            log_file,
            shared_library,
            record_outcome,
            exclude_pattern_ids,
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
    exclude_pattern_ids: Optional[set] = None,
) -> Optional[FixPattern]:
    def _excluded(candidate: Optional[FixPattern]) -> bool:
        # A pattern the caller already attempted for this finding (e.g. the
        # standalone mainline replay before the cascade re-tries) resolves as
        # not-found, so the second path cannot re-attempt and double-record
        # the same outcome.
        if candidate is None or not exclude_pattern_ids:
            return False
        if candidate.pattern_id in exclude_pattern_ids:
            append_log(
                log_file,
                (
                    f"pattern-replay-skip: rule={finding.rule} "
                    f"pattern_id={candidate.pattern_id} "
                    f"reason=excluded_upstream_attempt"
                ),
            )
            return True
        return False

    cache_key = (
        f"{finding.rule}::{finding.path}::{normalize_snippet(finding.snippet)[:64]}"
    )
    now = time.monotonic()
    cached = _PATTERN_CACHE.get(cache_key)
    if cached is not None and (now - cached[0]) < _PATTERN_CACHE_TTL:
        return None if _excluded(cached[1]) else cached[1]

    normalized = normalize_snippet(finding.snippet)
    language = getattr(finding, "language", None) or infer_language_from_path(
        finding.path, fallback="python"
    )

    result: Optional[FixPattern] = None

    pattern = store.lookup(finding.rule, normalized, target_path=finding.path)
    if pattern is not None:
        result = pattern

    if result is None:
        pattern = store.lookup_structural(
            finding.rule, normalized, language, target_path=finding.path
        )
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
            target_path=finding.path,
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
            # Cap cross-repo confidence below AUTO_REPLAY_THRESHOLD.
            # Cross-repo patterns have privacy-normalized snippets (_v0, _v1, etc.)
            # that can't be literally matched in real source files. Auto-replay
            # would silently fail with "snippet_not_found_in_file". Capping here
            # ensures they fall through to the LLM hint path instead, which is
            # the only working application method for cross-repo patterns today.
            # TODO: implement AST-based structural replay to enable true cross-repo auto-apply.
            effective_confidence = min(
                xrepo_pattern.confidence, PROMPT_HINT_THRESHOLD - 0.01
            )
            append_log(
                log_file,
                (
                    f"pattern-replay-xrepo-hit: rule={finding.rule} "
                    f"pattern_id={xrepo_pattern.pattern_id} "
                    f"source_repos={len(xrepo_pattern.source_repos)} "
                    f"effective_confidence={effective_confidence:.2f} (capped: cross-repo snippets are privacy-normalized)"
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
                confidence=effective_confidence,
                success_count=xrepo_pattern.success_count,
                failure_count=xrepo_pattern.failure_count,
            )

    _PATTERN_CACHE[cache_key] = (time.monotonic(), result)
    if len(_PATTERN_CACHE) > 512:
        oldest = sorted(_PATTERN_CACHE.items(), key=lambda x: x[1][0])[:256]
        for k, _ in oldest:
            _PATTERN_CACHE.pop(k, None)
    return None if _excluded(result) else result


def _clear_pattern_cache() -> None:
    _PATTERN_CACHE.clear()


@dataclass
class PatternApplication:
    """One successful rung of the pattern-application ladder.

    ``mechanism`` is ``"literal"`` (normalized snippet splice) or
    ``"structural"`` (AST structural replay, ADR-0019). ``structural``
    carries the :class:`StructuralReplayResult` when the structural rung
    applied, else ``None``.
    """

    new_text: str
    mechanism: str
    structural: Optional[Any] = None


def apply_pattern_to_text(
    file_text: str,
    pattern: FixPattern,
    language: str,
    target_line: int = 0,
) -> Optional[PatternApplication]:
    """Apply a Pattern's before→after transformation to file text.

    This is the SINGLE application ladder shared by live replay
    (``_try_replay_inner``) and Dry Replay (``run_dry_replay``):

      1. Literal (indentation-normalized) snippet match → text splice.
      2. On literal miss → AST structural replay (ADR-0019): rename-tolerant
         application via variable bindings.

    Returns a :class:`PatternApplication` with the new file text and which
    rung applied, or ``None`` when neither rung applies (the caller records
    a MISS). Purely functional — nothing is written to disk; the caller owns
    checkpointing, validation, and restoration.
    """
    match_result = _find_snippet_in_file(file_text, pattern.before_snippet)
    if match_result is not None:
        match_pos, matched_length = match_result
        new_text = (
            file_text[:match_pos]
            + pattern.after_snippet
            + file_text[match_pos + matched_length :]
        )
        return PatternApplication(new_text=new_text, mechanism="literal")

    from bluei.engine.structural_replay import apply_structural_replay

    structural = apply_structural_replay(
        file_text, pattern, language, target_line=target_line
    )
    if structural is not None:
        return PatternApplication(
            new_text=structural.new_source,
            mechanism="structural",
            structural=structural,
        )
    return None


def _try_replay_inner(
    worktree_path: Path,
    finding: Finding,
    store: FixPatternStore,
    baseline_checks: Dict[str, List[str]],
    log_file: Path,
    shared_library: Any = None,
    record_outcome: bool = True,
    exclude_pattern_ids: Optional[set] = None,
) -> Tuple[bool, Optional[str]]:
    pattern = _resolve_pattern(
        finding, store, log_file, shared_library, exclude_pattern_ids
    )

    if pattern is None:
        append_log(
            log_file,
            (f"pattern-replay-miss: rule={finding.rule} reason=no_matching_pattern"),
        )
        return (False, None)

    if not _matches_file_pattern(finding.path, pattern.file_pattern):
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
        if record_outcome:
            store.record_replay_outcome(pid, ReplayOutcome.MISS)
        append_log(
            log_file,
            (
                f"pattern-replay-fail: pattern_id={pid} reason=file_not_found "
                f"path={finding.path}"
            ),
        )
        return (False, None)

    file_text = target_file.read_text(encoding="utf-8")
    language = getattr(pattern, "language", None) or infer_language_from_path(
        finding.path, fallback="python"
    )

    # Shared application ladder (literal → structural). Dry Replay uses the
    # same helper so counterfactual outcomes mirror what live replay would
    # have done.
    application = apply_pattern_to_text(
        file_text, pattern, language, target_line=finding.line
    )
    if application is None:
        if record_outcome:
            store.record_replay_outcome(pid, ReplayOutcome.MISS)
        append_log(
            log_file,
            (f"pattern-replay-fail: pattern_id={pid} reason=snippet_not_found_in_file"),
        )
        return (False, None)

    target_file.write_text(application.new_text, encoding="utf-8")

    validation_passed = _run_baseline_validation(
        worktree_path,
        baseline_checks,
        log_file,
    )[0]

    if not validation_passed:
        run_capture(
            ["git", "checkout", "--", finding.path],
            cwd=Path(worktree_path),
            timeout=30,
        )
        if record_outcome:
            store.record_replay_outcome(pid, ReplayOutcome.FAILURE)
        if application.mechanism == "structural":
            append_log(
                log_file,
                (
                    f"pattern-replay-structural-fail: pattern_id={pid} "
                    f"reason=validation_failed"
                ),
            )
        else:
            append_log(
                log_file,
                (f"pattern-replay-fail: pattern_id={pid} reason=validation_failed"),
            )
        return (False, None)

    if record_outcome:
        store.record_replay_outcome(pid, ReplayOutcome.HIT)
    if application.mechanism == "structural":
        append_log(
            log_file,
            (
                f"pattern-replay-structural-success: pattern_id={pid} "
                f"binding_map_size={len(application.structural.binding_map)} "
                f"fuzzy={application.structural.fuzzy_score:.2f}"
            ),
        )
    else:
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
) -> Tuple[bool, List[str]]:
    """Run baseline validation checks. Returns (all_passed, names_of_passing_checks)."""
    if not baseline_checks:
        return True, []

    passing: List[str] = []
    for name, cmd in baseline_checks.items():
        rc, out = run_capture(cmd, cwd=Path(worktree_path), timeout=120)
        if rc != 0:
            return False, passing
        passing.append(name)
    return True, passing
