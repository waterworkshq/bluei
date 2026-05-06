#!/usr/bin/env python3
"""Tests for Phase J: Learned-Rule conservative autonomous pattern learning.

Run with: python -m pytest tests/test_review_learned_rules.py -v
"""

import json
import shutil
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa_agent.models import (
    LearnedRule,
    LearnedRuleStatus,
    FindingSource,
    FindingActionability,
    FindingSeverity,
    generate_id,
    normalize_finding_header,
)
from qa_agent.review import (
    _classify_pattern_risk,
    _check_rule_conflicts,
    _should_activate_tentative_rule,
    _propose_learned_rule_from_finding,
    _increment_rule_evidence,
    _activate_tentative_rule,
    _suppress_finding_with_rule,
    _process_learned_rules_for_run,
    _get_learned_rules_state,
    _save_learned_rules_state,
    _build_learned_rules_payload,
)
from qa_agent.state import StateManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _isolated_tmp() -> Path:
    base = Path(f"/tmp/qa_test_learned_rules_{uuid.uuid4().hex[:8]}")
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    return base


def make_tentative_rule(
    header: str = "outstanding-todo",
    evidence_count: int = 1,
    risk_level: str = "low",
) -> LearnedRule:
    now = "2026-03-29T00:00:00Z"
    return LearnedRule(
        rule_id=f"lr-{uuid.uuid4().hex[:12]}-000",
        header=normalize_finding_header(header),
        pattern="src/main.ts",
        status=LearnedRuleStatus.TENTATIVE,
        risk_level=risk_level,
        precedence=10,
        evidence_count=evidence_count,
        source_finding_ids=["rf-abc123-000"],
        proposal_run_id="arun-test-001",
        created_at=now,
        updated_at=now,
    )


def make_active_rule(
    header: str = "outstanding-todo",
    evidence_count: int = 5,
) -> LearnedRule:
    now = "2026-03-29T00:00:00Z"
    return LearnedRule(
        rule_id=f"lr-{uuid.uuid4().hex[:12]}-000",
        header=normalize_finding_header(header),
        pattern="src/main.ts",
        status=LearnedRuleStatus.ACTIVE,
        risk_level="low",
        precedence=10,
        evidence_count=evidence_count,
        source_finding_ids=["rf-abc123-000"],
        proposal_run_id="arun-test-001",
        activated_at=now,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Test: pattern risk classification
# ---------------------------------------------------------------------------

class TestClassifyPatternRisk:
    def test_low_risk_style_pattern(self):
        # style/format/import-order: low risk
        risk = _classify_pattern_risk(
            header="outstanding-todo",
            path="src/main.ts",
            severity=FindingSeverity.LOW,
            actionability=FindingActionability.LOW,
        )
        assert risk == "low"

    def test_low_risk_long_line(self):
        risk = _classify_pattern_risk(
            header="excessively-long-line",
            path="src/main.ts",
            severity=FindingSeverity.LOW,
            actionability=FindingActionability.INFORMATIONAL,
        )
        assert risk == "low"

    def test_high_risk_security_header(self):
        risk = _classify_pattern_risk(
            header="security-credential-exposure",
            path="src/auth.ts",
            severity=FindingSeverity.MEDIUM,
            actionability=FindingActionability.MEDIUM,
        )
        assert risk == "high"

    def test_high_risk_secret_path(self):
        risk = _classify_pattern_risk(
            header="unused-import",
            path=".env.example",
            severity=FindingSeverity.LOW,
            actionability=FindingActionability.LOW,
        )
        assert risk == "high"

    def test_high_risk_high_severity(self):
        risk = _classify_pattern_risk(
            header="unused-import",
            path="src/main.ts",
            severity=FindingSeverity.CRITICAL,
            actionability=FindingActionability.LOW,
        )
        assert risk == "high"

    def test_high_risk_high_actionability(self):
        risk = _classify_pattern_risk(
            header="outstanding-todo",
            path="src/main.ts",
            severity=FindingSeverity.LOW,
            actionability=FindingActionability.HIGH,
        )
        assert risk == "high"


# ---------------------------------------------------------------------------
# Test: tentative vs active rule behavior
# ---------------------------------------------------------------------------

class TestTentativeVsActive:
    def test_tentative_rule_requires_min_evidence(self):
        rule = make_tentative_rule(evidence_count=1)
        should_activate, reason = _should_activate_tentative_rule(rule, [])
        assert not should_activate
        assert "evidence_count=1" in reason

    def test_tentative_rule_activates_at_threshold(self):
        rule = make_tentative_rule(evidence_count=3)
        should_activate, reason = _should_activate_tentative_rule(rule, [])
        assert should_activate
        assert "Activated" in reason

    def test_high_risk_rule_never_activates(self):
        rule = make_tentative_rule(evidence_count=10, risk_level="high")
        should_activate, reason = _should_activate_tentative_rule(rule, [])
        assert not should_activate
        assert "risk_level" in reason

    def test_activate_tentative_rule_sets_activated_at(self):
        rule = make_tentative_rule(evidence_count=3)
        assert rule.activated_at is None
        activated = _activate_tentative_rule(rule)
        assert activated.status == LearnedRuleStatus.ACTIVE
        assert activated.activated_at is not None
        # Original rule unchanged (immutable)
        assert rule.status == LearnedRuleStatus.TENTATIVE

    def test_increment_rule_evidence(self):
        rule = make_tentative_rule(evidence_count=1)
        incremented = _increment_rule_evidence(rule, "rf-xyz789-000")
        assert incremented.evidence_count == 2
        assert "rf-xyz789-000" in incremented.source_finding_ids
        # Original unchanged
        assert rule.evidence_count == 1


# ---------------------------------------------------------------------------
# Test: precedence and conflict resolution
# ---------------------------------------------------------------------------

class TestRuleConflicts:
    def test_no_conflict_with_different_headers(self):
        existing = [make_active_rule(header="different-rule")]
        conflicts = _check_rule_conflicts("new-rule", existing)
        assert len(conflicts) == 0

    def test_conflict_with_same_header_active_rule(self):
        existing = [make_active_rule(header="outstanding-todo")]
        conflicts = _check_rule_conflicts("outstanding-todo", existing)
        assert len(conflicts) == 1
        assert "already covers" in conflicts[0]

    def test_operator_authored_rule_takes_precedence(self):
        """Operator rules (precedence 0) dominate learned rules."""
        op_rule = make_active_rule(header="outstanding-todo")
        op_rule = LearnedRule(
            rule_id="lr-operator-000",
            header="outstanding-todo",
            pattern="src/main.ts",
            status=LearnedRuleStatus.ACTIVE,
            risk_level="low",
            precedence=0,  # operator-authored
            evidence_count=1,
            created_at="2026-03-29T00:00:00Z",
            updated_at="2026-03-29T00:00:00Z",
        )
        conflicts = _check_rule_conflicts("outstanding-todo", [op_rule])
        assert len(conflicts) == 1
        assert "Operator-authored" in conflicts[0]

    def test_conflict_with_tentative_rule_same_header(self):
        existing = [make_tentative_rule(header="outstanding-todo")]
        conflicts = _check_rule_conflicts("outstanding-todo", existing)
        assert len(conflicts) == 1
        assert "tentative" in conflicts[0].lower()


# ---------------------------------------------------------------------------
# Test: operator-authored rule dominance
# ---------------------------------------------------------------------------

class TestOperatorRuleDominance:
    def test_operator_rule_suppresses_any_finding(self):
        op_rule = LearnedRule(
            rule_id="lr-op-000",
            header="outstanding-todo",
            pattern="src/main.ts",
            status=LearnedRuleStatus.ACTIVE,
            risk_level="low",
            precedence=0,  # operator-authored
            evidence_count=1,
            created_at="2026-03-29T00:00:00Z",
            updated_at="2026-03-29T00:00:00Z",
        )
        finding = {
            "finding_id": "rf-abc123-000",
            "header": "outstanding-todo",
            "path": "src/main.ts",
        }
        should_suppress, reason = _suppress_finding_with_rule(finding, [op_rule])
        assert should_suppress
        assert "Operator-authored" in reason

    def test_learned_rule_does_not_override_operator_rule(self):
        # A source-finding (one that contributed to a rule) is never suppressed
        # by that rule — it is still a valid finding for review.
        learned = make_active_rule(header="outstandingtodo")
        finding_source = {
            "finding_id": "rf-abc123-000",  # matches source_finding_ids
            "header": "outstandingtodo",
            "path": "src/main.ts",
        }
        should_suppress, _ = _suppress_finding_with_rule(finding_source, [learned])
        assert not should_suppress, "Source findings must not be suppressed"

        # A DIFFERENT finding with the same header IS suppressed by the active rule
        finding_other = {
            "finding_id": "rf-abc999-999",  # not in source_finding_ids
            "header": "outstandingtodo",
            "path": "src/main.ts",
        }
        should_suppress2, reason = _suppress_finding_with_rule(finding_other, [learned])
        assert should_suppress2, "Non-source findings should be suppressed"


# ---------------------------------------------------------------------------
# Test: reaction-only suppression rejection
# ---------------------------------------------------------------------------

class TestReactionOnlySuppression:
    def test_reaction_signals_not_inspected_in_suppression(self):
        """
        _suppress_finding_with_rule does NOT inspect feedback/reaction signals.
        It only checks header+pattern match.  This means reaction-only signals
        can NEVER be sufficient to suppress a finding through the learned rule
        path, satisfying the conservative policy requirement.
        """
        learned = make_active_rule(header="outstanding-todo")
        finding = {
            "finding_id": "rf-abc123-999",
            "header": "unrelated-rule",  # Different header — no suppression
            "path": "src/main.ts",
        }
        should_suppress, _ = _suppress_finding_with_rule(finding, [learned])
        assert not should_suppress

        # Even with the right header, source findings aren't suppressed
        finding_source = {
            "finding_id": "rf-abc123-000",
            "header": "outstanding-todo",
            "path": "src/main.ts",
        }
        should_suppress2, _ = _suppress_finding_with_rule(finding_source, [learned])
        assert not should_suppress2


# ---------------------------------------------------------------------------
# Test: safe auto-activation for low-risk repeated style-like patterns
# ---------------------------------------------------------------------------

class TestAutoActivation:
    def test_propose_from_low_risk_finding(self):
        finding = {
            "repo": "test-repo",
            "path": "src/main.ts",
            "line": 10,
            "header": "outstanding-todo",
            "snippet": "# TODO: refactor",
            "source": FindingSource.LINTER.value,
            "actionability": FindingActionability.LOW.value,
            "severity": FindingSeverity.LOW.value,
            "confidence": 0.7,
            "safe_to_autofix": False,
            "discovered_at": "2026-03-29T00:00:00Z",
        }
        rule = _propose_learned_rule_from_finding(
            finding, "arun-test-001", []
        )
        assert rule is not None
        assert rule.status == LearnedRuleStatus.TENTATIVE
        assert rule.risk_level == "low"
        assert rule.evidence_count == 1

    def test_propose_rejected_for_high_risk(self):
        finding = {
            "repo": "test-repo",
            "path": "src/auth.ts",
            "line": 10,
            "header": "security-credential-exposure",
            "snippet": "# BUG: hardcoded password",
            "source": FindingSource.LINTER.value,
            "actionability": FindingActionability.HIGH.value,
            "severity": FindingSeverity.HIGH.value,
            "confidence": 0.9,
            "safe_to_autofix": False,
            "discovered_at": "2026-03-29T00:00:00Z",
        }
        rule = _propose_learned_rule_from_finding(
            finding, "arun-test-001", []
        )
        assert rule is None  # High-risk patterns are never proposed

    def test_propose_rejected_for_high_actionability(self):
        finding = {
            "repo": "test-repo",
            "path": "src/main.ts",
            "line": 10,
            "header": "outstanding-todo",
            "snippet": "# TODO: fix this",
            "source": FindingSource.LINTER.value,
            "actionability": FindingActionability.HIGH.value,  # HIGH = not low-risk
            "severity": FindingSeverity.LOW.value,
            "confidence": 0.7,
            "safe_to_autofix": False,
            "discovered_at": "2026-03-29T00:00:00Z",
        }
        rule = _propose_learned_rule_from_finding(
            finding, "arun-test-001", []
        )
        assert rule is None

    def test_propose_rejected_when_conflict_exists(self):
        existing = [make_active_rule(header="outstanding-todo")]
        finding = {
            "repo": "test-repo",
            "path": "src/main.ts",
            "line": 10,
            "header": "outstanding-todo",
            "snippet": "# TODO: another one",
            "source": FindingSource.LINTER.value,
            "actionability": FindingActionability.LOW.value,
            "severity": FindingSeverity.LOW.value,
            "confidence": 0.7,
            "safe_to_autofix": False,
            "discovered_at": "2026-03-29T00:00:00Z",
        }
        rule = _propose_learned_rule_from_finding(
            finding, "arun-test-001", existing
        )
        assert rule is None  # Conflict rejected


# ---------------------------------------------------------------------------
# Test: persistence / load round-trip
# ---------------------------------------------------------------------------

class TestPersistenceRoundTrip:
    def test_learned_rules_save_load_roundtrip(self, tmp_path: Path):
        repo_name = "test-repo"
        state = StateManager(tmp_path / "repos")

        rule = make_tentative_rule(header="outstanding-todo", evidence_count=3)
        payload = _build_learned_rules_payload(
            [rule],
            updated_at="2026-03-29T00:00:00Z",
        )
        _save_learned_rules_state(state, repo_name, payload)

        loaded = _get_learned_rules_state(state, repo_name)
        assert loaded["version"] == 1
        assert len(loaded["rules"]) == 1
        assert loaded["rules"][0]["status"] == "tentative"
        assert loaded["active_count"] == 0
        assert loaded["tentative_count"] == 1

    def test_multiple_rules_persist(self, tmp_path: Path):
        repo_name = "test-repo"
        state = StateManager(tmp_path / "repos")

        tentative = make_tentative_rule(header="outstanding-todo", evidence_count=2)
        active = make_active_rule(header="unused-import", evidence_count=5)
        payload = _build_learned_rules_payload(
            [tentative, active],
            updated_at="2026-03-29T00:00:00Z",
        )
        _save_learned_rules_state(state, repo_name, payload)

        loaded = _get_learned_rules_state(state, repo_name)
        assert len(loaded["rules"]) == 2
        assert loaded["active_count"] == 1
        assert loaded["tentative_count"] == 1

    def test_empty_rules_default(self, tmp_path: Path):
        repo_name = "test-repo"
        state = StateManager(tmp_path / "repos")
        loaded = _get_learned_rules_state(state, repo_name)
        assert loaded["version"] == 1
        assert loaded["rules"] == []
        assert loaded["active_count"] == 0


# ---------------------------------------------------------------------------
# Test: full _process_learned_rules_for_run integration
# ---------------------------------------------------------------------------

class TestProcessLearnedRulesForRun:
    def test_repeated_finding_proposes_rule(self):
        finding = {
            "finding_id": "rf-abc123-000",
            "repo": "test-repo",
            "path": "src/main.ts",
            "line": 10,
            "header": "outstanding-todo",
            "snippet": "# TODO: refactor this",
            "source": FindingSource.LINTER.value,
            "actionability": FindingActionability.LOW.value,
            "severity": FindingSeverity.LOW.value,
            "confidence": 0.7,
            "safe_to_autofix": False,
            "discovered_at": "2026-03-29T00:00:00Z",
        }
        rules_state = {"version": 1, "rules": [], "active_count": 0, "tentative_count": 0}
        # Same finding twice to trigger proposal (2 occurrences)
        findings = [dict(finding), dict(finding)]
        findings[1]["finding_id"] = "rf-abc123-001"

        filtered, updated_rules, log = _process_learned_rules_for_run(
            findings, rules_state, "arun-test-001"
        )
        assert len(updated_rules) == 1
        assert updated_rules[0].status == LearnedRuleStatus.TENTATIVE
        assert "PROPOSED" in log[0]

    def test_active_rule_suppresses_matching_finding(self):
        finding = {
            "finding_id": "rf-new-finding-999",  # Not a source finding
            "repo": "test-repo",
            "path": "src/main.ts",
            "line": 20,
            "header": "outstanding-todo",
            "snippet": "# TODO: another",
            "source": FindingSource.LINTER.value,
            "actionability": FindingActionability.LOW.value,
            "severity": FindingSeverity.LOW.value,
            "confidence": 0.7,
            "safe_to_autofix": False,
            "discovered_at": "2026-03-29T00:00:00Z",
        }
        active = make_active_rule(header="outstanding-todo")
        rules_state = {
            "version": 1,
            "rules": [active.to_dict()],
            "active_count": 1,
            "tentative_count": 0,
        }
        findings = [finding]
        filtered, updated_rules, log = _process_learned_rules_for_run(
            findings, rules_state, "arun-test-002"
        )
        # Finding should be suppressed (filtered out)
        assert len(filtered) == 0
        assert any("SUPPRESSED" in line for line in log)

    def test_source_finding_not_suppressed(self):
        """A finding that contributed to a rule is not suppressed by it."""
        finding = {
            "finding_id": "rf-abc123-000",  # Same as source_finding_ids
            "repo": "test-repo",
            "path": "src/main.ts",
            "line": 10,
            "header": "outstanding-todo",
            "snippet": "# TODO: original",
            "source": FindingSource.LINTER.value,
            "actionability": FindingActionability.LOW.value,
            "severity": FindingSeverity.LOW.value,
            "confidence": 0.7,
            "safe_to_autofix": False,
            "discovered_at": "2026-03-29T00:00:00Z",
        }
        active = make_active_rule(header="outstanding-todo")
        active = LearnedRule(
            rule_id=active.rule_id,
            header=active.header,
            pattern=active.pattern,
            status=LearnedRuleStatus.ACTIVE,
            risk_level=active.risk_level,
            precedence=active.precedence,
            evidence_count=active.evidence_count,
            source_finding_ids=["rf-abc123-000"],  # Same as finding
            proposal_run_id=active.proposal_run_id,
            activated_at=active.activated_at,
            created_at=active.created_at,
            updated_at=active.updated_at,
        )
        rules_state = {
            "version": 1,
            "rules": [active.to_dict()],
            "active_count": 1,
            "tentative_count": 0,
        }
        findings = [finding]
        filtered, updated_rules, log = _process_learned_rules_for_run(
            findings, rules_state, "arun-test-003"
        )
        # Source finding should NOT be suppressed
        assert len(filtered) == 1

    def test_evidence_increment_on_subsequent_run(self):
        finding = {
            "finding_id": "rf-xyz789-000",
            "repo": "test-repo",
            "path": "src/main.ts",
            "line": 30,
            "header": "outstanding-todo",
            "snippet": "# TODO: new occurrence",
            "source": FindingSource.LINTER.value,
            "actionability": FindingActionability.LOW.value,
            "severity": FindingSeverity.LOW.value,
            "confidence": 0.7,
            "safe_to_autofix": False,
            "discovered_at": "2026-03-29T00:00:00Z",
        }
        # Start with evidence_count=1 so increment brings it to 2 (below activation threshold=3)
        tentative = make_tentative_rule(header="outstanding-todo", evidence_count=1)
        rules_state = {
            "version": 1,
            "rules": [tentative.to_dict()],
            "active_count": 0,
            "tentative_count": 1,
        }
        findings = [finding]
        filtered, updated_rules, log = _process_learned_rules_for_run(
            findings, rules_state, "arun-test-004"
        )
        # After seeing 1 new occurrence, evidence_count = 2 (tentative, not yet active)
        updated_tentative = next(
            r for r in updated_rules if r.status == LearnedRuleStatus.TENTATIVE
        )
        assert updated_tentative.evidence_count == 2
        assert "EVIDENCE" in log[0]

    def test_tentative_rule_activates_after_threshold(self):
        finding = {
            "finding_id": "rf-activation-test-000",
            "repo": "test-repo",
            "path": "src/main.ts",
            "line": 40,
            "header": "excessively-long-line",
            "snippet": "x" * 150,
            "source": FindingSource.LINTER.value,
            "actionability": FindingActionability.LOW.value,
            "severity": FindingSeverity.LOW.value,
            "confidence": 0.7,
            "safe_to_autofix": True,
            "discovered_at": "2026-03-29T00:00:00Z",
        }
        # Start with evidence_count = 2 (one more needed for activation)
        tentative = make_tentative_rule(header="excessively-long-line", evidence_count=2)
        tentative = LearnedRule(
            rule_id=tentative.rule_id,
            header=tentative.header,
            pattern=tentative.pattern,
            status=LearnedRuleStatus.TENTATIVE,
            risk_level="low",
            precedence=10,
            evidence_count=2,
            source_finding_ids=["rf-old-000"],
            proposal_run_id=tentative.proposal_run_id,
            created_at=tentative.created_at,
            updated_at=tentative.updated_at,
        )
        rules_state = {
            "version": 1,
            "rules": [tentative.to_dict()],
            "active_count": 0,
            "tentative_count": 1,
        }
        findings = [finding]
        filtered, updated_rules, log = _process_learned_rules_for_run(
            findings, rules_state, "arun-test-005"
        )
        active_rules = [r for r in updated_rules if r.status == LearnedRuleStatus.ACTIVE]
        assert len(active_rules) == 1
        assert any("ACTIVATED" in line for line in log)


# ---------------------------------------------------------------------------
# Test: build_learned_rules_payload
# ---------------------------------------------------------------------------

class TestBuildLearnedRulesPayload:
    def test_counts_reflect_status(self):
        tentative = make_tentative_rule()
        active = make_active_rule()
        payload = _build_learned_rules_payload([tentative, active])
        assert payload["active_count"] == 1
        assert payload["tentative_count"] == 1
        assert len(payload["rules"]) == 2

    def test_updated_at_set(self):
        payload = _build_learned_rules_payload([], updated_at="2026-03-29T12:00:00Z")
        assert payload["updated_at"] == "2026-03-29T12:00:00Z"
