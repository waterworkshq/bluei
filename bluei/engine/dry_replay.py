"""Dry Replay — non-mutating evidence collection for matched-but-not-selected
FixPatterns (ADR-0011).

During a pr-cycle, after the winner fix is applied, Dry Replay restores the
pre-fix file content, applies each alternate candidate Pattern, runs the
baseline validation, records the counterfactual outcome to
``dry_replay.jsonl``, then restores the winner's content. The worktree is
left unchanged.

Records produced here are read by Phase 7 (SPRT) to update Pattern confidence
without risking the live cycle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.governance import format_asset_ref, is_governance_active
from bluei.engine.jsonl import append_jsonl
from bluei.engine.models import Finding, now_iso
from bluei.engine.pattern_replay import _find_snippet_in_file, _run_baseline_validation
from bluei.engine.pattern_store import FixPattern, FixPatternStore, normalize_snippet
from bluei.engine.report import infer_language_from_path

_logger = logging.getLogger(__name__)


def collect_dry_replay_candidates(
    finding: Finding,
    pattern_store: FixPatternStore,
    governance_state: Dict[str, str],
    winner_pattern_id: Optional[str] = None,
) -> List[FixPattern]:
    """Find all Patterns that matched the finding but weren't the winner.

    Runs the three lookup strategies (exact / structural / fuzzy) against the
    finding's rule + normalized snippet, collects unique patterns by
    ``pattern_id``, excludes the winner and any PAUSED/RETIRED patterns.

    Returns the candidate list in lookup-order (exact, then structural, then
    fuzzy). The Dry Replay caller may cap the count.
    """
    normalized = normalize_snippet(finding.snippet)
    language = getattr(finding, "language", None) or infer_language_from_path(
        finding.path, fallback="python"
    )

    candidates: List[FixPattern] = []
    seen: set[str] = set()

    lookup_hits: List[Optional[FixPattern]] = []
    try:
        lookup_hits.append(
            pattern_store.lookup(finding.rule, normalized, target_path=finding.path)
        )
    except Exception as exc:
        _logger.debug("dry-replay exact lookup failed: %s", exc)
    try:
        lookup_hits.append(
            pattern_store.lookup_structural(
                finding.rule, normalized, language, target_path=finding.path
            )
        )
    except Exception as exc:
        _logger.debug("dry-replay structural lookup failed: %s", exc)
    try:
        from bluei.engine.constants import DETECTOR_CATALOG

        lookup_hits.append(
            pattern_store.lookup_fuzzy(
                finding.rule,
                normalized,
                language,
                catalog=DETECTOR_CATALOG,
                target_path=finding.path,
            )
        )
    except Exception as exc:
        _logger.debug("dry-replay fuzzy lookup failed: %s", exc)

    for pattern in lookup_hits:
        if pattern is None:
            continue
        pid = pattern.pattern_id
        if pid in seen:
            continue
        if winner_pattern_id is not None and pid == winner_pattern_id:
            continue
        if not is_governance_active(format_asset_ref("pattern", pid), governance_state):
            continue
        seen.add(pid)
        candidates.append(pattern)

    return candidates


def run_dry_replay(
    finding: Finding,
    candidates: List[FixPattern],
    worktree_path: Path,
    original_content: str,
    winner_content: str,
    baseline_checks: Dict[str, Any],
    log_file: Path,
    run_id: str,
    dry_replay_path: Path,
    cap: int = 20,
) -> int:
    """Run Dry Replay for each candidate. Returns the number performed.

    For each candidate (up to ``cap``):
      1. Restore original (pre-fix) content on the target file.
      2. Apply the Pattern via ``_find_snippet_in_file`` + replace.
      3. If snippet not found → outcome = ``"MISS"`` (skip validation).
      4. If snippet found → run ``_run_baseline_validation`` → ``"HIT"`` /
         ``"FAILURE"``.
      5. Record to ``dry_replay_path``.
      6. Restore winner content (worktree unchanged across the call).
    """
    target_rel = finding.path
    target_file = Path(worktree_path) / target_rel

    performed = 0
    for pattern in candidates[:cap]:
        outcome = "MISS"
        try:
            target_file.write_text(original_content, encoding="utf-8")

            match_result = _find_snippet_in_file(
                original_content, pattern.before_snippet
            )
            if match_result is None:
                outcome = "MISS"
            else:
                match_pos, matched_length = match_result
                new_text = (
                    original_content[:match_pos]
                    + pattern.after_snippet
                    + original_content[match_pos + matched_length :]
                )
                target_file.write_text(new_text, encoding="utf-8")

                if _run_baseline_validation(worktree_path, baseline_checks, log_file):
                    outcome = "HIT"
                else:
                    outcome = "FAILURE"
        except Exception as exc:
            _logger.debug(
                "dry-replay candidate failed: pattern_id=%s error=%s",
                pattern.pattern_id,
                exc,
            )
            outcome = "FAILURE"
        finally:
            try:
                append_jsonl(
                    dry_replay_path,
                    {
                        "run_id": run_id,
                        "pattern_id": pattern.pattern_id,
                        "finding_id": finding.finding_id,
                        "rule": finding.rule,
                        "would_have_outcome": outcome,
                        "validation_commands_passed": [],
                        "timestamp": now_iso(),
                    },
                )
            except Exception as exc:
                _logger.debug(
                    "dry-replay record write failed: pattern_id=%s error=%s",
                    pattern.pattern_id,
                    exc,
                )
            try:
                target_file.write_text(winner_content, encoding="utf-8")
            except Exception as exc:
                _logger.debug(
                    "dry-replay winner restore failed: path=%s error=%s",
                    target_rel,
                    exc,
                )
            performed += 1

    return performed
