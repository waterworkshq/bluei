"""escalation.py — QA Agent escalation log writer.

Thin wrapper for review.py's write_escalation import.
Writes to the same state/escalation_log.jsonl that the
sandbox runner's escalation module uses, ensuring a single
unified escalation log regardless of which layer fires first.
"""

from __future__ import annotations

import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Optional

_logger = logging.getLogger(__name__)


DEFAULT_ESCALATION_FILE = Path("state/escalation_log.jsonl")


def write_escalation(
    message: str,
    severity: Literal["info", "warning", "error"] = "error",
    repo: Optional[str] = None,
    escalation_file: Optional[Path] = None,
) -> None:
    """Write an escalation record to escalation_log.jsonl.

    Args:
        message: Human-readable escalation message.
        severity: Severity level (info, warning, error).
        repo: Optional repo name for context.
        escalation_file: Path to escalation log. Defaults to
                        state/escalation_log.jsonl relative to CWD.
    """
    file_path = escalation_file or DEFAULT_ESCALATION_FILE
    record: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "findings": [
            {
                "type": severity,
                "detail": f"[{repo}] {message}" if repo else message,
            }
        ],
        "count": 1,
    }

    try:
        abs_path = file_path.resolve() if file_path else Path.cwd() / DEFAULT_ESCALATION_FILE
        if not abs_path.parent.exists():
            abs_path.parent.mkdir(parents=True, exist_ok=True)
        with open(abs_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError:
        _logger.debug("Failed to write escalation record")


def check_escalation_status(repo_name: str) -> Dict[str, Any]:
    """Read the escalation log and return current escalation status for a repo.

    Scans the unified escalation log (state/escalation_log.jsonl) for
    entries relevant to the given repo name, and returns a summary of
    active escalations.

    Args:
        repo_name: Repository name or slug to filter by.

    Returns:
        Dict with keys:
            - total_escalations: Total escalation records found.
            - active_escalations: Count of recent (last 60 min) escalations.
            - latest: Most recent escalation record (or None).
            - types: Dict mapping escalation type to count.
            - repo: The repo name that was queried.
    """
    escalation_file = DEFAULT_ESCALATION_FILE

    records: list[Dict[str, Any]] = []
    if escalation_file.exists():
        try:
            for line in escalation_file.read_text().strip().splitlines():
                if line.strip():
                    records.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            _logger.debug("Failed to read escalation log")

    # Filter by repo mention
    repo_records: list[Dict[str, Any]] = []
    for r in records:
        findings = r.get('findings', [])
        if not findings:
            continue
        repo_match = False
        for f in findings:
            detail = str(f.get('detail', '') or '')
            if repo_name in detail:
                repo_match = True
                break
        if repo_match:
            repo_records.append(r)

    now = datetime.now(timezone.utc)
    active_cutoff = now.timestamp() - 3600  # 60 minutes ago

    active_count = 0
    for r in repo_records:
        ts_str = r.get('timestamp', '')
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str).timestamp()
                if ts >= active_cutoff:
                    active_count += 1
            except (ValueError, TypeError):
                _logger.debug("Failed to parse escalation timestamp")

    type_counts: Dict[str, int] = {}
    for r in repo_records:
        for f in r.get('findings', []):
            ftype = str(f.get('type', 'unknown'))
            type_counts[ftype] = type_counts.get(ftype, 0) + 1

    latest = repo_records[-1] if repo_records else None

    return {
        'total_escalations': len(repo_records),
        'active_escalations': active_count,
        'latest': latest,
        'types': type_counts,
        'repo': repo_name,
    }
