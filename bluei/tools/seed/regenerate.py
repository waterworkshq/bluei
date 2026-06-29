"""Regenerate seeded Patterns from existing Golden Validation Bundles.

alpha.3 shipped 38 Golden Validation Bundles but the parallel seeded Pattern
JSONLs (specified by package.py's Pattern-writing gate) never landed. This
module reads each bundle, reconstructs a ParsedRule, re-validates via the
linter (to auto-detect has_autofix), and calls package_rule so the
detection-only rules (has_autofix=False with a non-empty after) produce the
seeded Patterns. No LLM is needed — the bundles are already validated LLM
output.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Optional

import yaml

from bluei.tools.seed.models import ParsedRule
from bluei.tools.seed.package import package_rule
from bluei.tools.seed.validator import validate_candidate

_logger = logging.getLogger(__name__)


def regenerate_patterns_from_bundles(
    golden_bundles_dir: Path,
    seeded_patterns_dir: Path,
    ruff_executable: str = "ruff",
    eslint_executable: str = "eslint",
    bundles_tmp_dir: Optional[Path] = None,
) -> Dict[str, int]:
    """Regenerate seeded Patterns from existing Golden Validation Bundles.

    Reads each gb-*.yaml bundle, constructs a ParsedRule, re-validates via the
    linter (to auto-detect has_autofix), and calls package_rule. The
    Pattern-writing gate in package_rule fires for detection-only rules
    (has_autofix=False with a non-empty after).

    The bundles themselves are re-written to bundles_tmp_dir (or a tempdir if
    None) — they should be identical to the existing ones and are NOT committed
    (the existing bundles are the canonical shipped set).

    Returns: {"bundles_read": N, "patterns_written": M, "skipped_autofixable": K}
    """
    seeded_patterns_dir = Path(seeded_patterns_dir)
    golden_bundles_dir = Path(golden_bundles_dir)

    # package_rule appends; clear existing *.jsonl to keep regeneration
    # idempotent (do NOT touch calibration.yaml).
    if seeded_patterns_dir.is_dir():
        for stale in seeded_patterns_dir.glob("*.jsonl"):
            stale.unlink()

    bundles_read = 0
    patterns_written = 0
    skipped_autofixable = 0

    tmp_owner = None
    if bundles_tmp_dir is None:
        tmp_owner = tempfile.TemporaryDirectory(prefix="bluei-regen-bundles-")
        bundles_tmp_dir = Path(tmp_owner.name)
    bundles_tmp_dir = Path(bundles_tmp_dir)
    bundles_tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        for bundle_path in sorted(golden_bundles_dir.glob("*.yaml")):
            raw = yaml.safe_load(bundle_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or "rule" not in raw:
                continue
            bundles_read += 1

            rule = raw["rule"]
            after = raw.get("after") or None
            source_linter = ""
            if rule.startswith("ruff-"):
                source_linter = "ruff"
            elif rule.startswith("eslint-"):
                source_linter = "eslint"

            parsed = ParsedRule(
                rule=rule,
                language=raw["language"],
                before=raw["before"],
                after=after,
                detector_before=raw.get("detector_before", ""),
                detector_after=raw.get("detector_after", ""),
                negative_examples=list(raw.get("negative_examples", [])),
                has_autofix=False,
                source_linter=source_linter,
                validation_command=raw.get("validation_command", ""),
                description="",
            )

            result = validate_candidate(
                parsed,
                ruff_executable=ruff_executable,
                eslint_executable=eslint_executable,
            )
            if not result.valid:
                _logger.warning(
                    "Bundle %s failed re-validation (rule=%s): %s",
                    bundle_path.name,
                    rule,
                    result.reason,
                )
                continue

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
