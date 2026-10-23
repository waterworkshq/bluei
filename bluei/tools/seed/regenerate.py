"""Regenerate seeded Patterns from existing Golden Validation Bundles.

alpha.3 shipped 38 Golden Validation Bundles but the parallel seeded Pattern
JSONLs (specified by package.py's Pattern-writing gate) never landed. This
module reads each bundle, reconstructs a ParsedRule, re-validates via the
linter (to auto-detect has_autofix), and calls package_rule so the
detection-only rules (has_autofix=False with a non-empty after) produce the
seeded Patterns. No LLM is needed — the bundles are already validated LLM
output.

D3-06: regeneration is TWO-PASS. Pass 1 reads and re-validates EVERY bundle
and builds the full output plan in memory; nothing is deleted or written.
Only when every bundle validated (and at least one Pattern would be written)
does Pass 2 clear the shipped ``*.jsonl`` and write the new set. Any
validation failure — including the fail-closed missing-linter case
(``validate_candidate`` returns invalid when ruff/eslint is not on PATH) —
aborts via :class:`RegenerationError` BEFORE any deletion, so a machine
without the linters can never wipe the shipped corpus.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from bluei.tools.seed.models import ParsedRule
from bluei.tools.seed.package import package_rule
from bluei.tools.seed.validator import validate_candidate

_logger = logging.getLogger(__name__)


class RegenerationError(RuntimeError):
    """A bundle failed re-validation; shipped seeded patterns were NOT touched.

    Raised by :func:`regenerate_patterns_from_bundles` after Pass 1 when at
    least one bundle failed re-validation. The message names the first
    failing bundle and its reason. Callers (the CLI wrapper) surface this as
    a non-zero exit.
    """


def _parse_bundle(bundle_path: Path) -> Optional[ParsedRule]:
    """Parse one Golden Bundle YAML into a ParsedRule.

    Returns None for files that are not bundle-shaped (not a mapping, or no
    ``rule`` key) — same skip condition the original single-pass loop used.
    """
    raw = yaml.safe_load(bundle_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "rule" not in raw:
        return None

    rule = raw["rule"]
    source_linter = ""
    if rule.startswith("ruff-"):
        source_linter = "ruff"
    elif rule.startswith("eslint-"):
        source_linter = "eslint"

    return ParsedRule(
        rule=rule,
        language=raw["language"],
        before=raw["before"],
        after=raw.get("after") or None,
        detector_before=raw.get("detector_before", ""),
        detector_after=raw.get("detector_after", ""),
        negative_examples=list(raw.get("negative_examples", [])),
        has_autofix=False,
        source_linter=source_linter,
        validation_command=raw.get("validation_command", ""),
        description="",
    )


def regenerate_patterns_from_bundles(
    golden_bundles_dir: Path,
    seeded_patterns_dir: Path,
    ruff_executable: str = "ruff",
    eslint_executable: str = "eslint",
    bundles_tmp_dir: Optional[Path] = None,
) -> Dict[str, int]:
    """Regenerate seeded Patterns from existing Golden Validation Bundles.

    Two-pass (D3-06): Pass 1 reads each gb-*.yaml bundle, constructs a
    ParsedRule, and re-validates via the linter (to auto-detect
    has_autofix) — with NO writes and NO deletions. Pass 2 (only if every
    bundle validated and at least one Pattern would be written) calls
    package_rule for each validated rule; the Pattern-writing gate in
    package_rule fires for any rule with a non-empty after.

    The bundles themselves are re-written to bundles_tmp_dir (or a tempdir
    if None) — they should be identical to the existing ones and are NOT
    committed (the existing bundles are the canonical shipped set).

    Raises:
        RegenerationError: If any bundle failed re-validation (including the
            fail-closed missing-linter case). Raised BEFORE any existing
            file is deleted or written.

    Returns: {"bundles_read": N, "patterns_written": M, "skipped_autofixable": K}
    """
    seeded_patterns_dir = Path(seeded_patterns_dir)
    golden_bundles_dir = Path(golden_bundles_dir)

    # ---- PASS 1: read + validate ALL bundles; build the output plan in
    # memory. Nothing is deleted and nothing is written in this pass.
    bundles_read = 0
    valid_rules: List[ParsedRule] = []
    failures: List[str] = []
    for bundle_path in sorted(golden_bundles_dir.glob("*.yaml")):
        parsed = _parse_bundle(bundle_path)
        if parsed is None:
            continue
        bundles_read += 1

        result = validate_candidate(
            parsed,
            ruff_executable=ruff_executable,
            eslint_executable=eslint_executable,
        )
        if not result.valid:
            _logger.error(
                "Bundle %s failed re-validation (rule=%s): %s",
                bundle_path.name,
                parsed.rule,
                result.reason,
            )
            failures.append(
                f"{bundle_path.name} (rule={parsed.rule}): {result.reason}"
            )
            continue
        valid_rules.append(parsed)

    if failures:
        raise RegenerationError(
            f"{len(failures)} of {bundles_read} bundles failed re-validation; "
            "shipped seeded patterns left untouched. "
            f"First failure: {failures[0]}"
        )

    # A validated bundle only yields a Pattern when it has a fix shape
    # (non-empty after) — package.py's Pattern-writing gate.
    would_write = sum(1 for rule in valid_rules if rule.after)
    if would_write == 0:
        # Nothing to write — do NOT wipe the existing patterns (D3-06: the
        # wipe used to happen unconditionally, which mass-deleted the shipped
        # set whenever the run could not rewrite it).
        _logger.warning(
            "regenerate: 0 of %d validated bundles would produce a Pattern; "
            "existing seeded patterns left untouched",
            bundles_read,
        )
        return {
            "bundles_read": bundles_read,
            "patterns_written": 0,
            "skipped_autofixable": len(valid_rules),
        }

    # ---- PASS 2: every bundle validated — safe to wipe + write.
    # package_rule appends; clear existing *.jsonl to keep regeneration
    # idempotent (do NOT touch calibration.yaml).
    if seeded_patterns_dir.is_dir():
        for stale in seeded_patterns_dir.glob("*.jsonl"):
            stale.unlink()

    patterns_written = 0
    skipped_autofixable = 0

    tmp_owner = None
    if bundles_tmp_dir is None:
        tmp_owner = tempfile.TemporaryDirectory(prefix="bluei-regen-bundles-")
        bundles_tmp_dir = Path(tmp_owner.name)
    bundles_tmp_dir = Path(bundles_tmp_dir)
    bundles_tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        for parsed in valid_rules:
            manifest = package_rule(
                parsed,
                bundles_dir=bundles_tmp_dir,
                seeded_patterns_dir=seeded_patterns_dir,
            )
            if manifest.get("pattern") is not None:
                patterns_written += 1
            else:
                skipped_autofixable += 1
    finally:
        if tmp_owner is not None:
            tmp_owner.cleanup()

    return {
        "bundles_read": bundles_read,
        "patterns_written": patterns_written,
        "skipped_autofixable": skipped_autofixable,
    }
