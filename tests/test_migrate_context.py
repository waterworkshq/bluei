"""Tests for the contextual fix migration engine."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "core"))

from bluei.engine.migrate_context import (
    reclassify_findings,
    dry_run_report,
)
from bluei.engine.reforge import RefactorClass


def _make_findings(data: list) -> Path:
    """Create a temporary findings.json file."""
    p = Path(tempfile.mktemp(suffix=".json"))
    with open(p, "w") as f:
        json.dump(data, f)
    return p


def _sample_finding(rule, path, safe=False, refactor_class=None):
    return {
        "finding_id": f"test-{rule}",
        "repo": "test-repo",
        "path": path,
        "line": 10,
        "rule": rule,
        "snippet": f"Sample {rule} issue",
        "confidence": 0.8,
        "quick_win": False,
        "safe_to_autofix": safe,
        "refactor_class": refactor_class,
    }


class TestReclassifyFindings:
    """Tests for reclassify_findings()."""

    def test_reclassify_updates_refactor_class(self):
        """Reclassify updates refactor_class for contextually fixable findings."""
        findings = [
            _sample_finding("ruff-b904", "zerver/middleware.py", safe=False),
        ]
        findings_path = _make_findings(findings)

        changes = reclassify_findings(findings_path)

        assert "test-ruff-b904" in changes
        assert (
            changes["test-ruff-b904"]["new_class"] == RefactorClass.CONTEXTUAL_FIX.value
        )

        # Verify the file was updated
        with open(findings_path) as f:
            updated = json.load(f)
        assert updated[0]["refactor_class"] == RefactorClass.CONTEXTUAL_FIX.value

    def test_reclassify_updates_safe_to_autofix(self):
        """Reclassify updates safe_to_autofix based on new classification."""
        findings = [
            _sample_finding("ruff-b007", "analytics/lib/fixtures.py", safe=False),
        ]
        findings_path = _make_findings(findings)

        changes = reclassify_findings(findings_path)

        # b007 in fixtures is deterministic_safe → classified as SIMPLE_FIX
        assert changes["test-ruff-b007"]["new_class"] == RefactorClass.SIMPLE_FIX.value
        with open(findings_path) as f:
            updated = json.load(f)
        assert updated[0]["safe_to_autofix"] is True  # SIMPLE_FIX → safe

    def test_reclassify_is_idempotent(self):
        """Running reclassify twice produces no changes on second run."""
        findings = [
            _sample_finding("ruff-b904", "zerver/middleware.py", safe=False),
        ]
        findings_path = _make_findings(findings)

        # First run
        changes1 = reclassify_findings(findings_path)
        assert len(changes1) > 0

        # Second run — no changes expected
        changes2 = reclassify_findings(findings_path)
        assert len(changes2) == 0

    def test_reclassify_unknown_rule_to_cascade(self):
        """Unknown rules are classified as CASCADE_FIX."""
        findings = [
            _sample_finding("some-unknown-rule", "app/main.py", safe=True),
        ]
        findings_path = _make_findings(findings)

        changes = reclassify_findings(findings_path)
        assert len(changes) == 1
        assert (
            changes["test-some-unknown-rule"]["new_class"]
            == RefactorClass.CASCADE_FIX.value
        )


class TestDryRunReport:
    """Tests for dry_run_report()."""

    def test_dry_run_shows_summary(self):
        """Dry run report includes total and change counts."""
        findings = [
            _sample_finding("some-rule", "app/main.py", safe=True),
        ]
        findings_path = _make_findings(findings)

        report = dry_run_report(findings_path)

        assert "Total findings: 1" in report
        assert "New CASCADE_FIX: 1" in report

    def test_dry_run_shows_changes(self):
        """Dry run report lists affected findings."""
        findings = [
            _sample_finding("ruff-b904", "zerver/middleware.py", safe=False),
        ]
        findings_path = _make_findings(findings)

        report = dry_run_report(findings_path)

        assert "test-ruff-b904" in report
        assert "CONTEXTUAL_FIX" in report

    def test_dry_run_does_not_modify_file(self):
        """Dry run does not change the findings file."""
        findings = [
            _sample_finding("ruff-b904", "zerver/middleware.py", safe=False),
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
        assert "Total findings: 0" in report


# ── Extracted from test_small_tier23_remaining.py ──
# Additional migrate_context edge cases


class TestMigrateContextSkipNoFindingId:
    def test_reclassify_skips_finding_without_id(self, tmp_path):
        data = [{"rule": "ruff-test", "path": "a.py", "line": 1}]
        findings_path = _make_findings(data)
        changes = reclassify_findings(findings_path)
        assert len(changes) == 0


class TestMigrateContextClaudeFixSetsUnsafe:
    def test_claude_fix_rule_sets_safe_false(self, tmp_path):
        data = [
            _sample_finding(
                "test-coverage-branch",
                "src/main.py",
                safe=True,
                refactor_class="simple_fix",
            )
        ]
        findings_path = _make_findings(data)
        changes = reclassify_findings(findings_path)
        fid = "test-test-coverage-branch"
        assert fid in changes
        assert changes[fid]["new_safe"] is False


class TestMigrateContextRefactorClassSetsUnsafe:
    def test_refactor_class_rule_sets_safe_false(self, tmp_path):
        data = [
            _sample_finding(
                "xo-max-lines",
                "src/main.py",
                safe=True,
                refactor_class="simple_fix",
            )
        ]
        findings_path = _make_findings(data)
        changes = reclassify_findings(findings_path)
        fid = "test-xo-max-lines"
        assert fid in changes
        assert changes[fid]["new_safe"] is False


class TestMigrateContextUnknownClassPreservesSafe:
    def test_unknown_class_branch_preserves_old_safe(self, tmp_path):
        from unittest.mock import MagicMock

        mock_class = MagicMock()
        mock_class.value = "mystery_class"

        data = [
            _sample_finding(
                "ruff-test",
                "src/main.py",
                safe=True,
                refactor_class="simple_fix",
            )
        ]
        findings_path = _make_findings(data)

        with patch(
            "bluei.engine.migrate_context.classify_finding", return_value=mock_class
        ):
            changes = reclassify_findings(findings_path)

        fid = "test-ruff-test"
        assert fid in changes
        assert changes[fid]["new_safe"] is True


class TestMigrateContextDryRunSkipNoFindingId:
    def test_dry_run_skips_entry_without_id(self, tmp_path):
        data = [{"rule": "ruff-test", "path": "a.py", "line": 1}]
        findings_path = _make_findings(data)
        report = dry_run_report(findings_path)
        assert "Total findings: 1" in report
        assert "Unchanged: 0" in report


class TestMigrateContextDryRunSimpleNew:
    def test_dry_run_counts_simple_fix_changes(self, tmp_path):
        data = [
            _sample_finding(
                "ruff-FAKESIMPLE", "src/main.py", safe=True, refactor_class=None
            )
        ]
        findings_path = _make_findings(data)
        report = dry_run_report(findings_path)
        assert "New SIMPLE_FIX: 1" in report


class TestMigrateContextDryRunClaudeNew:
    def test_dry_run_counts_claude_fix_changes(self, tmp_path):
        data = [
            _sample_finding("test-coverage-branch", "src/main.py", refactor_class=None)
        ]
        findings_path = _make_findings(data)
        report = dry_run_report(findings_path)
        assert "New CLAUDE_FIX: 1" in report


class TestMigrateContextDryRunRefactorNew:
    def test_dry_run_counts_refactor_class_changes(self, tmp_path):
        data = [_sample_finding("xo-max-lines", "src/main.py", refactor_class=None)]
        findings_path = _make_findings(data)
        report = dry_run_report(findings_path)
        assert "New REFACTOR_CLASS: 1" in report


class TestMigrateContextDryRunUnchanged:
    def test_dry_run_counts_unchanged_when_class_same(self, tmp_path):
        data = [
            _sample_finding(
                "some-unknown-rule", "src/main.py", refactor_class="cascade_fix"
            )
        ]
        findings_path = _make_findings(data)
        report = dry_run_report(findings_path)
        assert "Unchanged: 1" in report
