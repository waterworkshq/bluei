"""cycle_signals.py — Cross-cycle awareness bridge.

Lets cycles share findings about what's working and what's not:

- Review cycle: "finding rule X keeps failing retry — suppress it for N cycles"
- Issue cycle: "check rule X before creating — skip if suppressed"
- After N clean cycles: lift the suppression

This closes the cross-cycle learning loop without coupling the cycles.
"""

from __future__ import annotations

import logging
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.jsonl import read_jsonl

_logger = logging.getLogger(__name__)


# ── Defaults ──

SUPPRESSION_DURATION_CYCLES = 24  # ~12h at 30min review cycle
RETRY_FAILURE_SUPPRESSION_THRESHOLD = 4  # consecutive retry failures before suppressing


class CycleSignalStore:
    """Read/write cross-cycle signals from a shared JSON file."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"suppressed_rules": {}}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"suppressed_rules": {}}

    def _save(self, data: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2) + "\n")
        except OSError:
            _logger.debug("Failed to save cycle signals")

    def suppress_rule(
        self, rule: str, reason: str, duration_cycles: int = SUPPRESSION_DURATION_CYCLES
    ) -> None:
        """Mark a finding rule as suppressed for N cycles."""
        data = self.load()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=30 * duration_cycles)
        ).isoformat()
        data["suppressed_rules"][rule] = {
            "expires_at": expires_at,
            "reason": reason,
            "duration_cycles": duration_cycles,
        }
        self._save(data)

    def is_rule_suppressed(self, rule: str) -> Optional[str]:
        """Check if a finding rule is currently suppressed.

        Returns the reason if suppressed, None if clean.
        Also prunes expired entries on read.
        """
        data = self.load()
        now = datetime.now(timezone.utc)

        # Prune expired
        pruned = False
        for r in list(data["suppressed_rules"].keys()):
            expires = data["suppressed_rules"][r].get("expires_at")
            if expires:
                try:
                    if datetime.fromisoformat(expires) < now:
                        del data["suppressed_rules"][r]
                        pruned = True
                except (ValueError, TypeError):
                    del data["suppressed_rules"][r]
                    pruned = True

        if pruned:
            self._save(data)

        entry = data["suppressed_rules"].get(rule)
        if entry:
            return entry.get("reason", "suppressed")
        return None

    def lift_suppression(self, rule: str) -> None:
        """Manually lift suppression for a rule."""
        data = self.load()
        data["suppressed_rules"].pop(rule, None)
        self._save(data)

    def get_all_suppressed(self) -> Dict[str, Any]:
        """Get all currently suppressed rules and their reasons."""
        # Trigger pruning on read
        data = self.load()
        now = datetime.now(timezone.utc)
        result = {}
        for rule, info in data.get("suppressed_rules", {}).items():
            expires = info.get("expires_at")
            if expires:
                try:
                    if datetime.fromisoformat(expires) < now:
                        continue  # expired, will be pruned on next is_rule_suppressed call
                except (ValueError, TypeError):
                    continue
            result[rule] = info.get("reason", "suppressed")
        return result


def record_retry_failure_pattern(
    store: CycleSignalStore,
    review_stats_file: Path,
    rule: str,
    threshold: int = RETRY_FAILURE_SUPPRESSION_THRESHOLD,
) -> bool:
    """Check review telemetry and suppress rule if retry failure pattern detected.

    Returns True if rule was newly suppressed.
    """
    if store.is_rule_suppressed(rule):
        return False  # already suppressed

    # Count consecutive retry failures for this rule
    # Since review_stats.jsonl doesn't track by rule yet, we use the
    # global retry_failed count as a proxy. Future: add per-rule tracking.
    if not review_stats_file.exists():
        return False

    try:
        records = read_jsonl(review_stats_file, skip_errors=False)
        consecutive = 0
        for rec in reversed(records):
            if rec.get("retry_failed", 0) > 0:
                consecutive += 1
            else:
                break
        if consecutive >= threshold:
            store.suppress_rule(rule, f"retry_failed x{consecutive} consecutive cycles")
            return True
    except (json.JSONDecodeError, OSError):
        _logger.debug("Failed to read cycle signal history")

    return False
