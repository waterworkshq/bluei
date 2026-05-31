"""migrate_context.py — Bulk re-classification and issue state migration.

Reads existing findings and issues, re-runs classify_finding() with the new
context rules, and updates the stored state so the pr-cycle can pick up
previously "not fixable" findings that are now contextually fixable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Any

from .reforge import classify_finding, RefactorClass

if TYPE_CHECKING:
    from .models import Finding


def _load_json(path: Path) -> Any:
    with open(path, 'r') as f:
        return json.load(f)


def _save_json(path: Path, data: Any) -> None:
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def reclassify_findings(findings_file: Path) -> Dict[str, Dict]:
    """Re-run classify_finding() on all findings and return a change summary.

    Returns a dict of {finding_id: {old_class, new_class, old_safe, new_safe}}
    for findings whose classification changed.
    """
    findings = _load_json(findings_file)
    changes = {}

    for finding_data in findings:
        fid = finding_data.get('finding_id')
        if not fid:
            continue

        old_class = finding_data.get('refactor_class')
        old_safe = finding_data.get('safe_to_autofix')

        # Reconstruct Finding for classification
        from .models import Finding
        finding = Finding(
            finding_id=fid,
            repo=finding_data.get('repo', ''),
            path=finding_data.get('path', ''),
            line=finding_data.get('line', 0),
            rule=finding_data.get('rule', ''),
            snippet=finding_data.get('snippet', ''),
            confidence=finding_data.get('confidence', 0.0),
            quick_win=finding_data.get('quick_win', False),
            safe_to_autofix=old_safe,
        )

        new_class = classify_finding(finding)

        # Determine new safe_to_autofix based on new class
        if new_class == RefactorClass.CONTEXTUAL_FIX:
            # Contextual: keep original safe_to_autofix but mark as contextually fixable
            new_safe = old_safe
        elif new_class == RefactorClass.SIMPLE_FIX:
            new_safe = True
        elif new_class == RefactorClass.CLAUDE_FIX:
            new_safe = False
        elif new_class == RefactorClass.REFACTOR_CLASS:
            new_safe = False
        else:
            new_safe = old_safe

        if old_class != new_class.value or old_safe != new_safe:
            changes[fid] = {
                'old_class': old_class,
                'new_class': new_class.value,
                'old_safe': old_safe,
                'new_safe': new_safe,
                'rule': finding.rule,
                'path': finding.path,
            }
            # Update the finding data in place
            finding_data['refactor_class'] = new_class.value
            finding_data['safe_to_autofix'] = new_safe

    # Save updated findings
    _save_json(findings_file, findings)

    return changes


def dry_run_report(findings_file: Path) -> str:
    """Generate a preview report of what would change."""
    findings = _load_json(findings_file)
    stats = {
        'total': 0,
        'contextual_new': 0,
        'simple_new': 0,
        'refactor_new': 0,
        'claude_new': 0,
        'unchanged': 0,
    }
    changes = []

    for finding_data in findings:
        stats['total'] += 1
        fid = finding_data.get('finding_id')
        if not fid:
            continue

        old_class = finding_data.get('refactor_class')
        old_safe = finding_data.get('safe_to_autofix')

        from .models import Finding
        finding = Finding(
            finding_id=fid,
            repo=finding_data.get('repo', ''),
            path=finding_data.get('path', ''),
            line=finding_data.get('line', 0),
            rule=finding_data.get('rule', ''),
            snippet=finding_data.get('snippet', ''),
            confidence=finding_data.get('confidence', 0.0),
            quick_win=finding_data.get('quick_win', False),
            safe_to_autofix=old_safe,
        )

        new_class = classify_finding(finding)

        if old_class != new_class.value:
            changes.append(
                f"  {fid}: {old_class or 'none'} → {new_class.value} "
                f"(rule={finding.rule}, path={finding.path})"
            )
            if new_class == RefactorClass.CONTEXTUAL_FIX:
                stats['contextual_new'] += 1
            elif new_class == RefactorClass.SIMPLE_FIX:
                stats['simple_new'] += 1
            elif new_class == RefactorClass.CLAUDE_FIX:
                stats['claude_new'] += 1
            elif new_class == RefactorClass.REFACTOR_CLASS:
                stats['refactor_new'] += 1
        else:
            stats['unchanged'] += 1

    lines = [
        "Contextual Fix Migration — Dry Run Report",
        "=" * 50,
        f"Total findings: {stats['total']}",
        f"Unchanged: {stats['unchanged']}",
        f"  New CONTEXTUAL_FIX: {stats['contextual_new']}",
        f"  New SIMPLE_FIX: {stats['simple_new']}",
        f"  New CLAUDE_FIX: {stats['claude_new']}",
        f"  New REFACTOR_CLASS: {stats['refactor_new']}",
        "",
    ]
    if changes:
        lines.append("Changes:")
        lines.extend(changes)
    else:
        lines.append("No changes detected.")

    return '\n'.join(lines)
