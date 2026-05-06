"""Tests for the contextual fix migration engine."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'core'))

from sandbox_local_runner.migrate_context import (
    reclassify_findings,
    dry_run_report,
)
from sandbox_local_runner.reforge import RefactorClass


def _make_findings(data: list) -> Path:
    """Create a temporary findings.json file."""
    p = Path(tempfile.mktemp(suffix='.json'))
    with open(p, 'w') as f:
        json.dump(data, f)
    return p


def _sample_finding(rule, path, safe=False, refactor_class=None):
    return {
        'finding_id': f'test-{rule}',
        'repo': 'test-repo',
        'path': path,
        'line': 10,
        'rule': rule,
        'snippet': f'Sample {rule} issue',
        'confidence': 0.8,
        'quick_win': False,
        'safe_to_autofix': safe,
        'refactor_class': refactor_class,
    }


class TestReclassifyFindings:
    """Tests for reclassify_findings()."""

    def test_reclassify_updates_refactor_class(self):
        """Reclassify updates refactor_class for contextually fixable findings."""
        findings = [
            _sample_finding('ruff-b904', 'zerver/middleware.py', safe=False),
        ]
        findings_path = _make_findings(findings)

        changes = reclassify_findings(findings_path)

        assert 'test-ruff-b904' in changes
        assert changes['test-ruff-b904']['new_class'] == RefactorClass.CONTEXTUAL_FIX.value

        # Verify the file was updated
        with open(findings_path) as f:
            updated = json.load(f)
        assert updated[0]['refactor_class'] == RefactorClass.CONTEXTUAL_FIX.value

    def test_reclassify_updates_safe_to_autofix(self):
        """Reclassify updates safe_to_autofix based on new classification."""
        findings = [
            _sample_finding('ruff-b007', 'analytics/lib/fixtures.py', safe=False),
        ]
        findings_path = _make_findings(findings)

        changes = reclassify_findings(findings_path)

        # b007 in fixtures is deterministic_safe → classified as SIMPLE_FIX
        assert changes['test-ruff-b007']['new_class'] == RefactorClass.SIMPLE_FIX.value
        with open(findings_path) as f:
            updated = json.load(f)
        assert updated[0]['safe_to_autofix'] is True  # SIMPLE_FIX → safe

    def test_reclassify_is_idempotent(self):
        """Running reclassify twice produces no changes on second run."""
        findings = [
            _sample_finding('ruff-b904', 'zerver/middleware.py', safe=False),
        ]
        findings_path = _make_findings(findings)

        # First run
        changes1 = reclassify_findings(findings_path)
        assert len(changes1) > 0

        # Second run — no changes expected
        changes2 = reclassify_findings(findings_path)
        assert len(changes2) == 0

    def test_reclassify_unknown_rule_to_claude(self):
        """Unknown rules are classified as CLAUDE_FIX."""
        findings = [
            _sample_finding('some-unknown-rule', 'app/main.py', safe=True),
        ]
        findings_path = _make_findings(findings)

        changes = reclassify_findings(findings_path)
        assert len(changes) == 1
        assert changes['test-some-unknown-rule']['new_class'] == RefactorClass.CLAUDE_FIX.value


class TestDryRunReport:
    """Tests for dry_run_report()."""

    def test_dry_run_shows_summary(self):
        """Dry run report includes total and change counts."""
        findings = [
            _sample_finding('some-rule', 'app/main.py', safe=True),
        ]
        findings_path = _make_findings(findings)

        report = dry_run_report(findings_path)

        assert 'Total findings: 1' in report
        # Unknown rules are classified as CLAUDE_FIX, so not unchanged
        assert 'New CLAUDE_FIX: 1' in report

    def test_dry_run_shows_changes(self):
        """Dry run report lists affected findings."""
        findings = [
            _sample_finding('ruff-b904', 'zerver/middleware.py', safe=False),
        ]
        findings_path = _make_findings(findings)

        report = dry_run_report(findings_path)

        assert 'test-ruff-b904' in report
        assert 'CONTEXTUAL_FIX' in report

    def test_dry_run_does_not_modify_file(self):
        """Dry run does not change the findings file."""
        findings = [
            _sample_finding('ruff-b904', 'zerver/middleware.py', safe=False),
        ]
        findings_path = _make_findings(findings)

        with open(findings_path) as f:
            before = f.read()

        dry_run_report(findings_path)

        with open(findings_path) as f:
            after = f.read()

        assert before == after

    def test_dry_run_handles_empty_findings(self):
        """Dry run handles empty findings list."""
        findings_path = _make_findings([])
        report = dry_run_report(findings_path)
        assert 'Total findings: 0' in report
