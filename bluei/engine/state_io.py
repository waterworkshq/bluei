"""Low-level atomic file I/O primitives shared by engine and app layers.

These are pure filesystem operations with no domain logic — safe to depend
on from any layer.

Extracted from bluei/app/state.py to resolve engine→app layering violations
(rec-08). Historically, ``bluei/engine/state.py`` and
``bluei/engine/issue_lifecycle.py`` needed atomic JSON writes but had to
defer-import the implementation from ``bluei/app/state.py``, which inverted
the intended layering (engine should never depend on app). This module sits
in ``bluei/engine/`` so both layers can import it without cycles.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# ——— JSONL rotation ———
# review_events.jsonl and feedback_events.jsonl accumulate indefinitely.
# Rotate by archiving the current file when it exceeds the threshold.
EVENT_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
EVENT_LOG_MAX_LINES = 10_000  # whichever comes first


def atomic_json_write(path: Path, data: Any) -> None:
    """Write JSON data atomically: temp file + rename, same filesystem.

    This avoids leaving a partial/corrupted file if the process is
    interrupted mid-write. The rename is atomic on POSIX systems.

    Uses fcntl.flock(LOCK_EX) to prevent concurrent cycle collisions
    (both cycles writing .tmp + os.replace simultaneously).
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def rotate_jsonl_if_needed(path: Path) -> None:
    """Rotate a JSONL file if it exceeds size or line thresholds."""
    if not path.exists():
        return
    if path.stat().st_size < EVENT_LOG_MAX_BYTES:
        # Check line count only if size threshold not reached
        try:
            with open(path) as f:
                line_count = sum(1 for _ in f)
            if line_count < EVENT_LOG_MAX_LINES:
                return
        except Exception:
            return
    # Rotate: rename current file to .bak, start fresh
    bak_path = path.with_suffix(path.suffix + ".bak")
    try:
        os.replace(str(path), str(bak_path))
    except Exception:
        _logger.debug("Failed to rotate state file")
