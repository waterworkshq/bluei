"""clean_prs.py — Stale PR detection and cleanup.

Identifies PRs that should be closed:
- Duplicates: same finding_id, older of multiple open PRs
- Stale: no activity for >48h
- Mergable candidates left open too long

Run as: python -m bluei.engine --run-phase clean-prs --repo owner/repo
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.utils import run_capture
from bluei.engine.state import _append_text


def _fetch_open_prs(repo_slug: str, cwd: Path) -> List[Dict[str, Any]]:
    """Fetch all open PRs with details."""
    rc, out = run_capture(
        [
            'gh', 'pr', 'list',
            '--repo', repo_slug,
            '--state', 'open',
            '--limit', '100',
            '--json', 'number,title,headRefName,createdAt,updatedAt,body,labels',
        ],
        cwd=cwd,
    )
    if rc != 0 or not out.strip():
        return []
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return []


def _close_pr(repo_slug: str, pr_number: int, reason: str, dry_run: bool, cwd: Path) -> bool:
    """Close a PR with a comment."""
    if dry_run:
        return True
    rc, _ = run_capture(
        [
            'gh', 'pr', 'close', str(pr_number),
            '--repo', repo_slug,
            '--comment', reason,
        ],
        cwd=cwd,
    )
    return rc == 0


def _find_duplicate_prs(prs: List[Dict[str, Any]], dedup_window_hours: int = 24) -> List[Dict[str, Any]]:
    """Find duplicate PRs — same title pattern, keep newest.

    Groups PRs by title pattern (stripping finding counts like '9 ruff-c408')
    and marks all but the newest for closure.
    """
    from collections import defaultdict

    by_pattern: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for pr in prs:
        title = pr.get('title', '')
        # Extract rule pattern from titles like "fix: resolve N RULE findings"
        # Also matches solo titles like "fix: resolve RULE in path"
        import re
        m = re.search(r'fix: resolve \d+ (.+) findings', title)
        if m:
            pattern = m.group(1)
        else:
            m = re.search(r'fix: resolve (.+) in ', title)
            pattern = m.group(1) if m else title

        by_pattern[pattern].append(pr)

    to_close: List[Dict[str, Any]] = []
    for pattern, group in by_pattern.items():
        if len(group) < 2:
            continue
        # Sort oldest first — keep the newest (last)
        group.sort(key=lambda p: p.get('createdAt', ''))
        for pr in group[:-1]:
            to_close.append({
                'type': 'duplicate',
                'pr_number': pr['number'],
                'title': pr['title'],
                'pattern': pattern,
                'age_hours': _pr_age_hours(pr),
                'reason': f'Superseded by #{group[-1]["number"]}',
            })

    return to_close


def _pr_age_hours(pr: Dict[str, Any]) -> float:
    created = pr.get('createdAt', '')
    if not created:
        return 0
    try:
        dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except (ValueError, AttributeError):
        return 0


def _find_stale_prs(prs: List[Dict[str, Any]], max_age_hours: int = 48) -> List[Dict[str, Any]]:
    """Find PRs older than max_age_hours with no recent activity.

    Checks updatedAt — if a PR hasn't been touched in max_age_hours,
    it's considered stale.
    """
    to_close: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for pr in prs:
        updated = pr.get('updatedAt', '')
        if not updated:
            continue
        try:
            updated_dt = datetime.fromisoformat(updated.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            continue

        age_hours = (now - updated_dt).total_seconds() / 3600
        if age_hours >= max_age_hours:
            to_close.append({
                'type': 'stale',
                'pr_number': pr['number'],
                'title': pr['title'],
                'age_hours': round(age_hours, 1),
                'reason': f'No activity in {int(age_hours)}h (threshold: {max_age_hours}h)',
            })

    return to_close


def clean_stale_prs(
    repo_slug: str,
    cwd: Path,
    log_file: Path,
    dry_run: bool = True,
    stale_hours: int = 48,
    dedup_window: int = 24,
) -> Dict[str, Any]:
    """Find and close stale/duplicate PRs.

    Returns summary dict with counts.
    """
    _append_text(log_file, f'clean-stale-prs: scanning {repo_slug}')

    prs = _fetch_open_prs(repo_slug, cwd)
    _append_text(log_file, f'clean-stale-prs: found {len(prs)} open PR(s)')

    duplicates = _find_duplicate_prs(prs, dedup_window)
    stale = _find_stale_prs(prs, stale_hours)

    # Remove already-duplicate PRs from stale list (don't double-close)
    dup_numbers = {d['pr_number'] for d in duplicates}
    stale = [s for s in stale if s['pr_number'] not in dup_numbers]

    closed: List[Dict[str, Any]] = []
    for pr in duplicates:
        _append_text(log_file, f'  close-duplicate: #{pr["pr_number"]} {pr["title"]} — {pr["reason"]}')
        if _close_pr(repo_slug, pr['pr_number'], f'Closing duplicate — {pr["reason"]}', dry_run, cwd):
            closed.append(pr)
        else:
            _append_text(log_file, f'  close-failed: #{pr["pr_number"]}')

    for pr in stale:
        _append_text(log_file, f'  close-stale: #{pr["pr_number"]} {pr["title"]} — {pr["reason"]}')
        if _close_pr(repo_slug, pr['pr_number'], f'Closing stale PR — {pr["reason"]}', dry_run, cwd):
            closed.append(pr)
        else:
            _append_text(log_file, f'  close-failed: #{pr["pr_number"]}')

    _append_text(
        log_file,
        f'clean-stale-prs: closed={len(closed)} '
        f'duplicates={len(duplicates)} stale={len(stale)}',
    )

    return {
        'closed': len(closed),
        'duplicates': len(duplicates),
        'stale': len(stale),
        'items': closed,
    }
