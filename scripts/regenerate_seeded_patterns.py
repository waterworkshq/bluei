#!/usr/bin/env python3
"""Regenerate the shipped seeded Patterns from the existing Golden Bundles.

Thin CLI wrapper around bluei.tools.seed.regenerate.regenerate_patterns_from_bundles.
Run from the repo root:

    PYTHONPATH=. .venv/bin/python scripts/regenerate_seeded_patterns.py

Exit codes (D3-06): 0 on a productive regeneration; 1 when any bundle fails
re-validation (RegenerationError — nothing was deleted) or when bundles were
read but zero Patterns were written (e.g. linters missing from PATH make the
validator fail closed; the shipped patterns are never silently wiped with a
success exit).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_BUNDLES_DIR = REPO_ROOT / "bluei" / "engine" / "golden_bundles"
SEEDED_PATTERNS_DIR = REPO_ROOT / "bluei" / "engine" / "seeded_patterns"


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    from bluei.tools.seed.regenerate import (
        RegenerationError,
        regenerate_patterns_from_bundles,
    )

    ruff_executable = shutil.which("ruff") or "ruff"
    eslint_executable = shutil.which("eslint") or "eslint"

    try:
        summary = regenerate_patterns_from_bundles(
            golden_bundles_dir=GOLDEN_BUNDLES_DIR,
            seeded_patterns_dir=SEEDED_PATTERNS_DIR,
            ruff_executable=ruff_executable,
            eslint_executable=eslint_executable,
        )
    except RegenerationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("Seeded pattern regeneration summary:")
    for key, value in summary.items():
        print(f"  {key}: {value}")

    if summary["bundles_read"] > 0 and summary["patterns_written"] == 0:
        print(
            "error: read "
            f"{summary['bundles_read']} bundles but wrote 0 patterns — "
            "existing seeded patterns left untouched",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
