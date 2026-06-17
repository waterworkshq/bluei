"""Shared primitives for ``bluei.engine.gh``.

Holds ``gh_json`` (the retry/backoff hub around :func:`run_capture`) and
``finding_dedupe_marker`` (the dedup helper used by issue/PR ops).

Imported by :mod:`bluei.engine.gh` (the facade) and, in later decomposition
steps, by the per-concern submodules.

Patch-surface note (plan Risk #1, section 4.1): ``gh_json`` resolves
``run_capture`` and ``time`` through the ``_facade`` reference (the parent
package) rather than importing them into this module's globals.  This is what
keeps ``patch("bluei.engine.gh.run_capture")`` and
``patch("bluei.engine.gh.time.sleep")`` reaching ``gh_json``'s call sites
after the move -- the patch replaces the attribute on the facade module, and
``_facade.<name>`` looks it up at call time.  The retry/backoff behaviour
(3 retries, 1s/2s/4s delays) is unchanged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

# Call-time resolution of run_capture / time through the facade.  ``import ...
# as _facade`` binds to the (possibly still-initializing) package module object
# that Python has already inserted into sys.modules; attribute access happens
# later at call time, by which point the facade is fully populated.
import bluei.engine.gh as _facade

logger = logging.getLogger(__name__)


def finding_dedupe_marker(finding_id: str) -> str:
    return f"[finding_id:{finding_id}]"


def gh_json(cmd: list[str], cwd: Path) -> Optional[Any]:
    """Run a ``gh`` CLI command and parse its JSON output.

    Retries up to 3 times with exponential backoff (1s, 2s, 4s) on
    non-zero exit codes.  JSON parse failures are never retried.

    Args:
        cmd: Full command list (e.g. ``['gh', 'issue', 'list', ...]``).
        cwd: Working directory for the subprocess.

    Returns:
        Parsed JSON object, or ``None`` on exhaustion or parse failure.
    """
    MAX_RETRIES = 3
    rc = -1
    for attempt in range(MAX_RETRIES + 1):
        rc, out = _facade.run_capture(cmd, cwd=cwd)
        if rc == 0:
            try:
                return json.loads(out)
            except Exception:
                return None
        if attempt < MAX_RETRIES:
            delay = 2**attempt
            logger.warning(
                "gh_json: attempt %d/%d failed rc=%d — retrying in %ds  cmd=%s",
                attempt + 1,
                MAX_RETRIES + 1,
                rc,
                delay,
                cmd[:4],
            )
            _facade.time.sleep(delay)
    logger.error(
        "gh_json: all %d attempts failed rc=%d  cmd=%s",
        MAX_RETRIES + 1,
        rc,
        cmd[:4],
    )
    return None
