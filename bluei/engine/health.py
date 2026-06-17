"""Health summary helpers that incorporate cost-tracking data.

Provides a thin function that reads cost history from a ``CostTracker`` log and
enriches a health summary dict with cost-related metrics such as
``avg_cost_per_run``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from bluei.engine.jsonl import read_jsonl


def enrich_health_with_cost(
    health_summary: Dict[str, Any],
    cost_log_path: Optional[Path] = None,
    total_runs: int = 0,
) -> Dict[str, Any]:
    """Augment *health_summary* with cost metrics if cost data is available.

    Args:
        health_summary: A dict (e.g. the health/snapshot section of a status
                        artifact) that may already contain keys like
                        ``score``, ``components``, etc.
        cost_log_path: Path to a ``cost_log.jsonl`` file written by
                       ``CostTracker``.  If ``None`` or the file does not
                       exist, no cost metrics are added.
        total_runs: Total number of runs to use as the denominator for
                    ``avg_cost_per_run``.  If 0 or less, the count of
                    invocations from the cost log is used instead.

    Returns:
        The augmented *health_summary* (same reference, for chaining).
    """
    if cost_log_path is None or not cost_log_path.exists():
        return health_summary

    try:
        entries = read_jsonl(cost_log_path, skip_errors=False)
    except (OSError, json.JSONDecodeError):
        return health_summary

    if not entries:
        return health_summary

    total_cost = sum(e.get("cost", 0.0) for e in entries)
    total_invocations = len(entries)

    # Per-model breakdown
    per_model: Dict[str, dict] = {}
    for e in entries:
        model = e.get("model", "unknown")
        rec = per_model.setdefault(model, {"count": 0, "cost": 0.0})
        rec["count"] += 1
        rec["cost"] += e.get("cost", 0.0)

    denominator = total_runs if total_runs > 0 else total_invocations
    avg_cost = round(total_cost / denominator, 6) if denominator > 0 else 0.0

    cost_info: Dict[str, Any] = {
        "total_cost": round(total_cost, 6),
        "total_invocations": total_invocations,
        "avg_cost_per_run": avg_cost,
        "per_model": {
            m: {"count": v["count"], "cost": round(v["cost"], 6)}
            for m, v in per_model.items()
        },
    }

    health_summary["cost"] = cost_info
    return health_summary


def build_cost_health_summary(
    cost_log_path: Optional[Path] = None,
    total_runs: int = 0,
) -> Dict[str, Any]:
    """Return a standalone cost health summary dict.

    Useful when you want cost info separate from an existing health dict.
    Delegates to :func:`enrich_health_with_cost` under the hood.
    """
    summary: Dict[str, Any] = {}
    enrich_health_with_cost(summary, cost_log_path=cost_log_path, total_runs=total_runs)
    return summary
