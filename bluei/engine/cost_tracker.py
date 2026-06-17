"""Cost tracking for model API invocations per cycle.

Tracks per-invocation cost using known model rates, logs every invocation
to a JSONL file, and supports soft-warn / hard-limit thresholds so a cycle
can stop early when over budget.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from bluei.engine.jsonl import read_jsonl

logger = logging.getLogger(__name__)

# Known model per-token rates (USD per 1K tokens).
# Extend this dict as new models are added.
MODEL_RATES: Dict[str, Dict[str, float]] = {
    "claude-sonnet-4": {
        "input_per_1k": 0.003,
        "output_per_1k": 0.015,
    },
    "claude-sonnet-4-20250514": {
        "input_per_1k": 0.003,
        "output_per_1k": 0.015,
    },
    "claude-3-5-sonnet-20241022": {
        "input_per_1k": 0.003,
        "output_per_1k": 0.015,
    },
    "claude-opus-4": {
        "input_per_1k": 0.015,
        "output_per_1k": 0.075,
    },
    "claude-3-haiku-20240307": {
        "input_per_1k": 0.00025,
        "output_per_1k": 0.00125,
    },
    "claude-3-opus-20240229": {
        "input_per_1k": 0.015,
        "output_per_1k": 0.075,
    },
    "gpt-4o": {
        "input_per_1k": 0.005,
        "output_per_1k": 0.015,
    },
    "gpt-4o-mini": {
        "input_per_1k": 0.00015,
        "output_per_1k": 0.0006,
    },
    # Default / unknown model fallback rate
    "default": {
        "input_per_1k": 0.003,
        "output_per_1k": 0.015,
    },
}

_DEFAULT_LOG_FILENAME = "cost_log.jsonl"


class CostTracker:
    """Tracks model API costs for one cycle.

    Logs every invocation to a JSONL file and exposes thresholds so callers
    can warn (soft) or stop (hard) before continuing to invoke expensive models.
    """

    def __init__(
        self,
        log_path: Path,
        soft_warn: float = 2.0,
        hard_limit: float = 10.0,
    ) -> None:
        """Initialize the tracker.

        Args:
            log_path: Path to the JSONL log file (created under state directory).
            soft_warn: Float USD threshold at which warned() becomes True.
            hard_limit: Float USD threshold at which exceeded_limit() becomes True.
        """
        self._log_path = log_path
        self._soft_warn = soft_warn
        self._hard_limit = hard_limit
        self._cycle_cost: float = 0.0
        self._warned_flag: bool = False
        self._exceeded_flag: bool = False
        self._invocations: int = 0

        # Ensure parent directory exists
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Public API ──────────────────────────────────────────────────────

    def record_invocation(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> float:
        """Record one model invocation and return its computed cost.

        Args:
            model: Model identifier (e.g. ``"claude-sonnet-4"``).  Falls back
                   to the ``"default"`` entry in MODEL_RATES if not found.
            input_tokens: Number of input (prompt) tokens sent.
            output_tokens: Number of output (completion) tokens received.

        Returns:
            The computed cost for this single invocation in USD.
        """
        rates = MODEL_RATES.get(model) or MODEL_RATES["default"]
        input_rate = rates.get("input_per_1k", MODEL_RATES["default"]["input_per_1k"])
        output_rate = rates.get(
            "output_per_1k", MODEL_RATES["default"]["output_per_1k"]
        )

        input_cost = input_tokens * input_rate / 1000.0
        output_cost = output_tokens * output_rate / 1000.0
        invocation_cost = input_cost + output_cost

        self._cycle_cost += invocation_cost
        self._invocations += 1

        # Persist to log
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": round(invocation_cost, 6),
            "cycle_total_so_far": round(self._cycle_cost, 6),
        }
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError as exc:
            logger.warning("cost_tracker: failed to write %s — %s", self._log_path, exc)

        # Evaluate thresholds
        if self._cycle_cost >= self._hard_limit and not self._exceeded_flag:
            self._exceeded_flag = True
            logger.warning(
                "cost_tracker: HARD LIMIT reached — $%.4f (limit=$%.2f)",
                self._cycle_cost,
                self._hard_limit,
            )
        if self._cycle_cost >= self._soft_warn and not self._warned_flag:
            self._warned_flag = True
            logger.info(
                "cost_tracker: soft warning at $%.4f (threshold=$%.2f)",
                self._cycle_cost,
                self._soft_warn,
            )

        return invocation_cost

    def record_pattern_replay_savings(
        self,
        model: str,
        saved_cost: float,
        pattern_id: str,
        rule: str,
    ) -> None:
        """Record a cost savings from a successful pattern replay.

        Appends a savings record to the cost log but does NOT affect
        ``cycle_total()``, soft-warn, or hard-limit thresholds.

        Args:
            model: Model identifier whose call was avoided.
            saved_cost: Estimated cost avoided (USD), computed using
                        the same MODEL_RATES as ``record_invocation()``.
            pattern_id: The replayed pattern's identifier.
            rule: The rule that the pattern matches.
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "pattern_replay_savings",
            "model": model,
            "saved_cost": round(saved_cost, 6),
            "pattern_id": pattern_id,
            "rule": rule,
        }
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError as exc:
            logger.warning(
                "cost_tracker: failed to write savings %s — %s", self._log_path, exc
            )

    def estimate_invocation_cost(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> float:
        """Estimate cost for a model invocation without recording it.

        Uses the same MODEL_RATES as ``record_invocation()``.
        """
        rates = MODEL_RATES.get(model) or MODEL_RATES["default"]
        input_rate = rates.get("input_per_1k", MODEL_RATES["default"]["input_per_1k"])
        output_rate = rates.get(
            "output_per_1k", MODEL_RATES["default"]["output_per_1k"]
        )
        input_cost = input_tokens * input_rate / 1000.0
        output_cost = output_tokens * output_rate / 1000.0
        return input_cost + output_cost

    def cycle_total(self) -> float:
        """Return the total cost accumulated for the current cycle."""
        return self._cycle_cost

    def warned(self) -> bool:
        """Return True if the soft warning threshold has been reached."""
        return self._warned_flag

    def exceeded_limit(self) -> bool:
        """Return True if the hard limit has been reached.

        When True, callers should skip further model invocations for the
        remainder of the cycle.
        """
        return self._exceeded_flag

    # ── Utilities ───────────────────────────────────────────────────────

    @classmethod
    def load_history(
        cls,
        log_path: Path,
    ) -> list[dict]:
        """Load all cost log entries for read-only inspection."""
        return read_jsonl(log_path)

    @classmethod
    def summary(
        cls,
        log_path: Path,
    ) -> dict:
        """Return a summary dict of all cost history in *log_path*.

        Keys: ``total_cost``, ``total_invocations``, ``per_model``,
        ``earliest``, ``latest``.
        """
        entries = cls.load_history(log_path)
        if not entries:
            return {
                "total_cost": 0.0,
                "total_invocations": 0,
                "per_model": {},
                "earliest": None,
                "latest": None,
            }

        total = 0.0
        per_model: Dict[str, dict] = {}
        savings_total = 0.0
        savings_count = 0
        for e in entries:
            if e.get("type") == "pattern_replay_savings":
                savings_total += e.get("saved_cost", 0)
                savings_count += 1
                continue
            total += e.get("cost", 0)
            model = e.get("model", "unknown")
            rec = per_model.setdefault(model, {"count": 0, "cost": 0.0})
            rec["count"] += 1
            rec["cost"] += e.get("cost", 0)

        result: dict = {
            "total_cost": round(total, 6),
            "total_invocations": len(entries) - savings_count,
            "per_model": {
                m: {"count": v["count"], "cost": round(v["cost"], 6)}
                for m, v in per_model.items()
            },
            "earliest": entries[0].get("timestamp"),
            "latest": entries[-1].get("timestamp"),
        }
        if savings_count > 0:
            result["pattern_replay_savings"] = {
                "count": savings_count,
                "total_saved": round(savings_total, 6),
            }
        return result
