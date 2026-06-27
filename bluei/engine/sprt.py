"""bluei.engine.sprt — two-sided Sequential Probability Ratio Test (ADR-0012).

After each pr-cycle, the Dry Replay phase has appended counterfactual
outcomes (HIT / MISS / FAILURE) to ``dry_replay.jsonl``. SPRT scans each
Pattern's outcomes since its most recent reset boundary, computes the
log-likelihood ratio between two hypotheses

* H1 (healthy): probability of a positive Dry Replay = ``p_healthy``
* H0 (broken) : probability of a positive Dry Replay = ``p_broken``

and, if the LLR crosses a boundary, writes an ``ApprovalRecord``:

* LLR >= A (``log((1-beta)/alpha)``) → ``auto_promote`` (H1 wins: the pattern
  reliably produces HIT outcomes — it's healthy, promote to ACTIVE).
* LLR <= B (``log(beta/(1-alpha))``)  → ``auto_demote``  (H0 wins: the pattern
  reliably produces FAIL outcomes — it's broken, demote to PAUSED).

HITs contribute ``ln(p_healthy/p_broken)`` (positive, pushes toward promote).
FAILs contribute ``ln((1-p_healthy)/(1-p_broken))`` (negative, pushes toward
demote). MISS outcomes do not contribute to the LLR. The LLR is recomputed
from durable stores (``dry_replay.jsonl`` + ``approval_records.jsonl``) on
every call — there is no stored accumulator.

This module is inert during alpha: no cycles run, so no Dry Replay records
exist for SPRT to consume.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bluei.engine.governance import find_sprt_reset_boundary
from bluei.engine.jsonl import read_jsonl
from bluei.engine.models import now_iso

_DEFAULTS: Dict[str, float] = {
    "alpha": 0.01,
    "beta": 0.01,
    "p_healthy": 0.90,
    "p_broken": 0.50,
}


def _sprt_params(config: Dict[str, Any]) -> Dict[str, float]:
    """Read SPRT params from ``learning.sprt`` in config with defaults."""
    section = config.get("learning", {}).get("sprt", {})
    return {k: float(section.get(k, d)) for k, d in _DEFAULTS.items()}


def compute_llr(
    pattern_id: str,
    dry_replay_path: Path,
    approval_records: List[Dict[str, Any]],
    p_healthy: float = 0.90,
    p_broken: float = 0.50,
) -> Tuple[float, int, int]:
    """Compute LLR for a Pattern since its most recent reset boundary.

    Args:
        pattern_id: The Pattern ID to compute LLR for.
        dry_replay_path: Path to ``dry_replay.jsonl``.
        approval_records: Full approval_records trail (used for reset boundary).
        p_healthy: Probability of a positive Dry Replay under H1 (healthy).
        p_broken: Probability of a positive Dry Replay under H0 (broken).

    Returns:
        ``(llr, hit_count, fail_count)``. MISS outcomes do not contribute.
    """
    asset_ref = f"pattern:{pattern_id}"
    reset_ts = find_sprt_reset_boundary(approval_records, asset_ref)

    records = read_jsonl(dry_replay_path, skip_errors=True)

    hits = 0
    fails = 0
    for r in records:
        if r.get("pattern_id") != pattern_id:
            continue
        ts = r.get("timestamp")
        if reset_ts is not None and (ts is None or ts < reset_ts):
            continue
        outcome = r.get("would_have_outcome")
        if outcome == "HIT":
            hits += 1
        elif outcome == "FAILURE":
            fails += 1

    llr = hits * math.log(p_healthy / p_broken) + fails * math.log(
        (1 - p_healthy) / (1 - p_broken)
    )
    return llr, hits, fails


def check_sprt(llr: float, alpha: float = 0.01, beta: float = 0.01) -> Optional[str]:
    """Map an LLR value to an SPRT decision.

    LLR formula: hits * ln(p_healthy/p_broken) + fails * ln((1-p_healthy)/(1-p_broken)).
    HITs push LLR positive (evidence favors healthy); FAILs push it negative
    (evidence favors broken).

    Returns ``"auto_demote"`` if ``llr <= B`` (evidence strongly favors broken),
    ``"auto_promote"`` if ``llr >= A`` (evidence strongly favors healthy),
    otherwise ``None`` (no boundary crossed).
    """
    A = math.log((1 - beta) / alpha)
    B = math.log(beta / (1 - alpha))
    if llr >= A:
        return "auto_promote"
    if llr <= B:
        return "auto_demote"
    return None


def run_sprt_check(
    pattern_ids: List[str],
    dry_replay_path: Path,
    approval_records_path: Path,
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Check SPRT for each Pattern. Returns ApprovalRecord dicts to append.

    Reads SPRT params from ``learning.sprt`` in config (with defaults). For
    each pattern, recomputes the LLR from durable stores and, if a boundary
    is crossed, returns an ApprovalRecord dict with the full evidence
    snapshot. Patterns that don't cross a boundary are skipped.
    """
    params = _sprt_params(config)
    alpha = params["alpha"]
    beta = params["beta"]
    p_healthy = params["p_healthy"]
    p_broken = params["p_broken"]

    A = math.log((1 - beta) / alpha)
    B = math.log(beta / (1 - alpha))

    records = read_jsonl(approval_records_path, skip_errors=True)

    new_records: List[Dict[str, Any]] = []
    for pattern_id in pattern_ids:
        if not pattern_id:
            continue
        llr, hits, fails = compute_llr(
            pattern_id,
            dry_replay_path,
            records,
            p_healthy=p_healthy,
            p_broken=p_broken,
        )
        decision = check_sprt(llr, alpha=alpha, beta=beta)
        if decision is None:
            continue
        new_records.append(
            {
                "asset_ref": f"pattern:{pattern_id}",
                "decision": decision,
                "native_state_before": (
                    "active" if decision == "auto_demote" else "paused"
                ),
                "native_state_after": (
                    "paused" if decision == "auto_demote" else "active"
                ),
                "reason": f"SPRT: LLR={llr:.3f}, hits={hits}, fails={fails}",
                "evidence_snapshot": {
                    "llr": llr,
                    "hits": hits,
                    "fails": fails,
                    "A": A,
                    "B": B,
                    "alpha": alpha,
                    "beta": beta,
                    "p_healthy": p_healthy,
                    "p_broken": p_broken,
                },
                "actor": "system:sprt",
                "timestamp": now_iso(),
            }
        )
    return new_records
