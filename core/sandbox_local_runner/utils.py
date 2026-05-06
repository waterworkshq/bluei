"""sandbox_local_runner.utils — Core shell/process primitives and repo guards."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .constants import (
    BLOCKED_REPOS,
    WORKSPACE,
)


def run_capture(cmd: list[str], cwd: Path, timeout: int = 0) -> Tuple[int, str]:
    kwargs = {'cwd': str(cwd), 'text': True, 'stdout': subprocess.PIPE, 'stderr': subprocess.STDOUT}
    if timeout > 0:
        kwargs['timeout'] = timeout
    res = subprocess.run(cmd, **kwargs)
    return res.returncode, (res.stdout or '').strip()


def run_no_capture(cmd: list[str], cwd: Path) -> int:
    res = subprocess.run(cmd, cwd=str(cwd), text=True)
    return res.returncode


def is_path_tracked(repo_path: Path, relative_path: str) -> bool:
    rc, _ = run_capture(['git', 'ls-files', '--error-unmatch', '--', relative_path], cwd=repo_path)
    return rc == 0


def sanitize_command_template(template: str) -> str:
    compact = ' '.join(str(template).split())
    if len(compact) > 1000:
        return compact[:1000] + '...<truncated>'
    return compact


def command_list_to_shell(cmd: List[str]) -> str:
    return shlex.join(cmd)


def append_lesson(
    lessons_file: Path,
    cycle_type: str,
    finding_id: str = '',        # NEW: attribute entry to a specific finding
    what_broke: str = '',
    what_changed: str = '',
    what_worked: str = '',
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
    date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
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
    with lessons_file.open('a', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def load_lessons_for_finding(finding_id: str, lessons_file: Path) -> List[Dict[str, Any]]:
    """Parse lessons_file for entries tagged with this finding_id.

    Returns a list of lesson-entry dicts, newest entries first.
    Entries without a finding_id tag are skipped (cannot be attributed).
    Malformed lines are silently skipped.

    Returns:
        List of dicts: {
            'date': '2026-03-25',
            'cycle_type': 'fix-cycle',
            'finding_id': 'abc...',
            'broke': str,
            'changed': str,
            'worked': str,
        }
    """
    if not lessons_file.exists():
        return []

    entries: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}

    with lessons_file.open('r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.rstrip()

            # New entry header
            if line.startswith('## '):
                # Flush previous entry
                if current and current.get('finding_id') == finding_id:
                    entries.append(current)
                # Parse new header: "## 2026-03-25 | pr-cycle"
                parts = line.lstrip('#').strip().split('|')
                date_part = parts[0].strip() if parts else ''
                cycle_type = parts[1].strip() if len(parts) > 1 else ''
                current = {
                    'date': date_part,
                    'cycle_type': cycle_type,
                    'finding_id': '',   # will be set below
                    'broke': '',
                    'changed': '',
                    'worked': '',
                }
                continue

            if not current:
                continue

            # Finding ID tag
            if line.startswith('finding_id:'):
                current['finding_id'] = line.split(':', 1)[1].strip()
                continue

            # Bullet fields
            if line.startswith('- **Broke:**'):
                current['broke'] = line.split('**Broke:**', 1)[1].strip()
            elif line.startswith('- **Changed:**'):
                current['changed'] = line.split('**Changed:**', 1)[1].strip()
            elif line.startswith('- **Worked:**'):
                current['worked'] = line.split('**Worked:**', 1)[1].strip()

    # Flush last entry
    if current and current.get('finding_id') == finding_id:
        entries.append(current)

    # Newest-first
    entries.reverse()
    return entries


def load_recent_lessons(lessons_file: Path, limit: int = 20) -> List[Dict[str, Any]]:
    """Return the most recent `limit` lesson entries, newest-first.

    Unlike load_lessons_for_finding, this parses ALL entries regardless of
    finding_id. Entries without a finding_id tag have finding_id=''.

    Returns:
        List of dicts (same shape as load_lessons_for_finding).
    """
    if not lessons_file.exists():
        return []
    if limit <= 0:
        return []

    entries: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}

    with lessons_file.open('r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.rstrip()

            if line.startswith('## '):
                # Flush previous entry
                if current:
                    entries.append(current)
                    if len(entries) >= limit:
                        break  # Safe: current was the OLD entry, new entry is not yet set
                # Set current to the NEW entry from this ## header BEFORE checking break next time
                parts = line.lstrip('#').strip().split('|')
                date_part = parts[0].strip() if parts else ''
                cycle_type = parts[1].strip() if len(parts) > 1 else ''
                current = {
                    'date': date_part,
                    'cycle_type': cycle_type,
                    'finding_id': '',
                    'broke': '',
                    'changed': '',
                    'worked': '',
                }
                continue
                parts = line.lstrip('#').strip().split('|')
                date_part = parts[0].strip() if parts else ''
                cycle_type = parts[1].strip() if len(parts) > 1 else ''
                current = {
                    'date': date_part,
                    'cycle_type': cycle_type,
                    'finding_id': '',
                    'broke': '',
                    'changed': '',
                    'worked': '',
                }
                continue

            if not current:
                continue

            if line.startswith('finding_id:'):
                current['finding_id'] = line.split(':', 1)[1].strip()
            elif line.startswith('- **Broke:**'):
                current['broke'] = line.split('**Broke:**', 1)[1].strip()
            elif line.startswith('- **Changed:**'):
                current['changed'] = line.split('**Changed:**', 1)[1].strip()
            elif line.startswith('- **Worked:**'):
                current['worked'] = line.split('**Worked:**', 1)[1].strip()

    if current and len(entries) < limit:
        entries.append(current)

    entries.reverse()
    return entries


def assert_safe_repo(repo_path: Path) -> None:
    """Safety check: ensure git operations stay within the sandbox repo.

    Safety is primarily enforced by the runner's cwd (always qa-agent workspace).
    This check blocks explicitly blocked repos as a secondary guard.
    """
    rp = repo_path.resolve()
    if rp.name in BLOCKED_REPOS:
        raise RuntimeError(f'repo-path is blocked for safety: {rp.name}')


def branch_suffix(value: str) -> str:
    cleaned = re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')
    return cleaned[:32] or 'finding'
