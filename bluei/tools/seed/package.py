"""Convert ParsedRule tuples into committed product fixtures.

Output classification (grilling Q2):
  - has_autofix=True  -> Bundle only (wildcard Recipes handle the fix)
  - has_autofix=False -> Bundle + seeded Pattern (+ Recipe if after is a clean text transform)

All output is additive product fixtures loaded at runtime by bundle_loader
and seeded_pattern_loader. Provenance is by storage locus (product dirs).
"""

import json
from pathlib import Path
from typing import Optional

import yaml

from bluei.engine.models import now_iso
from bluei.engine.rule_family import derive_rule_family
from bluei.engine.structural_hash import (
    compute_structural_hash,
    extract_imports_touched,
)
from bluei.tools.seed.models import ParsedRule


# Output directories (product fixtures, shipped with bluei)
_BUNDLES_DIR = Path(__file__).parent.parent.parent / "engine" / "golden_bundles"
_SEEDED_PATTERNS_DIR = (
    Path(__file__).parent.parent.parent / "engine" / "seeded_patterns"
)


def package_rule(
    parsed: ParsedRule,
    *,
    bundles_dir: Optional[Path] = None,
    seeded_patterns_dir: Optional[Path] = None,
) -> dict:
    """Package a ParsedRule into product fixtures. Returns a manifest of what was written.

    Writes:
      - Golden Bundle YAML (always) to golden_bundles/
      - Seeded Pattern JSONL entry (if not has_autofix) to seeded_patterns/<family>.jsonl
      - Seeded Recipe YAML (if not has_autofix AND after is a clean text transform) to recipes/built-in/

    The caller (the ingestion CLI) is responsible for committing these files.

    Output directories default to the shipped product dirs but can be
    overridden via ``bundles_dir`` / ``seeded_patterns_dir`` (used by tests
    and the ingestion CLI to redirect output).
    """
    bundles_dir = Path(bundles_dir) if bundles_dir is not None else _BUNDLES_DIR
    seeded_patterns_dir = (
        Path(seeded_patterns_dir)
        if seeded_patterns_dir is not None
        else _SEEDED_PATTERNS_DIR
    )

    rule_family = derive_rule_family(parsed.rule)
    imports = extract_imports_touched(parsed.before, parsed.language)
    if parsed.after:
        imports += extract_imports_touched(parsed.after, parsed.language)
    imports = sorted(set(imports))

    written = {"bundle": None, "pattern": None, "recipe": None}

    # 1. Always write a Golden Bundle
    bundle_id = f"gb-{parsed.rule}"
    bundle_data = {
        "id": bundle_id,
        "asset_class": "pattern",
        "asset_ref": None,
        "rule": parsed.rule,
        "rule_family": rule_family,
        "language": parsed.language,
        "before": parsed.before,
        "after": parsed.after or "",
        "detector_before": parsed.detector_before,
        "detector_after": parsed.detector_after,
        "validation_command": parsed.validation_command,
        "source_finding_id": None,
        "extracted_at": now_iso(),
        "imports_touched": imports,
        "negative_examples": list(parsed.negative_examples),
    }
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = bundles_dir / f"{bundle_id}.yaml"
    with bundle_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(bundle_data, f, sort_keys=False)
    written["bundle"] = str(bundle_path)

    # 2. Detection-only rules -> seeded Pattern (prompt-hint confidence, bypass cross-repo cap)
    if not parsed.has_autofix and parsed.after:
        pattern_entry = {
            "pattern_id": f"seed-{parsed.rule}",
            "rule": parsed.rule,
            "language": parsed.language,
            "file_path": "",
            "before_snippet": parsed.before,
            "after_snippet": parsed.after,
            "diff_patch": "",
            "confidence": 0.6,  # prompt-hint range; bypasses cross-repo cap (seeded, not privacy-normalized)
            "success_count": 0,
            "failure_count": 0,
            "skip_count": 0,
            "source": "authoritative-seed",
            "created_at": now_iso(),
            "last_used_at": None,
            "last_verified_at": None,
            "last_failed_at": None,
            "source_finding_ids": [],
            "framework_constraint": None,
            "file_pattern": "**/*",
            "structural_hash": compute_structural_hash(parsed.before, parsed.language),
            "excluded_paths": [],
            "imports_touched": imports,
            "validation_commands_passed": [],
            "rule_family": rule_family,
        }
        seeded_patterns_dir.mkdir(parents=True, exist_ok=True)
        patterns_file = seeded_patterns_dir / f"{rule_family}.jsonl"
        with patterns_file.open("a", encoding="utf-8") as f:
            f.write(_to_jsonl_line(pattern_entry))
        written["pattern"] = str(patterns_file)

    # 3. Seeded Recipe (detection-only with mechanical fix — rare; skip for now, document)
    # A Recipe needs a handler type (text/regex/command). Most detection-only rules
    # don't have a clean mechanical fix. This is intentionally conservative in alpha.3.
    # Recipe generation can be added when a parser identifies a mechanical transform.

    return written


def _to_jsonl_line(data: dict) -> str:
    return json.dumps(data, sort_keys=True) + "\n"
