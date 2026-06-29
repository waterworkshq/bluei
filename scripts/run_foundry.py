#!/usr/bin/env python3
"""Run the Recipe Foundry against seeded Patterns to produce canonical Recipes.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/run_foundry.py

Reads seeded Patterns from bluei/engine/seeded_patterns/, invokes the claude
CLI for each to author Recipe YAML, validates via detector oracle, and writes
to bluei/engine/recipes/staged/. Output is then curated and promoted to
built-in/ as the canonical seeded-Recipe set.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bluei.engine.pattern_store import FixPattern  # noqa: E402
from bluei.engine.bundle_loader import load_bundles  # noqa: E402
from bluei.engine.jsonl import read_jsonl  # noqa: E402
from bluei.tools.foundry.pipeline import run_foundry  # noqa: E402


def load_seeded_patterns() -> list[FixPattern]:
    patterns: list[FixPattern] = []
    seeded_dir = REPO_ROOT / "bluei" / "engine" / "seeded_patterns"
    for jsonl_path in sorted(seeded_dir.glob("*.jsonl")):
        for record in read_jsonl(jsonl_path):
            if isinstance(record, dict):
                patterns.append(FixPattern.from_dict(record))
    return patterns


from typing import Callable


def make_claude_callback() -> Callable[[str], str]:
    def callback(prompt: str) -> str:
        try:
            result = subprocess.run(
                ["claude", "-p"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return result.stdout
        except Exception as exc:
            print(f"  claude callback error: {exc}", file=sys.stderr)
            return ""

    return callback


def main() -> int:
    patterns = load_seeded_patterns()
    bundles = load_bundles()
    staged_dir = REPO_ROOT / "bluei" / "engine" / "recipes" / "staged"

    print(f"Loaded {len(patterns)} seeded Patterns, {len(bundles)} bundles")
    print(f"Output dir: {staged_dir}")
    print()

    callback = make_claude_callback()
    summary = run_foundry(
        patterns=patterns,
        bundles=bundles,
        llm_callback=callback,
        staged_dir=staged_dir,
        config={},
        approval_records_path=None,
    )

    print()
    print("=== Foundry Summary ===")
    print(f"  Proposed:     {summary.proposed}")
    print(f"  Rejected:     {summary.rejected}")
    print(f"  Written:      {summary.written}")
    print(f"  Auto-promoted:{summary.auto_promoted}")
    if summary.rejected_topics:
        print(f"  Rejected topics:")
        for topic in summary.rejected_topics:
            print(f"    - {topic}")

    return 0 if summary.written > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
