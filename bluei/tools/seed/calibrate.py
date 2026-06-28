"""Measure structural-match rates per rule family and emit calibration.yaml.

For each seeded Pattern, generate variations, compute how many match the
seed's structural hash, and derive per-family p_healthy/p_broken estimates.
Degenerate distributions (fewer than min_variance distinct structural hashes)
fall back to ADR-0012 defaults.

Output: bluei/engine/seeded_patterns/calibration.yaml

Note: this is BUILD-TIME tooling (lives in bluei/tools/seed/). Runtime code
in bluei/engine/ reads calibration.yaml directly via a small inline YAML
parse — do NOT import this module from engine/. See sprt.py:_load_calibration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import yaml

from bluei.engine.structural_hash import compute_structural_hash
from bluei.tools.seed.variations import generate_variations

_CALIBRATION_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "engine"
    / "seeded_patterns"
    / "calibration.yaml"
)


def calibrate_family(
    rule_family: str,
    language: str,
    seed_before: str,
    min_variance: int = 3,
) -> Dict:
    """Calibrate one rule family. Returns a calibration entry dict.

    Generates variations, measures structural-match rate, derives p_healthy
    estimate. If the variation suite is degenerate (fewer than min_variance
    distinct structural hashes), marks degenerate=True and returns defaults.
    """
    var_set = generate_variations(rule_family, language, seed_before)
    seed_hash = compute_structural_hash(seed_before, language)

    match_count = 0
    distinct_hashes = set()
    for v in var_set.variations:
        h = compute_structural_hash(v, language)
        distinct_hashes.add(h)
        if h == seed_hash:
            match_count += 1

    total = len(var_set.variations)
    degenerate = len(distinct_hashes) < min_variance

    if degenerate or total == 0:
        return {
            "p_healthy": 0.90,
            "p_broken": 0.50,
            "degenerate": True,
            "variations_tested": total,
            "distinct_hashes": len(distinct_hashes),
            "fallback_reason": (
                "insufficient structural variance in synthetic variations"
            ),
        }

    match_rate = match_count / total
    p_healthy = max(0.50, min(0.99, match_rate))
    p_broken = min(0.50, max(0.10, 1.0 - match_rate))
    return {
        "p_healthy": round(p_healthy, 3),
        "p_broken": round(p_broken, 3),
        "degenerate": False,
        "variations_tested": total,
        "distinct_hashes": len(distinct_hashes),
    }


def write_calibration(entries: Dict[str, Dict], path: Optional[Path] = None) -> Path:
    """Write the calibration.yaml file. Returns the path written."""
    out_path = path or _CALIBRATION_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "calibration_version": 1,
        "calibrated_at": _now_iso(),
        "families": entries,
    }
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=True)
    return out_path


def _now_iso() -> str:
    from bluei.engine.models import now_iso

    return now_iso()


def load_calibration(path: Optional[Path] = None) -> Dict[str, Dict]:
    """Load calibration.yaml. Returns {family: entry} or {} if absent/malformed.

    Build-time consumer (harness/tests). Runtime engine code reads the YAML
    inline via its own loader to avoid a bluei.engine→bluei.tools dependency.
    """
    in_path = path or _CALIBRATION_PATH
    if not in_path.is_file():
        return {}
    try:
        with in_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return {}
    return data.get("families", {}) if isinstance(data, dict) else {}
