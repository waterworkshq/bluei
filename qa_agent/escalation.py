"""escalation.py — QA Agent escalation log writer.

Thin wrapper for review.py's write_escalation import.
Writes to the same state/escalation_log.jsonl that the
sandbox runner's escalation module uses, ensuring a single
unified escalation log regardless of which layer fires first.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Optional


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
        pass
