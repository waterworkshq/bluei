#!/usr/bin/env python3
"""Regression tests for routing structural refactors into the review lane."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'core'))

from sandbox_local_runner.models import Finding


def make_finding(path: str, finding_id: str = 'ref-1') -> Finding:
    return Finding(
        finding_id=finding_id,
        repo='test-repo',
        path=path,
        line=10,
        rule='xo-max-lines',
        snippet='large file',
        confidence=0.95,
        quick_win=False,
        safe_to_autofix=False,
    )


def test_structural_refactor_issue_becomes_non_actionable_review_item():
    from sandbox_local_runner.orchestrator import route_findings_with_intent, ensure_issue_for_finding, find_issue_for_finding, set_issue_status
    from sandbox_local_runner.state import count_actionable_issues, NON_ACTIONABLE_ISSUE_STATUSES

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        findings_file = tmp / 'findings.jsonl'
        repo_path = tmp / 'repo'
        repo_path.mkdir()
        target = repo_path / 'src' / 'huge.ts'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('\n'.join(f'line {i}' for i in range(5001)), encoding='utf-8')

        finding = make_finding('src/huge.ts')
        findings_file.write_text(
            '{"confidence": 0.95, "finding_id": "ref-1", "line": 10, "path": "src/huge.ts", "quick_win": false, "repo": "test-repo", "rule": "xo-max-lines", "safe_to_autofix": false, "snippet": "large file"}\n',
            encoding='utf-8',
        )
        issues_data = {'issues': []}

        routed = route_findings_with_intent(
            [finding],
            confidence_threshold=0.8,
            findings_file=findings_file,
            worktree_path=repo_path,
        )
        routed_item = routed['refactor_queue'][0]

        issue = ensure_issue_for_finding(issues_data, finding, confidence_threshold=0.8)
        assert issue is not None
        issue.setdefault('refactor', {})['queue_work_id'] = routed_item['queued_work_id']
        set_issue_status(issue, 'needs-human-refactor-review', routed_item['reason'])

        stored = find_issue_for_finding(issues_data, 'ref-1')
        assert stored is not None
        assert stored['status'] in NON_ACTIONABLE_ISSUE_STATUSES
        assert stored['status'] == 'needs-human-refactor-review'
        assert count_actionable_issues(issues_data) == 0
