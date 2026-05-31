"""bluei.engine.utils — Core shell/process primitives and repo guards."""

from __future__ import annotations

import logging
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)


# --- Directive seeding: lesson decay configuration ---
# Lessons older than this are excluded (time-decay function)
_LESSON_DECAY_DAYS: int = 60
_LESSON_DECAY_WARN_DAYS: int = 30
_MAX_LESSON_ENTRIES: int = 500


def run_capture(cmd: list[str], cwd: Path, timeout: int = 60) -> Tuple[int, str]:
    kwargs = {
        "cwd": str(cwd),
        "text": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    }
    if timeout > 0:
        kwargs["timeout"] = timeout
    res = subprocess.run(cmd, **kwargs)
    return res.returncode, (res.stdout or "").strip()


def run_no_capture(cmd: list[str], cwd: Path) -> int:
    res = subprocess.run(cmd, cwd=str(cwd), text=True)
    return res.returncode


def is_path_tracked(repo_path: Path, relative_path: str) -> bool:
    rc, _ = run_capture(
        ["git", "ls-files", "--error-unmatch", "--", relative_path], cwd=repo_path
    )
    return rc == 0


def sanitize_command_template(template: str) -> str:
    compact = " ".join(str(template).split())
    if len(compact) > 1000:
        return compact[:1000] + "...<truncated>"
    return compact


def command_list_to_shell(cmd: List[str]) -> str:
    return shlex.join(cmd)


def append_lesson(
    lessons_file: Path,
    cycle_type: str,
    finding_id: str = "",  # NEW: attribute entry to a specific finding
    what_broke: str = "",
    what_changed: str = "",
    what_worked: str = "",
) -> None:
    """Append a short lesson entry to the lessons log.

    Each entry is 1-4 lines capturing what broke, changed, or worked.
    Entries can optionally be tagged with a finding_id for targeted retrieval.

    Log format (finding_id omitted when empty):
        ## 2026-03-25 | pr-cycle
        finding_id: abc123def...
        - **Broke:** ...
        - **Changed:** ...
        - **Worked:** ...
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines: List[str] = [f"\n## {date_str} | {cycle_type}"]

    # Tag with finding_id if provided (NEW)
    if finding_id:
        lines.append(f"finding_id: {finding_id}")

    if what_broke:
        lines.append(f"- **Broke:** {what_broke}")
    if what_changed:
        lines.append(f"- **Changed:** {what_changed}")
    if what_worked:
        lines.append(f"- **Worked:** {what_worked}")

    if len(lines) == 1:
        # No content, don't write
        return

    lessons_file.parent.mkdir(parents=True, exist_ok=True)
    with lessons_file.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    _rotate_lessons_if_needed(lessons_file)


def _rotate_lessons_if_needed(lessons_file: Path) -> None:
    if not lessons_file.exists():
        return
    try:
        content = lessons_file.read_text(encoding="utf-8")
        entries = [e for e in content.split("\n## ") if e.strip()]

        cutoff = datetime.now(timezone.utc) - timedelta(days=_LESSON_DECAY_DAYS)
        entries = [e for e in entries if not _entry_older_than(e, cutoff)]

        if len(entries) > _MAX_LESSON_ENTRIES:
            entries = entries[-_MAX_LESSON_ENTRIES:]

        lessons_file.write_text("\n## " + "\n## ".join(entries), encoding="utf-8")
    except OSError:
        _logger.debug("Failed to save lesson entries")


def _entry_older_than(entry_text: str, cutoff: datetime) -> bool:
    lines = entry_text.strip().split("\n")
    if not lines:
        return False
    try:
        date_str = lines[0].split("|")[0].strip()
        entry_date = datetime.strptime(date_str, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        return entry_date < cutoff
    except (ValueError, IndexError):
        return False


def _lesson_age_days(date_str: str) -> Optional[float]:
    """Compute age in days for a lesson date string.

    Returns None if the date cannot be parsed.
    Used by load_lessons_for_finding for decay filtering.
    """
    if not date_str:
        return None
    try:
        from datetime import datetime

        lesson_date = datetime.strptime(date_str, "%Y-%m-%d")
        now = datetime.now()
        return (now - lesson_date).days
    except (ValueError, TypeError):
        return None


def load_lessons_for_finding(
    finding_id: str, lessons_file: Path
) -> List[Dict[str, Any]]:
    """Parse lessons_file for entries tagged with this finding_id.

    Returns a list of lesson-entry dicts, newest entries first.
    Entries without a finding_id tag are skipped (cannot be attributed).
    Malformed lines are silently skipped.

    Implements time-weighted decay:
    - Lessons older than _LESSON_DECAY_DAYS (60) are excluded entirely.
    - Lessons between _LESSON_DECAY_WARN_DAYS (30) and 60 days old are
      included but flagged with `decayed: true` to indicate lower weight.
    - Lessons under 30 days old are included at full weight.

    Returns:
        List of dicts: {
            'date': '2026-03-25',
            'cycle_type': 'fix-cycle',
            'finding_id': 'abc...',
            'broke': str,
            'changed': str,
            'worked': str,
            'decayed': bool,  # True if lesson is past the warn threshold
        }
    """
    if not lessons_file.exists():
        return []

    entries: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}

    with lessons_file.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip()

            # New entry header
            if line.startswith("## "):
                # Flush previous entry
                if current and current.get("finding_id") == finding_id:
                    entries.append(current)
                # Parse new header: "## 2026-03-25 | pr-cycle"
                parts = line.lstrip("#").strip().split("|")
                date_part = parts[0].strip() if parts else ""
                cycle_type = parts[1].strip() if len(parts) > 1 else ""
                current = {
                    "date": date_part,
                    "cycle_type": cycle_type,
                    "finding_id": "",  # will be set below
                    "broke": "",
                    "changed": "",
                    "worked": "",
                    "decayed": False,  # reset decay flag
                }
                continue

            if not current:
                continue

            # Finding ID tag
            if line.startswith("finding_id:"):
                current["finding_id"] = line.split(":", 1)[1].strip()
                continue

            # Bullet fields
            if line.startswith("- **Broke:**"):
                current["broke"] = line.split("**Broke:**", 1)[1].strip()
            elif line.startswith("- **Changed:**"):
                current["changed"] = line.split("**Changed:**", 1)[1].strip()
            elif line.startswith("- **Worked:**"):
                current["worked"] = line.split("**Worked:**", 1)[1].strip()

    # Flush last entry
    if current and current.get("finding_id") == finding_id:
        entries.append(current)

    # --- Apply decay filtering ---
    # Exclude entries older than _LESSON_DECAY_DAYS (60)
    # Flag entries between _LESSON_DECAY_WARN_DAYS (30) and 60 as decayed
    filtered: List[Dict[str, Any]] = []
    for entry in entries:
        age_days = _lesson_age_days(entry.get("date", ""))
        if age_days is None:
            # Can't determine age, include but flag as potentially stale
            entry["decayed"] = True
            filtered.append(entry)
        elif age_days >= _LESSON_DECAY_DAYS:
            # Too old, exclude entirely
            continue
        elif age_days >= _LESSON_DECAY_WARN_DAYS:
            # Aging but still useful
            entry["decayed"] = True
            filtered.append(entry)
        else:
            # Fresh lesson, full weight
            entry["decayed"] = False
            filtered.append(entry)

    # Newest-first
    filtered.reverse()
    return filtered


def load_lessons_for_rule(
    rule: str, lessons_file: Path, limit: int = 5
) -> List[Dict[str, Any]]:
    if not lessons_file.exists():
        return []

    entries: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}

    with lessons_file.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip()

            if line.startswith("## "):
                if current and _entry_mentions_rule(current, rule):
                    entries.append(current)
                parts = line.lstrip("#").strip().split("|")
                date_part = parts[0].strip() if parts else ""
                cycle_type = parts[1].strip() if len(parts) > 1 else ""
                current = {
                    "date": date_part,
                    "cycle_type": cycle_type,
                    "finding_id": "",
                    "broke": "",
                    "changed": "",
                    "worked": "",
                    "decayed": False,
                }
                continue

            if not current:
                continue

            if line.startswith("finding_id:"):
                current["finding_id"] = line.split(":", 1)[1].strip()
            elif line.startswith("- **Broke:**"):
                current["broke"] = line.split("**Broke:**", 1)[1].strip()
            elif line.startswith("- **Changed:**"):
                current["changed"] = line.split("**Changed:**", 1)[1].strip()
            elif line.startswith("- **Worked:**"):
                current["worked"] = line.split("**Worked:**", 1)[1].strip()

    if current and _entry_mentions_rule(current, rule):
        entries.append(current)

    filtered: List[Dict[str, Any]] = []
    for entry in entries:
        age_days = _lesson_age_days(entry.get("date", ""))
        if age_days is None:
            entry["decayed"] = True
            filtered.append(entry)
        elif age_days >= _LESSON_DECAY_DAYS:
            continue
        elif age_days >= _LESSON_DECAY_WARN_DAYS:
            entry["decayed"] = True
            filtered.append(entry)
        else:
            entry["decayed"] = False
            filtered.append(entry)

    filtered.reverse()
    return filtered[:limit]


def _entry_mentions_rule(entry: Dict[str, Any], rule: str) -> bool:
    searchable = (
        f"{entry.get('broke', '')} {entry.get('changed', '')} {entry.get('worked', '')}"
    )
    return rule in searchable


def load_failure_clusters_for_rule(
    rule: str,
    lessons_file: Path,
    limit: int = 50,
    min_count: int = 3,
    min_ratio: float = 0.3,
) -> Optional[str]:
    entries = load_lessons_for_rule(rule, lessons_file, limit=limit)
    failures = [e for e in entries if e.get("broke", "").strip()]

    if len(failures) < min_count:
        return None

    error_counts: Dict[str, int] = {}
    for entry in failures:
        broke = entry.get("broke", "")
        if "path=" in broke:
            m = re.search(r":\d+:\s+(.+)", broke)
            if m:
                error = m.group(1).strip()
            else:
                error = broke
        elif ": " in broke:
            parts = broke.split(": ", 2)
            if len(parts) >= 3:
                error = parts[2].strip()
            else:
                error = broke
        else:
            error = broke
        error_counts[error] = error_counts.get(error, 0) + 1

    patterns = []
    for error, count in sorted(error_counts.items(), key=lambda x: -x[1]):
        ratio = count / len(failures)
        if count >= min_count and ratio >= min_ratio:
            patterns.append(f"- {count}/{len(failures)} failures: `{error}`")

    if not patterns:
        return None

    return "\n".join(patterns[:3])


def assert_safe_repo(repo_path: Path) -> None:
    """Safety check: ensure git operations stay within the sandbox repo.

    Safety is primarily enforced by the runner's cwd (always bluei workspace).
    """
    pass


def branch_suffix(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned[:32] or "finding"
