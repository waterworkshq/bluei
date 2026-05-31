"""auto_tune.py — Adapt thresholds based on telemetry patterns.

Reads review cycle telemetry (review_stats.jsonl) and adjusts
cycle parameters to reduce retry pressure and batch bloat.

The feedback loop:
  1. Review cycle runs → writes result to review_stats.jsonl
  2. Auto-tune reads last N records → if retry_failed > 0 consecutively → suggest lower batch/cooldown
  3. Runner reads suggestion → applies as CLI arg overrides
  4. Next cycle runs with adjusted params
"""

from __future__ import annotations

import logging
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)


# ── Defaults ──

CONSECUTIVE_RETRY_FAILURE_THRESHOLD = 4
COOLDOWN_BOOST_MULTIPLIER = 2.0
BATCH_REDUCTION_FACTOR = 0.5
MIN_BATCH_SIZE = 1
MAX_COOLDOWN_HOURS = 12


def _load_jsonl(path: Path, limit: int = 20) -> List[Dict[str, Any]]:
    """Load last N records from a JSONL file."""
    if not path.exists():
        return []
    try:
        lines = path.read_text().strip().splitlines()
        records: List[Dict[str, Any]] = []
        for line in lines[-limit:]:
            if line.strip():
                records.append(json.loads(line))
        return records
    except (json.JSONDecodeError, OSError):
        return []


def _load_tune_state(path: Path) -> Dict[str, Any]:
    """Load current tune state."""
    if not path.exists():
        return {'tuned_fields': {}, 'last_tune_ts': None}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {'tuned_fields': {}, 'last_tune_ts': None}


def _save_tune_state(path: Path, state: Dict[str, Any]) -> None:
    """Persist tune state."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2) + '\n')
    except OSError:
        _logger.debug("Failed to save tune state")


def _check_retry_pattern(records: List[Dict[str, Any]], threshold: int) -> Optional[int]:
    """Check if retry_failed > 0 for N consecutive records.

    Returns the count of consecutive failures, or None if below threshold.
    """
    consecutive = 0
    for rec in reversed(records):
        if rec.get('retry_failed', 0) > 0:
            consecutive += 1
        else:
            break
    return consecutive if consecutive >= threshold else None


def _check_finding_pattern(records: List[Dict[str, Any]], threshold: int) -> Optional[int]:
    """Check if findings_failed > 0 for N consecutive records."""
    consecutive = 0
    for rec in reversed(records):
        if rec.get('findings_failed', 0) > 0:
            consecutive += 1
        else:
            break
    return consecutive if consecutive >= threshold else None


def compute_tune(
    stats_path: Path,
    tune_path: Path,
    retry_threshold: int = CONSECUTIVE_RETRY_FAILURE_THRESHOLD,
) -> Dict[str, Any]:
    """Compute auto-tune from review telemetry.

    Returns a dict of suggested overrides (empty if no tune needed).
    """
    records = _load_jsonl(stats_path, limit=20)
    current = _load_tune_state(tune_path)
    override: Dict[str, Any] = {}

    # 1. Retry failure pattern → reduce batch load
    retry_hit = _check_retry_pattern(records, retry_threshold)
    if retry_hit is not None:
        current_batch = current.get('tuned_fields', {}).get('max_prs_per_run', 1)
        new_batch = max(MIN_BATCH_SIZE, int(current_batch * BATCH_REDUCTION_FACTOR))
        if new_batch < current_batch:
            override['max_prs_per_run'] = new_batch
            override['_reason'] = f'retry_failed x{retry_hit} consecutive cycles'

    # 2. Finding failure pattern → extend cooldown
    finding_hit = _check_finding_pattern(records, retry_threshold)
    if finding_hit is not None:
        current_cooldown = current.get('tuned_fields', {}).get('finding_cooldown_seconds', 14400)
        new_cooldown = min(
            int(current_cooldown * COOLDOWN_BOOST_MULTIPLIER),
            MAX_COOLDOWN_HOURS * 3600,
        )
        if new_cooldown > current_cooldown:
            override['finding_cooldown_seconds'] = new_cooldown
            override.setdefault('_reason', '')
            override['_reason'] += f'; findings_failed x{finding_hit} consecutive cycles'

    if override:
        current['tuned_fields'].update(override)
        from datetime import datetime, timezone
        current['last_tune_ts'] = datetime.now(timezone.utc).isoformat()
        _save_tune_state(tune_path, current)

    return override


REPLAY_THRESHOLD_FLOOR = 0.7
REPLAY_THRESHOLD_CEILING = 0.98
REPLAY_SUCCESS_WINDOW = 20
REPLAY_FAILURE_WINDOW = 10
REPLAY_LOWER_STEP = 0.02
REPLAY_RAISE_STEP = 0.05


def adjust_replay_thresholds(
    telemetry_path: Path,
    tune_path: Path,
    floor: float = REPLAY_THRESHOLD_FLOOR,
    ceiling: float = REPLAY_THRESHOLD_CEILING,
    success_window: int = REPLAY_SUCCESS_WINDOW,
    failure_window: int = REPLAY_FAILURE_WINDOW,
    lower_step: float = REPLAY_LOWER_STEP,
    raise_step: float = REPLAY_RAISE_STEP,
) -> Dict[str, float]:
    records = _load_jsonl(telemetry_path, limit=100)
    if not records:
        return {}

    by_rule: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        rule = rec.get("rule", "")
        if not rule:
            continue
        by_rule.setdefault(rule, []).append(rec)

    current = _load_tune_state(tune_path)
    thresholds = current.get("replay_thresholds", {})
    adjustments: Dict[str, float] = {}

    for rule, rule_records in by_rule.items():
        recent = rule_records[-(success_window + failure_window):]
        if not recent:
            continue

        current_threshold = thresholds.get(rule, 0.85)

        consecutive_successes = 0
        for rec in reversed(recent):
            if rec.get("success", False) and rec.get("final_stage", "") != "cascade_exhausted":
                consecutive_successes += 1
            else:
                break

        failures_in_window = 0
        for rec in recent[-failure_window:]:
            if not rec.get("success", True):
                failures_in_window += 1

        new_threshold = current_threshold

        if failures_in_window > 0:
            new_threshold = min(ceiling, current_threshold + raise_step)
        elif consecutive_successes >= success_window:
            new_threshold = max(floor, current_threshold - lower_step)

        if new_threshold != current_threshold:
            thresholds[rule] = round(new_threshold, 4)
            adjustments[rule] = round(new_threshold, 4)

    if adjustments:
        current["replay_thresholds"] = thresholds
        from datetime import datetime, timezone
        current["replay_threshold_ts"] = datetime.now(timezone.utc).isoformat()
        _save_tune_state(tune_path, current)

    return adjustments


def read_tune_overrides(tune_path: Path) -> Dict[str, Any]:
    """Read current tune overrides (call before building CLI args)."""
    state = _load_tune_state(tune_path)
    return state.get('tuned_fields', {})


def reset_tune(tune_path: Path) -> None:
    """Reset tune state (used after successful cycle)."""
    _save_tune_state(tune_path, {'tuned_fields': {}, 'last_tune_ts': None, 'reset_at': None})


def flag_tune_success(tune_path: Path) -> None:
    """Mark that a tuned cycle completed successfully (for gradual recovery)."""
    state = _load_tune_state(tune_path)
    # Reduce overrides gradually on success
    tuned = state.get('tuned_fields', {})
    if 'max_prs_per_run' in tuned:
        tuned['max_prs_per_run'] = min(tuned['max_prs_per_run'] + 1, 2)
        tuned['_recovery_step'] = 'increased'
        if tuned.get('max_prs_per_run', 0) >= 2:
            del tuned['max_prs_per_run']
    if 'finding_cooldown_seconds' in tuned:
        tuned['finding_cooldown_seconds'] = int(tuned['finding_cooldown_seconds'] / 2)
        tuned['_recovery_step'] = 'halved_cooldown'
        if tuned.get('finding_cooldown_seconds', 0) <= 14400:
            del tuned['finding_cooldown_seconds']
    if not tuned or tuned == {'_recovery_step': 'increased'} or tuned == {'_recovery_step': 'halved_cooldown'}:
        state['tuned_fields'] = {}
        state['reset_at'] = state.get('last_tune_ts')
    state['last_tune_ts'] = None  # clear to avoid re-trigger on next read
    _save_tune_state(tune_path, state)
