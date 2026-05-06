#!/usr/bin/env python3
"""Tests for Batch PR Phase 4: cross-rule batching and severity-aware batching."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from core.sandbox_local_runner.models import (
    BatchGroup, BatchRule, BatchStatus, Finding, FixResult,
)
from core.sandbox_local_runner.batch_pr import (
    group_findings_for_batch,
    load_batch_rules,
    rule_matches,
    _severity_batch_cap,
    SEVERITY_ORDER,
)


# ── Helpers ──────────────────────────────────────────────────────

def make_finding(**overrides) -> Finding:
    """Create a Finding with sensible defaults."""
    defaults = {
        'finding_id': 'f001',
        'repo': 'test-repo',
        'path': 'zerver/lib/message.py',
        'line': 42,
        'rule': 'ruff-c408',
        'snippet': 'dict(a=1)',
        'confidence': 0.72,
        'quick_win': True,
        'safe_to_autofix': True,
    }
    defaults.update(overrides)
    return Finding(**defaults)


def make_issue(finding_id: str = 'f001', **overrides) -> Dict[str, Any]:
    """Create a minimal issue dict."""
    defaults = {
        'finding_id': finding_id,
        'issue_id': f'ISS-{finding_id[:4]}',
        'status': 'open',
        'github': {'issue_number': 1},
    }
    defaults.update(overrides)
    return defaults


def make_candidate(fid='f001', rule='ruff-c408', path='zerver/lib/message.py', line=42):
    """Create a (issue, finding) candidate tuple."""
    finding = make_finding(finding_id=fid, rule=rule, path=path, line=line)
    issue = make_issue(finding_id=fid)
    return (issue, finding)


# ── Cross-Rule Batching ────────────────────────────────────────

class TestCrossRuleBatching:

    def test_cross_rule_groups_all_ruff_findings(self):
        """All ruff-* findings should be grouped into 1 batch when cross-rule enabled."""
        cross_rule = BatchRule(
            rule_pattern="ruff-*",
            enabled=True,
            group_by="cross-rule",
            max_batch_size=30,
            severity="low",
            priority=10,
        )
        candidates = [
            make_candidate(fid='f001', rule='ruff-c408', path='a.py', line=1),
            make_candidate(fid='f002', rule='ruff-b904', path='b.py', line=2),
            make_candidate(fid='f003', rule='ruff-s311', path='c.py', line=3),
        ]
        batches = group_findings_for_batch(candidates, [cross_rule])
        # Should be exactly 1 multi-finding batch
        multi = [b for b in batches if not b.is_solo]
        assert len(multi) == 1
        assert len(multi[0].findings) == 3

    def test_cross_rule_pr_title(self):
        """Cross-rule batch title: 'resolve N linter findings (M rules)'."""
        findings = [
            make_finding(finding_id='f001', rule='ruff-c408', path='a.py', line=1),
            make_finding(finding_id='f002', rule='ruff-b904', path='b.py', line=2),
            make_finding(finding_id='f003', rule='ruff-s311', path='c.py', line=3),
        ]
        batch = BatchGroup(
            batch_id='test-cross',
            rule_pattern='ruff-*',
            group_by='cross-rule',
            findings=findings,
            issues=[make_issue(f.finding_id) for f in findings],
        )
        title = batch.pr_title()
        assert '3 linter findings' in title
        assert '3 rules' in title

    def test_cross_rule_pr_body_grouped_by_rule(self):
        """Cross-rule body should have sections per rule."""
        findings = [
            make_finding(finding_id='f001', rule='ruff-c408', path='a.py', line=1),
            make_finding(finding_id='f002', rule='ruff-c408', path='b.py', line=2),
            make_finding(finding_id='f003', rule='ruff-b904', path='c.py', line=3),
        ]
        batch = BatchGroup(
            batch_id='test-cross',
            rule_pattern='ruff-*',
            group_by='cross-rule',
            findings=findings,
            issues=[make_issue(f.finding_id) for f in findings],
        )
        body = batch.pr_body()
        assert '#### ruff-c408' in body
        assert '#### ruff-b904' in body
        assert '2 findings' in body  # ruff-c408 has 2

    def test_cross_rule_respects_isolation(self):
        """Migration files should still be excluded from cross-rule batches."""
        cross_rule = BatchRule(
            rule_pattern="ruff-*",
            enabled=True,
            group_by="cross-rule",
            max_batch_size=30,
            severity="low",
            isolation={'file_patterns': ['**/migrations/*.py']},
            priority=10,
        )
        candidates = [
            make_candidate(fid='f001', rule='ruff-c408', path='zerver/lib/a.py', line=1),
            make_candidate(fid='f002', rule='ruff-b904', path='zerver/lib/b.py', line=2),
            make_candidate(fid='f003', rule='ruff-c408', path='zerver/migrations/0001.py', line=3),
        ]
        batches = group_findings_for_batch(candidates, [cross_rule])
        # Migration file should be solo (isolated)
        solo = [b for b in batches if b.is_solo]
        migration_solos = [b for b in solo if b.findings[0].path == 'zerver/migrations/0001.py']
        assert len(migration_solos) == 1
        # Non-migration findings should be in a multi-finding batch
        multi = [b for b in batches if not b.is_solo]
        assert len(multi) == 1
        assert len(multi[0].findings) == 2  # a.py and b.py

    def test_cross_rule_disabled_by_default(self):
        """Cross-rule rule is opt-in: when disabled, findings use per-rule rules."""
        cross_rule = BatchRule(
            rule_pattern="ruff-*",
            enabled=False,
            group_by="cross-rule",
            max_batch_size=30,
            severity="low",
            priority=10,
        )
        per_rule = BatchRule(
            rule_pattern="ruff-c408",
            enabled=True,
            group_by="rule",
            max_batch_size=20,
            severity="normal",
            priority=1,
        )
        candidates = [
            make_candidate(fid='f001', rule='ruff-c408', path='a.py', line=1),
            make_candidate(fid='f002', rule='ruff-c408', path='b.py', line=2),
            make_candidate(fid='f003', rule='ruff-b904', path='c.py', line=3),
        ]
        batches = group_findings_for_batch(candidates, [cross_rule, per_rule])
        # ruff-c408 should be batched together, ruff-b904 should be solo (no matching rule)
        c408_batches = [b for b in batches if any(f.rule == 'ruff-c408' for f in b.findings)]
        c408_multi = [b for b in c408_batches if not b.is_solo]
        assert len(c408_multi) == 1
        assert len(c408_multi[0].findings) == 2
        # ruff-b904 should be solo (no matching per-rule rule)
        b904_batches = [b for b in batches if any(f.rule == 'ruff-b904' for f in b.findings)]
        assert all(b.is_solo for b in b904_batches)

    def test_cross_rule_title_single_rule_fallback(self):
        """When all findings in a cross-rule batch share the same rule, use normal title."""
        findings = [
            make_finding(finding_id='f001', rule='ruff-c408', path='a.py', line=1),
            make_finding(finding_id='f002', rule='ruff-c408', path='b.py', line=2),
        ]
        batch = BatchGroup(
            batch_id='test-same-rule',
            rule_pattern='ruff-*',
            group_by='cross-rule',
            findings=findings,
            issues=[make_issue(f.finding_id) for f in findings],
        )
        title = batch.pr_title()
        # All same rule → standard title
        assert '2 ruff-* findings' in title


# ── Severity-Aware Batching ────────────────────────────────────

class TestSeverityBatching:

    def test_severity_critical_always_solo(self):
        """Critical severity findings should never be batched."""
        rule = BatchRule(
            rule_pattern="ruff-s311",
            enabled=True,
            group_by="rule",
            max_batch_size=20,
            severity="critical",
            priority=1,
        )
        candidates = [
            make_candidate(fid='f001', rule='ruff-s311', path='a.py', line=1),
            make_candidate(fid='f002', rule='ruff-s311', path='b.py', line=2),
            make_candidate(fid='f003', rule='ruff-s311', path='c.py', line=3),
        ]
        batches = group_findings_for_batch(candidates, [rule])
        # All should be solo
        assert all(b.is_solo for b in batches)
        assert len(batches) == 3

    def test_severity_high_max_5(self):
        """High severity should cap batches at 5 findings."""
        rule = BatchRule(
            rule_pattern="ruff-b904",
            enabled=True,
            group_by="rule",
            max_batch_size=20,
            severity="high",
            priority=1,
        )
        candidates = [make_candidate(fid=f'f{i:03d}', rule='ruff-b904', path=f'f{i}.py', line=i)
                      for i in range(12)]
        batches = group_findings_for_batch(candidates, [rule])
        multi = [b for b in batches if not b.is_solo]
        for batch in multi:
            assert len(batch.findings) <= 5

    def test_severity_low_max_30(self):
        """Low severity should allow up to 30 findings per batch."""
        rule = BatchRule(
            rule_pattern="ruff-b007",
            enabled=True,
            group_by="rule",
            max_batch_size=10,  # Even though rule says 10...
            max_files_per_batch=30,  # Need enough files too
            severity="low",     # ...severity=low overrides to 30
            priority=1,
        )
        candidates = [make_candidate(fid=f'f{i:03d}', rule='ruff-b007', path=f'f{i}.py', line=i)
                      for i in range(25)]
        batches = group_findings_for_batch(candidates, [rule])
        multi = [b for b in batches if not b.is_solo]
        # All 25 should fit in 1 batch (cap is 30)
        assert len(multi) == 1
        assert len(multi[0].findings) == 25

    def test_severity_ordering(self):
        """Batches should be ordered: critical → high → normal → low."""
        rules = [
            BatchRule(rule_pattern="ruff-c408", enabled=True, group_by="rule",
                      max_batch_size=20, severity="normal", priority=1),
            BatchRule(rule_pattern="ruff-s311", enabled=True, group_by="rule",
                      max_batch_size=20, severity="critical", priority=2),
            BatchRule(rule_pattern="ruff-b007", enabled=True, group_by="rule",
                      max_batch_size=20, severity="low", priority=3),
        ]
        candidates = [
            make_candidate(fid='f001', rule='ruff-s311', path='a.py', line=1),   # critical
            make_candidate(fid='f002', rule='ruff-s311', path='b.py', line=2),   # critical
            make_candidate(fid='f003', rule='ruff-c408', path='c.py', line=3),   # normal
            make_candidate(fid='f004', rule='ruff-c408', path='d.py', line=4),   # normal
            make_candidate(fid='f005', rule='ruff-b007', path='e.py', line=5),   # low
            make_candidate(fid='f006', rule='ruff-b007', path='f.py', line=6),   # low
        ]
        batches = group_findings_for_batch(candidates, rules)
        # Critical findings → solo (comes first), normal → batch, low → batch
        # Collect batch types by position
        s311_batches = [b for b in batches if any(f.rule == 'ruff-s311' for f in b.findings)]
        c408_batches = [b for b in batches if any(f.rule == 'ruff-c408' for f in b.findings)]
        b007_batches = [b for b in batches if any(f.rule == 'ruff-b007' for f in b.findings)]
        # All critical findings should be solo
        assert all(b.is_solo for b in s311_batches)
        # Normal/low should be multi-finding batches
        assert any(not b.is_solo for b in c408_batches)
        assert any(not b.is_solo for b in b007_batches)
        # Solo batches should come before multi-finding batches
        first_multi_idx = next(i for i, b in enumerate(batches) if not b.is_solo)
        all_solo_before = all(b.is_solo for b in batches[:first_multi_idx])
        assert all_solo_before

    def test_mixed_severity_findings(self):
        """Different severities should be batched according to their rules."""
        rules = [
            BatchRule(rule_pattern="ruff-s311", enabled=True, group_by="rule",
                      max_batch_size=20, severity="critical", priority=1),
            BatchRule(rule_pattern="ruff-c408", enabled=True, group_by="rule",
                      max_batch_size=20, severity="normal", priority=2),
        ]
        candidates = [
            make_candidate(fid='f001', rule='ruff-s311', path='a.py', line=1),
            make_candidate(fid='f002', rule='ruff-s311', path='b.py', line=2),
            make_candidate(fid='f003', rule='ruff-c408', path='c.py', line=3),
            make_candidate(fid='f004', rule='ruff-c408', path='d.py', line=4),
        ]
        batches = group_findings_for_batch(candidates, rules)
        # s311 (critical) → 2 solo batches
        # c408 (normal) → 1 batch of 2
        solo = [b for b in batches if b.is_solo]
        multi = [b for b in batches if not b.is_solo]
        assert len(solo) == 2
        assert len(multi) == 1
        assert len(multi[0].findings) == 2
        assert all(f.rule == 'ruff-c408' for f in multi[0].findings)


# ── Unit tests for helpers ─────────────────────────────────────

class TestSeverityHelpers:

    def test_severity_cap_critical(self):
        assert _severity_batch_cap("critical", 20) == 1

    def test_severity_cap_high(self):
        assert _severity_batch_cap("high", 20) == 5
        assert _severity_batch_cap("high", 3) == 3  # min(3, 5) = 3

    def test_severity_cap_normal(self):
        assert _severity_batch_cap("normal", 15) == 15

    def test_severity_cap_low(self):
        assert _severity_batch_cap("low", 10) == 30

    def test_severity_order_values(self):
        assert SEVERITY_ORDER["critical"] < SEVERITY_ORDER["high"]
        assert SEVERITY_ORDER["high"] < SEVERITY_ORDER["normal"]
        assert SEVERITY_ORDER["normal"] < SEVERITY_ORDER["low"]


# ── Load rules from YAML ───────────────────────────────────────

class TestBatchRulesYAML:

    def test_load_rules_with_severity(self, tmp_path):
        """Ensure severity is loaded from YAML."""
        yaml_content = """
rules:
  - rule_pattern: "ruff-c408"
    enabled: true
    group_by: "rule"
    max_batch_size: 20
    severity: "high"
    priority: 1
"""
        p = tmp_path / "rules.yaml"
        p.write_text(yaml_content)
        rules = load_batch_rules(p)
        assert len(rules) == 1
        assert rules[0].severity == "high"

    def test_load_rules_default_severity(self, tmp_path):
        """Severity defaults to 'normal' if not specified."""
        yaml_content = """
rules:
  - rule_pattern: "ruff-c408"
    enabled: true
    group_by: "rule"
    max_batch_size: 20
    priority: 1
"""
        p = tmp_path / "rules.yaml"
        p.write_text(yaml_content)
        rules = load_batch_rules(p)
        assert rules[0].severity == "normal"

    def test_load_cross_rule_config(self, tmp_path):
        """Load cross-rule configuration from YAML."""
        yaml_content = """
rules:
  - rule_pattern: "ruff-*"
    enabled: false
    group_by: "cross-rule"
    max_batch_size: 30
    severity: "low"
    priority: 10
"""
        p = tmp_path / "rules.yaml"
        p.write_text(yaml_content)
        rules = load_batch_rules(p)
        assert len(rules) == 1
        assert rules[0].group_by == "cross-rule"
        assert rules[0].severity == "low"
        assert rules[0].enabled is False
