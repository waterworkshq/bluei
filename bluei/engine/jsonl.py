"""bluei.engine.jsonl — JSONL file I/O primitives.

Shared by engine and app layers. Consolidates 4 previously-local
reimplementations (auto_tune, dashboard, notify) that had subtly different
semantics around blank-line handling, malformed-line errors, and limit behavior.

These are pure file-I/O utilities with no domain logic — safe to depend on
from any layer.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)


def read_jsonl(
    path: Path,
    *,
    limit: Optional[int] = None,
    skip_errors: bool = True,
    dicts_only: bool = False,
) -> List[Dict[str, Any]]:
    """Read a JSONL file and return a list of records.

    Args:
        path: JSONL file path. Returns [] if the file does not exist.
        limit: If set, return only the last N records (most recent).
            Default None returns all records.
        skip_errors: If True (default), silently skip blank or malformed lines.
            If False, propagate JSONDecodeError / OSError.
        dicts_only: If True, filter the result to dict records only (drops
            arrays/strings/numbers from the result list). Default False.

    Returns:
        List of parsed records. Empty list if file missing or all lines
        skipped.
    """
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        if skip_errors:
            return []
        raise

    records: List[Any] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if skip_errors:
                continue
            raise

    if dicts_only:
        records = [r for r in records if isinstance(r, dict)]

    if limit is not None:
        records = records[-limit:]

    return records


def append_jsonl(path: Path, record: Any, *, sort_keys: bool = True) -> None:
    """Append a single record as a JSON line to a JSONL file.

    Creates parent directories if needed. Atomic at the line level (single
    write() call). For multi-record atomic appends or full-file atomic writes,
    see bluei.engine.state_io.

    Args:
        path: JSONL file path.
        record: JSON-serializable record.
        sort_keys: If True (default), sort dict keys for deterministic output.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=sort_keys) + "\n")
