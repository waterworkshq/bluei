"""rebase_stats.py — Structured telemetry for the auto-rebase system.

Provides JSONL-based logging of rebase sweep outcomes and a summary
function for health/observability surfaces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from bluei.engine.jsonl import read_jsonl
from bluei.engine.models import now_iso


# Default path for the rebase telemetry JSONL file (relative to the sandbox state dir).
LOG_REBASE_STATS_PATH = "state/rebase_stats.jsonl"


def log_rebase_stats(
    stats_path: Path,
    stats: Dict[str, Any],
) -> None:
    """Write one telemetry entry to the JSONL file.

    Each entry includes an ISO-8601 timestamp merged with the caller-supplied
    stats dict.

    Args:
        stats_path: Path to the ``rebase_stats.jsonl`` file.
        stats: Dictionary of telemetry fields (e.g. rebases_attempted,
            rebases_succeeded, duration_seconds, …).
    """
    entry: Dict[str, Any] = {
        "timestamp": now_iso(),
        **stats,
    }
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    with stats_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def load_rebase_stats(
    stats_path: Path,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return the most recent ``limit`` telemetry entries, newest first.

    Args:
        stats_path: Path to the ``rebase_stats.jsonl`` file.
        limit: Maximum number of entries to return.

    Returns:
        List of dicts, one per JSONL line, ordered newest-first.
        Returns an empty list if the file does not exist.
    """
    entries = read_jsonl(stats_path, limit=limit)
    entries.reverse()
    return entries


def summary_from_stats(
    entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute a compact summary dict from a list of telemetry entries.

    Useful for health endpoints or status reports.

    Args:
        entries: List of telemetry entries (e.g. from ``load_rebase_stats``).

    Returns:
        Dict with aggregate counters aggregated across all supplied entries.
    """
    total = len(entries)
    if total == 0:
        return {
            "total_sweeps": 0,
            "total_rebased": 0,
            "total_conflicted": 0,
            "total_skipped": 0,
            "avg_duration_seconds": 0.0,
            "success_rate_pct": 0.0,
        }

    total_rebased = sum(e.get("rebases_succeeded", 0) for e in entries)
    total_conflicted = sum(e.get("rebases_conflicted", 0) for e in entries)
    total_skipped = sum(e.get("rebases_skipped", 0) for e in entries)
    total_attempted = sum(e.get("rebases_attempted", 0) for e in entries)
    total_sweep_time = sum(
        e.get("duration_seconds", 0.0)
        for e in entries
        if isinstance(e.get("duration_seconds"), (int, float))
    )

    return {
        "total_sweeps": total,
        "total_rebased": total_rebased,
        "total_conflicted": total_conflicted,
        "total_skipped": total_skipped,
        "total_attempted": total_attempted,
        "avg_duration_seconds": round(total_sweep_time / total, 2)
        if total > 0
        else 0.0,
        "success_rate_pct": round(
            (total_rebased / total_attempted * 100) if total_attempted > 0 else 0.0, 1
        ),
        "latest_timestamp": entries[0].get("timestamp", ""),
    }
