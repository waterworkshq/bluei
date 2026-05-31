#!/usr/bin/env python3
"""qa_status_digest — Compact Telegram-friendly system health summary.

Reads all 5 JSONL/JSON state files and produces a single formatted digest.
Designed to be called by the watchdog cron and delivered to the Work group.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


# ── Config ──

STATE_DIR = Path(
    os.environ.get(
        "QA_AGENT_WORKSPACE",
        Path(__file__).resolve().parents[1],
    )
) / "state"

MAX_HEALTH_RECORDS = 50


# ── Readers ──


def _read_jsonl(path: Path, limit: int = 100) -> List[Dict[str, Any]]:
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


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _recent_escalations(path: Path, max_age_hours: float = 48) -> List[Dict[str, Any]]:
    records = _read_jsonl(path)
    now = datetime.now(timezone.utc)
    recent: List[Dict[str, Any]] = []
    for rec in reversed(records):
        ts = rec.get("timestamp") or rec.get("ts", "")
        try:
            if (now - datetime.fromisoformat(ts)).total_seconds() < max_age_hours * 3600:
                recent.append(rec)
        except (ValueError, TypeError):
            recent.append(rec)
    return recent


# ── Digest builder ──


def build_digest() -> str:
    lines: List[str] = []

    # 1. Health trend snapshot
    health_records = _read_jsonl(STATE_DIR / "health_trend.jsonl", limit=MAX_HEALTH_RECORDS)
    if health_records:
        latest = health_records[-1]
        health_score = latest.get("vitality", "—")
        lines.append(f"**Vitality:** `{health_score}`")
        if len(health_records) >= 2:
            prev = health_records[-2]
            prev_score = prev.get("vitality", 0)
            if isinstance(health_score, (int, float)) and isinstance(prev_score, (int, float)):
                delta = health_score - prev_score
                if abs(delta) > 0.1:
                    arrow = "↑" if delta > 0 else "↓"
                    lines.append(f"  {arrow} {abs(delta):.1f}")

        repos = Counter(r.get("repo", "?") for r in health_records)
        lines.append(f"  **Repos:** {', '.join(f'{n}({c})' for n, c in repos.most_common())}")
        lines.append(f"  **Records:** {len(health_records)}")
        lines.append("")

    # 2. Latest review status per repo
    review_records = _read_jsonl(STATE_DIR / "review_stats.jsonl", limit=20)
    if review_records:
        lines.append("**Review Cycle:**")
        by_repo: Dict[str, List[Dict]] = {}
        for rec in review_records:
            by_repo.setdefault(rec.get("repo", "?"), []).append(rec)
        for repo, recs in sorted(by_repo.items()):
            r = recs[-1]
            parts = []
            for k, label in [("active_prs", "active"), ("blocked_prs", "blocked"),
                             ("retry_failed", "retry-fail"), ("retry_exhausted", "exhausted"),
                             ("merge_ready", "merge-ready")]:
                v = r.get(k, 0)
                if v:
                    parts.append(f"{label}:{v}")
            status = ", ".join(parts) if parts else "✅ clean"
            lines.append(f"  **{repo}:** {status}")
        lines.append("")

    # 3. Auto-tune state
    tune = _read_json(STATE_DIR / "auto_tune.json")
    tuned = tune.get("tuned_fields", {})
    if any(k for k in tuned if not k.startswith("_")):
        batch = tuned.get("max_prs_per_run")
        cooldown_s = tuned.get("finding_cooldown_seconds")
        reason = tuned.get("_reason", "")
        lines.append("**Auto-Tune:** active")
        if batch is not None:
            lines.append(f"  Batch: {batch}")
        if cooldown_s is not None:
            lines.append(f"  Cooldown: {cooldown_s // 60}m")
        if reason:
            lines.append(f"  Reason: {reason}")
    else:
        lines.append("**Auto-Tune:** default")
    lines.append("")

    # 4. Active suppressions
    signals = _read_json(STATE_DIR / "cycle_signals.json")
    suppressed = signals.get("suppressed_rules", {})
    if suppressed:
        lines.append("**Suppressions:**")
        now = datetime.now(timezone.utc)
        for rule, info in suppressed.items():
            expires = info.get("expires_at", "")
            reason = info.get("reason", rule)
            remaining = ""
            if expires:
                try:
                    rem = datetime.fromisoformat(expires) - now
                    if rem.total_seconds() > 0:
                        remaining = f" (~{int(rem.total_seconds() // 60)}m)"
                except (ValueError, TypeError):
                    pass
            lines.append(f"  `{rule}` — {reason}{remaining}")
        lines.append("")

    # 5. Escalations
    escalations = _recent_escalations(STATE_DIR / "escalation_log.jsonl")
    if escalations:
        lines.append("**Escalations (48h):**")
        for esc in escalations[:5]:
            sev = esc.get("severity", esc.get("type", ""))
            msg = esc.get("message", esc.get("detail", ""))
            repo = esc.get("repo", "")
            icon = "🔴" if sev in ("error", "consecutive_merge_failures") else "🟡"
            prefix = f"[{repo}] " if repo else ""
            lines.append(f"  {icon} {prefix}{msg}")
        if len(escalations) > 5:
            lines.append(f"  … +{len(escalations) - 5} more")
        lines.append("")

    # 6. Status line
    errors = [e for e in escalations if e.get("severity") in ("error",)]
    if errors:
        lines.append(f"⚠️ {len(errors)} escalation(s)")
    elif suppressed:
        lines.append("🟡 Suppressions active")
    else:
        lines.append("🟢 All clear")

    return "\n".join(lines)


if __name__ == "__main__":
    print(build_digest())
