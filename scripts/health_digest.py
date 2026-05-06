"""health_digest.py — Compact Telegram digest of QA agent health.

Reads all JSONL state files and produces a formatted summary
suitable for Telegram delivery alongside the watchdog cron.

Reads:
  - state/health_trend.jsonl    (last 50 records)
  - state/escalation_log.jsonl  (last 20 lines)
  - state/review_stats.jsonl    (last record)
  - state/auto_tune.json        (current override state)
  - state/cycle_signals.json    (suppressed rules)
  - state/rebase_stats.jsonl    (last record)

Output: multi-line string, Telegram-friendly (no markdown tables).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _read_jsonl(path: Path, limit: int = 20) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text().strip().splitlines()
        return [json.loads(l) for l in lines[-limit:] if l.strip()]
    except (json.JSONDecodeError, OSError):
        return []


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _trend_summary(records: List[Dict[str, Any]]) -> str:
    """Compact health trend from last N records."""
    if not records:
        return "health trend: no data yet"

    repos: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        repo = r.get('repo', 'unknown')
        repos.setdefault(repo, []).append(r)

    lines: List[str] = []
    for repo, recs in sorted(repos.items()):
        latest = recs[-1]
        score = latest.get('health_score', '?')
        issues = latest.get('open_issues', 0)
        lines.append(f"{repo}: health={score} issues={issues}")

        # Check for recent retry failures
        retry_fails = sum(1 for r in recs[-5:] if r.get('retry_failed', 0) > 0)
        if retry_fails > 0:
            lines.append(f"  ⚠ retry failures: {retry_fails}/5 cycles")

        # Check tune state
        tune_batch = latest.get('tune_batch')
        tune_cooldown = latest.get('tune_cooldown_h')
        if tune_batch is not None or tune_cooldown is not None:
            parts = []
            if tune_batch is not None:
                parts.append(f"batch={tune_batch}")
            if tune_cooldown is not None:
                parts.append(f"cooldown={tune_cooldown}h")
            lines.append(f"  🔧 auto-tune: {' '.join(parts)}")

    return '\n'.join(lines)


def _escalation_summary(records: List[Dict[str, Any]]) -> str:
    """Active escalations."""
    active = []
    for rec in records:
        for finding in rec.get('findings', []):
            if finding.get('type') != 'resolved':
                active.append(finding.get('detail', '?'))

    if not active:
        return ""
    lines = ["escalations:"]
    for detail in active[-5:]:
        lines.append(f"  ⚠ {detail[:120]}")
    return '\n'.join(lines)


def _review_summary(records: List[Dict[str, Any]]) -> str:
    """Latest review cycle stats."""
    if not records:
        return ""
    latest = records[-1]
    parts = []
    for key, label in [
        ('active_prs', 'active'),
        ('blocked_prs', 'blocked'),
        ('retry_failed', 'retry-fail'),
        ('merge_ready', 'merge-ready'),
    ]:
        val = latest.get(key, 0)
        if val > 0:
            parts.append(f"{label}={val}")

    if not parts:
        return "review cycle: idle"
    return "review cycle: " + ' '.join(parts)


def _tune_summary(tune: Dict[str, Any]) -> str:
    """Current auto-tune overrides."""
    tuned = tune.get('tuned_fields', {})
    if not tuned:
        return ""
    parts = []
    if 'max_prs_per_run' in tuned:
        parts.append(f"batch={tuned['max_prs_per_run']}")
    if 'finding_cooldown_seconds' in tuned:
        parts.append(f"cooldown={tuned['finding_cooldown_seconds'] // 3600}h")
    reason = tuned.get('_reason', '')
    line = "auto-tune active: " + ' '.join(parts)
    if reason:
        line += f" ({reason[:80]})"
    return line


def _signal_summary(signals: Dict[str, Any]) -> str:
    """Cross-cycle suppression state."""
    suppressed = signals.get('suppressed_rules', {})
    if not suppressed:
        return ""
    lines = ["suppressed rules:"]
    for rule, info in suppressed.items():
        reason = info.get('reason', '?')[:60]
        expires = info.get('expires_at', '?')[:16]
        lines.append(f"  {rule}: {reason} [expires {expires}]")
    return '\n'.join(lines)


def generate_digest(state_dir: Path) -> str:
    """Generate a compact health digest string."""
    blocks: List[str] = []

    # Trend
    trend = _read_jsonl(state_dir / 'health_trend.jsonl', limit=50)
    trend_str = _trend_summary(trend)
    blocks.append(trend_str)

    # Review
    review = _read_jsonl(state_dir / 'review_stats.jsonl', limit=1)
    review_str = _review_summary(review)
    if review_str:
        blocks.append(review_str)

    # Tune
    tune = _read_json(state_dir / 'auto_tune.json')
    tune_str = _tune_summary(tune)
    if tune_str:
        blocks.append(tune_str)

    # Signals
    signals = _read_json(state_dir / 'cycle_signals.json')
    signal_str = _signal_summary(signals)
    if signal_str:
        blocks.append(signal_str)

    # Escalations
    escalation = _read_jsonl(state_dir / 'escalation_log.jsonl', limit=20)
    escalation_str = _escalation_summary(escalation)
    if escalation_str:
        blocks.append(escalation_str)

    return '\n\n'.join(blocks)


if __name__ == '__main__':
    import sys
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    print(generate_digest(root))
