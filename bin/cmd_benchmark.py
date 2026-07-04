"""Benchmark command handler.

Mirrors bin/cmd_campaign.py. Runs the Benchmark Harness over the committed
Seed Library corpus and prints a dev report (gap analysis + Flywheel Score).

Slice 2 of alpha.6 "Economics". NOT user-facing — internal dev tool.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from bin.cmd_utils import has_flag, parse_option
from bin.help_text import HELP_TEXT

if TYPE_CHECKING:
    from bluei.engine.model_governor import CoveragePolicy


def _cmd_benchmark(rest: list[str]) -> int:
    """Handle ``bluei benchmark``."""
    if rest and rest[0] in ("-h", "--help"):
        print(HELP_TEXT["benchmark"])
        return 0

    if has_flag(rest, "--help") or has_flag(rest, "-h"):
        print(HELP_TEXT["benchmark"])
        return 0

    from bluei.engine.model_governor import CoveragePolicy
    from bluei.tools.benchmark import (
        default_mock_discovery,
        run_benchmark,
    )
    from bluei.tools.benchmark.report import render_benchmark_markdown

    policy = _load_policy(parse_option(rest, "--policy"))
    discovery = default_mock_discovery()

    try:
        result = run_benchmark(policy, discovery)
    except Exception as exc:  # pragma: no cover — defensive
        print(f"bluei: benchmark failed: {exc}", file=sys.stderr)
        return 1

    output = parse_option(rest, "--output")
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote JSON: {out_path}")

    print(render_benchmark_markdown(result))
    return 0


def _load_policy(policy_path: str | None) -> "CoveragePolicy":
    """Load a CoveragePolicy from YAML, or return the default."""
    from bluei.engine.model_governor import CoveragePolicy

    if not policy_path:
        return CoveragePolicy()
    import yaml

    raw = yaml.safe_load(Path(policy_path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return CoveragePolicy()
    from bluei.engine.model_governor import ModelTier

    kwargs = {}
    if "tier_0_min_assets" in raw:
        kwargs["tier_0_min_assets"] = int(raw["tier_0_min_assets"])
    if "tier_1_min_assets" in raw:
        kwargs["tier_1_min_assets"] = int(raw["tier_1_min_assets"])
    if "cascade_matched_tier" in raw:
        kwargs["cascade_matched_tier"] = ModelTier(raw["cascade_matched_tier"])
    if "escalate_on_zero_coverage" in raw:
        kwargs["escalate_on_zero_coverage"] = bool(raw["escalate_on_zero_coverage"])
    return CoveragePolicy(**kwargs)
