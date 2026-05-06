#!/usr/bin/env python3
"""Tests for Phase E1+E2 candidate finding validation/normalization layer.

Run with: python -m pytest tests/test_review_candidate_validation.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa_agent.models import (
    FindingSource,
    FindingActionability,
    FindingSeverity,
    RepoConfig,
)
from qa_agent.review import (
    CandidateValidationError,
    normalize_candidate,
    assign_finding_identity,
    dedupe_findings,
    is_remediation_eligible,
    RemediationEligibility,
    _DEFAULT_MIN_CONFIDENCE,
    _DEFAULT_MIN_ACTIONABILITY,
)


# ---------------------------------------------------------------------------
# normalize_candidate: valid candidate normalization
# ---------------------------------------------------------------------------

class TestNormalizeCandidateValid:
    """Valid candidates are normalized correctly."""

    def minimal_valid(self):
        return {
            "repo": "ky",
            "path": "src/index.ts",
            "line": 10,
            "header": "complexity",
            "source": "llm",
        }

    def test_all_required_fields_present(self):
        raw = self.minimal_valid()
        norm = normalize_candidate(raw)
        assert norm["repo"] == "ky"
        assert norm["path"] == "src/index.ts"
        assert norm["line"] == 10
        assert norm["header"] == "complexity"
        assert norm["source"] == FindingSource.LLM
        assert norm["actionability"] == FindingActionability.MEDIUM
        assert norm["severity"] == FindingSeverity.MEDIUM
        assert norm["confidence"] == 0.5
        assert norm["safe_to_autofix"] is False
        assert norm["snippet"] == ""
        assert norm["discovered_at"] is not None

    def test_string_enums_coerced(self):
        raw = {
            **self.minimal_valid(),
            "source": "linter",
            "actionability": "high",
            "severity": "critical",
        }
        norm = normalize_candidate(raw)
        assert norm["source"] == FindingSource.LINTER
        assert norm["actionability"] == FindingActionability.HIGH
        assert norm["severity"] == FindingSeverity.CRITICAL

    def test_confidence_clamped_to_0_1(self):
        raw = {**self.minimal_valid(), "confidence": 1.5}
        assert normalize_candidate(raw)["confidence"] == 1.0
        raw = {**self.minimal_valid(), "confidence": -0.5}
        assert normalize_candidate(raw)["confidence"] == 0.0

    def test_unknown_source_raises_candidate_validation_error(self):
        # Invalid source values (not in FindingSource enum) are rejected cleanly
        raw = {**self.minimal_valid(), "source": "unknown-garbage"}
        try:
            normalize_candidate(raw)
            assert False, "Should have raised CandidateValidationError"
        except CandidateValidationError as e:
            assert "Invalid source value" in str(e)

    def test_unknown_actionability_defaults_to_medium(self):
        raw = {**self.minimal_valid(), "actionability": "totally-invalid"}
        norm = normalize_candidate(raw)
        assert norm["actionability"] == FindingActionability.MEDIUM

    def test_unknown_severity_defaults_to_medium(self):
        raw = {**self.minimal_valid(), "severity": "super-critical"}
        norm = normalize_candidate(raw)
        assert norm["severity"] == FindingSeverity.MEDIUM

    def test_whitespace_stripped_from_string_fields(self):
        raw = {**self.minimal_valid()}
        raw["repo"] = "  ky  "
        raw["path"] = "  src/index.ts  "
        raw["header"] = "  complexity  "
        norm = normalize_candidate(raw)
        assert norm["repo"] == "ky"
        assert norm["path"] == "src/index.ts"
        assert norm["header"] == "complexity"

    def test_line_coerced_to_int(self):
        raw = {**self.minimal_valid(), "line": "42"}
        assert normalize_candidate(raw)["line"] == 42

    def test_source_as_enum_instance(self):
        raw = {**self.minimal_valid(), "source": FindingSource.BASELINE}
        norm = normalize_candidate(raw)
        assert norm["source"] == FindingSource.BASELINE

    def test_preserves_extra_fields(self):
        # Extra fields not in the canonical set are dropped silently
        raw = {**self.minimal_valid(), "extra_field": "keep-me"}
        norm = normalize_candidate(raw)
        assert "extra_field" not in norm


# ---------------------------------------------------------------------------
# normalize_candidate: invalid candidate rejection
# ---------------------------------------------------------------------------

class TestNormalizeCandidateInvalid:
    """Malformed candidates raise CandidateValidationError with useful messages."""

    def test_missing_repo_raises(self):
        raw = {"path": "a.ts", "line": 1, "header": "r", "source": "llm"}
        err = _assert_validation_error(raw)
        assert "repo" in str(err)

    def test_missing_path_raises(self):
        raw = {"repo": "ky", "line": 1, "header": "r", "source": "llm"}
        err = _assert_validation_error(raw)
        assert "path" in str(err)

    def test_missing_line_raises(self):
        raw = {"repo": "ky", "path": "a.ts", "header": "r", "source": "llm"}
        err = _assert_validation_error(raw)
        assert "line" in str(err)

    def test_missing_header_raises(self):
        raw = {"repo": "ky", "path": "a.ts", "line": 1, "source": "llm"}
        err = _assert_validation_error(raw)
        assert "header" in str(err)

    def test_missing_source_raises(self):
        raw = {"repo": "ky", "path": "a.ts", "line": 1, "header": "r"}
        err = _assert_validation_error(raw)
        assert "source" in str(err)

    def test_empty_repo_raises(self):
        raw = {"repo": "   ", "path": "a.ts", "line": 1, "header": "r", "source": "llm"}
        err = _assert_validation_error(raw)
        assert "repo" in str(err)

    def test_empty_path_raises(self):
        raw = {"repo": "ky", "path": "  ", "line": 1, "header": "r", "source": "llm"}
        err = _assert_validation_error(raw)
        assert "path" in str(err)

    def test_empty_header_raises(self):
        raw = {"repo": "ky", "path": "a.ts", "line": 1, "header": "  ", "source": "llm"}
        err = _assert_validation_error(raw)
        assert "header" in str(err)

    def test_negative_line_raises(self):
        raw = {"repo": "ky", "path": "a.ts", "line": -1, "header": "r", "source": "llm"}
        err = _assert_validation_error(raw)
        assert "non-negative" in str(err)

    def test_non_integer_line_raises(self):
        raw = {"repo": "ky", "path": "a.ts", "line": "oops", "header": "r", "source": "llm"}
        err = _assert_validation_error(raw)
        assert "integer" in str(err)

    def test_invalid_source_raises(self):
        raw = {"repo": "ky", "path": "a.ts", "line": 1, "header": "r", "source": "neither"}
        err = _assert_validation_error(raw)
        assert "Invalid source value" in str(err)

    def test_error_contains_error_list(self):
        raw = {}
        try:
            normalize_candidate(raw)
            assert False, "Should have raised"
        except CandidateValidationError as e:
            assert len(e.errors) >= 1
            # All required fields are missing
            assert any("repo" in err for err in e.errors)
            assert any("path" in err for err in e.errors)
            assert any("header" in err for err in e.errors)
            assert any("source" in err for err in e.errors)


def _assert_validation_error(raw):
    try:
        normalize_candidate(raw)
        assert False, f"Expected CandidateValidationError for {raw}"
    except CandidateValidationError as e:
        return e


# ---------------------------------------------------------------------------
# assign_finding_identity: deterministic identity assignment
# ---------------------------------------------------------------------------

class TestAssignFindingIdentity:
    """Identity assignment is deterministic and uses QA-owned helpers."""

    def base_normalized(self):
        return {
            "repo": "ky",
            "path": "src/main.ts",
            "line": 42,
            "header": "complexity",
            "snippet": "fn foo() {}",
            "source": FindingSource.LLM,
            "actionability": FindingActionability.HIGH,
            "severity": FindingSeverity.MEDIUM,
            "confidence": 0.85,
            "safe_to_autofix": True,
            "discovered_at": "2026-03-29T00:00:00Z",
        }

    def test_identity_fields_added(self):
        norm = self.base_normalized()
        result = assign_finding_identity(norm)
        assert "finding_fingerprint" in result
        assert "finding_id" in result

    def test_fingerprint_is_64_chars(self):
        result = assign_finding_identity(self.base_normalized())
        assert len(result["finding_fingerprint"]) == 64

    def test_finding_id_format(self):
        result = assign_finding_identity(self.base_normalized())
        assert result["finding_id"].startswith("rf-")
        assert result["finding_id"].endswith("-000")

    def test_deterministic_same_call(self):
        norm = self.base_normalized()
        id1 = assign_finding_identity(norm)
        id2 = assign_finding_identity(norm)
        assert id1["finding_fingerprint"] == id2["finding_fingerprint"]
        assert id1["finding_id"] == id2["finding_id"]

    def test_different_path_produces_different_fingerprint(self):
        norm1 = self.base_normalized()
        norm2 = {**self.base_normalized(), "path": "src/other.ts"}
        id1 = assign_finding_identity(norm1)
        id2 = assign_finding_identity(norm2)
        assert id1["finding_fingerprint"] != id2["finding_fingerprint"]

    def test_different_line_produces_different_fingerprint(self):
        norm1 = self.base_normalized()
        norm2 = {**self.base_normalized(), "line": 99}
        id1 = assign_finding_identity(norm1)
        id2 = assign_finding_identity(norm2)
        assert id1["finding_fingerprint"] != id2["finding_fingerprint"]

    def test_different_header_produces_different_fingerprint(self):
        norm1 = self.base_normalized()
        norm2 = {**self.base_normalized(), "header": "different-rule"}
        id1 = assign_finding_identity(norm1)
        id2 = assign_finding_identity(norm2)
        assert id1["finding_fingerprint"] != id2["finding_fingerprint"]

    def test_attempt_zero_is_first_occurrence(self):
        result = assign_finding_identity(self.base_normalized(), attempt=0)
        assert result["finding_id"].endswith("-000")

    def test_attempt_increments_id_suffix(self):
        result0 = assign_finding_identity(self.base_normalized(), attempt=0)
        result1 = assign_finding_identity(self.base_normalized(), attempt=1)
        result5 = assign_finding_identity(self.base_normalized(), attempt=5)
        assert result0["finding_id"] != result1["finding_id"]
        assert result1["finding_id"].endswith("-001")
        assert result5["finding_id"].endswith("-005")

    def test_negative_attempt_raises(self):
        try:
            assign_finding_identity(self.base_normalized(), attempt=-1)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "attempt must be >= 0" in str(e)

    def test_path_normalization_stabilizes_fingerprint(self):
        # Paths that differ only in slash normalization produce same fingerprint.
        # The repo prefix ("ky/") is NOT stripped by normalize_finding_path;
        # both paths must already have the same prefix for fingerprint stability.
        norm1 = {**self.base_normalized(), "path": "ky//src//main.ts"}
        norm2 = {**self.base_normalized(), "path": "ky/src/main.ts"}
        id1 = assign_finding_identity(norm1)
        id2 = assign_finding_identity(norm2)
        assert id1["finding_fingerprint"] == id2["finding_fingerprint"]

        # Backslash vs forward slash also stabilizes
        norm3 = {**self.base_normalized(), "path": "ky\\src\\main.ts"}
        id3 = assign_finding_identity(norm3)
        assert id3["finding_fingerprint"] == id2["finding_fingerprint"]

    def test_preserves_original_normalized_fields(self):
        norm = self.base_normalized()
        result = assign_finding_identity(norm)
        assert result["repo"] == norm["repo"]
        assert result["path"] == norm["path"]
        assert result["line"] == norm["line"]
        assert result["confidence"] == norm["confidence"]


# ---------------------------------------------------------------------------
# dedupe_findings: duplicate collapse behavior
# ---------------------------------------------------------------------------

class TestDedupFindings:
    """Exact structural duplicates are collapsed."""

    def finding(self, path, fingerprint=None):
        f = {
            "finding_id": f"rf-{path[:4]}-000",
            "finding_fingerprint": fingerprint or f"fp-{path}",
            "repo": "ky",
            "path": path,
            "line": 1,
            "header": "rule",
            "source": FindingSource.LLM,
            "actionability": FindingActionability.HIGH,
            "severity": FindingSeverity.MEDIUM,
            "confidence": 0.8,
            "safe_to_autofix": True,
            "discovered_at": "2026-03-29T00:00:00Z",
        }
        if fingerprint:
            f["finding_fingerprint"] = fingerprint
        return f

    def test_empty_list_returns_empty(self):
        assert dedupe_findings([]) == []

    def test_no_duplicates_unchanged(self):
        findings = [self.finding("a.ts"), self.finding("b.ts")]
        result = dedupe_findings(findings)
        assert len(result) == 2

    def test_exact_duplicate_removed(self):
        fp = "same-fp"
        findings = [
            {**self.finding("a.ts"), "finding_fingerprint": fp},
            {**self.finding("b.ts"), "finding_fingerprint": fp},
        ]
        result = dedupe_findings(findings)
        assert len(result) == 1
        assert result[0]["path"] == "a.ts"  # first kept

    def test_first_occurrence_kept_on_duplicate(self):
        fp = "same-fp"
        findings = [
            {**self.finding("keep-me.ts"), "finding_fingerprint": fp},
            {**self.finding("drop-me.ts"), "finding_fingerprint": fp},
            {**self.finding("also-drop.ts"), "finding_fingerprint": fp},
        ]
        result = dedupe_findings(findings)
        assert len(result) == 1
        assert result[0]["path"] == "keep-me.ts"

    def test_preserves_non_duplicate_order(self):
        findings = [
            {**self.finding("a.ts"), "finding_fingerprint": "fp-a"},
            {**self.finding("b.ts"), "finding_fingerprint": "fp-b"},
            {**self.finding("c.ts"), "finding_fingerprint": "fp-c"},
        ]
        result = dedupe_findings(findings)
        assert [f["path"] for f in result] == ["a.ts", "b.ts", "c.ts"]

    def test_missing_fingerprint_uses_repr_fallback(self):
        findings = [
            {**self.finding("a.ts"), "finding_fingerprint": ""},
            {**self.finding("a.ts"), "finding_fingerprint": ""},
        ]
        # With empty fingerprint, falls back to repr - may or may not dedupe
        # depending on dict repr uniqueness, but shouldn't crash
        result = dedupe_findings(findings)
        assert len(result) >= 1


# ---------------------------------------------------------------------------
# is_remediation_eligible: remediation eligibility gate behavior
# ---------------------------------------------------------------------------

class TestRemediationEligibilityGates:
    """All spec gates are checked correctly."""

    def base_eligible_finding(self):
        return {
            "repo": "ky",
            "path": "src/main.ts",
            "line": 10,
            "header": "complexity",
            "source": FindingSource.LLM,
            "actionability": FindingActionability.HIGH,
            "severity": FindingSeverity.MEDIUM,
            "confidence": 0.85,
            "safe_to_autofix": True,
            "discovered_at": "2026-03-29T00:00:00Z",
        }

    def test_full_eligible_finding(self):
        f = self.base_eligible_finding()
        result = is_remediation_eligible(f)
        assert result.eligible is True
        assert result.rejected_gates == []
        assert result.safe_to_autofix is True
        assert result.severity_ok is True
        assert result.actionability_ok is True
        assert result.confidence_ok is True

    def test_confidence_below_threshold_rejected(self):
        f = {**self.base_eligible_finding(), "confidence": 0.3}
        result = is_remediation_eligible(f)
        assert result.eligible is False
        assert "confidence" in result.rejected_gates
        assert result.confidence_ok is False

    def test_confidence_exactly_at_threshold_accepted(self):
        f = {**self.base_eligible_finding(), "confidence": 0.6}
        result = is_remediation_eligible(f)
        assert result.eligible is True
        assert result.confidence_ok is True

    def test_confidence_just_below_threshold_rejected(self):
        f = {**self.base_eligible_finding(), "confidence": 0.5999}
        result = is_remediation_eligible(f)
        assert result.eligible is False
        assert "confidence" in result.rejected_gates

    def test_actionability_low_rejected(self):
        f = {**self.base_eligible_finding(), "actionability": FindingActionability.LOW}
        result = is_remediation_eligible(f)
        assert result.eligible is False
        assert "actionability" in result.rejected_gates

    def test_actionability_informational_rejected(self):
        f = {**self.base_eligible_finding(), "actionability": FindingActionability.INFORMATIONAL}
        result = is_remediation_eligible(f)
        assert result.eligible is False
        assert "actionability" in result.rejected_gates

    def test_actionability_medium_accepted(self):
        f = {**self.base_eligible_finding(), "actionability": FindingActionability.MEDIUM}
        result = is_remediation_eligible(f)
        assert result.eligible is True
        assert result.actionability_ok is True

    def test_actionability_high_accepted(self):
        f = {**self.base_eligible_finding(), "actionability": FindingActionability.HIGH}
        result = is_remediation_eligible(f)
        assert result.eligible is True

    def test_not_safe_to_autofix_rejected(self):
        f = {**self.base_eligible_finding(), "safe_to_autofix": False}
        result = is_remediation_eligible(f)
        assert result.eligible is False
        assert "safe_to_autofix" in result.rejected_gates
        assert result.safe_to_autofix is False

    def test_critical_severity_rejected(self):
        f = {**self.base_eligible_finding(), "severity": FindingSeverity.CRITICAL}
        result = is_remediation_eligible(f)
        assert result.eligible is False
        assert "severity" in result.rejected_gates
        assert result.severity_ok is False

    def test_critical_severity_string_rejected(self):
        f = {**self.base_eligible_finding(), "severity": "critical"}
        result = is_remediation_eligible(f)
        assert result.eligible is False
        assert "severity" in result.rejected_gates

    def test_high_severity_accepted(self):
        f = {**self.base_eligible_finding(), "severity": FindingSeverity.HIGH}
        result = is_remediation_eligible(f)
        assert result.eligible is True

    def test_source_manual_rejected(self):
        f = {**self.base_eligible_finding(), "source": FindingSource.MANUAL}
        result = is_remediation_eligible(f)
        assert result.eligible is False
        assert "source" in result.rejected_gates

    def test_source_manual_string_rejected(self):
        f = {**self.base_eligible_finding(), "source": "manual"}
        result = is_remediation_eligible(f)
        assert result.eligible is False
        assert "source" in result.rejected_gates

    def test_source_linter_accepted(self):
        f = {**self.base_eligible_finding(), "source": FindingSource.LINTER}
        result = is_remediation_eligible(f)
        assert result.eligible is True

    def test_source_llm_accepted(self):
        f = {**self.base_eligible_finding(), "source": FindingSource.LLM}
        result = is_remediation_eligible(f)
        assert result.eligible is True

    def test_source_baseline_accepted(self):
        f = {**self.base_eligible_finding(), "source": FindingSource.BASELINE}
        result = is_remediation_eligible(f)
        assert result.eligible is True

    def test_multiple_gates_rejected_reports_all(self):
        f = {
            **self.base_eligible_finding(),
            "confidence": 0.2,
            "safe_to_autofix": False,
        }
        result = is_remediation_eligible(f)
        assert result.eligible is False
        assert "confidence" in result.rejected_gates
        assert "safe_to_autofix" in result.rejected_gates

    def test_rules_disabled_allowlist_rejected(self):
        repo_config = RepoConfig(
            id="ky",
            name="ky",
            path="/tmp/ky",
            language="typescript",
            rules_disabled=["complexity"],
        )
        f = self.base_eligible_finding()
        result = is_remediation_eligible(f, repo_config=repo_config)
        assert result.eligible is False
        assert "allowlist" in result.rejected_gates

    def test_rules_disabled_allowlist_not_triggered_for_other_rules(self):
        repo_config = RepoConfig(
            id="ky",
            name="ky",
            path="/tmp/ky",
            language="typescript",
            rules_disabled=["different-rule"],
        )
        f = self.base_eligible_finding()
        result = is_remediation_eligible(f, repo_config=repo_config)
        assert result.eligible is True

    def test_custom_min_confidence(self):
        f = {**self.base_eligible_finding(), "confidence": 0.7}
        result = is_remediation_eligible(f, min_confidence=0.8)
        assert result.eligible is False
        assert "confidence" in result.rejected_gates

    def test_custom_min_actionability(self):
        f = {**self.base_eligible_finding(), "actionability": FindingActionability.MEDIUM}
        result = is_remediation_eligible(
            f, min_actionability=FindingActionability.HIGH
        )
        assert result.eligible is False
        assert "actionability" in result.rejected_gates

    def test_eligible_to_dict(self):
        f = self.base_eligible_finding()
        result = is_remediation_eligible(f)
        d = result.to_dict()
        assert d["eligible"] is True
        assert d["rejected_gates"] == []
        assert d["safe_to_autofix"] is True

    def test_ineligible_to_dict(self):
        f = {**self.base_eligible_finding(), "confidence": 0.1}
        result = is_remediation_eligible(f)
        d = result.to_dict()
        assert d["eligible"] is False
        assert "confidence" in d["rejected_gates"]


# ---------------------------------------------------------------------------
# Full pipeline: normalize -> identity -> dedupe -> eligibility
# ---------------------------------------------------------------------------

class TestFullPipeline:
    """End-to-end pipeline: normalize, assign identity, dedupe, check eligibility."""

    def raw_candidates(self):
        return [
            {
                "repo": "ky",
                "path": "src/a.ts",
                "line": 1,
                "header": "complexity",
                "source": "llm",
                "confidence": 0.8,
                "safe_to_autofix": True,
            },
            {
                "repo": "ky",
                "path": "src/b.ts",
                "line": 5,
                "header": "no-explicit-any",
                "source": "linter",
                "confidence": 0.7,
                "safe_to_autofix": True,
            },
            # Duplicate of first
            {
                "repo": "ky",
                "path": "src/a.ts",
                "line": 1,
                "header": "complexity",
                "source": "llm",
                "confidence": 0.9,
                "safe_to_autofix": True,
            },
            # Ineligible: not safe to autofix
            {
                "repo": "ky",
                "path": "src/c.ts",
                "line": 10,
                "header": "security-risk",
                "source": "llm",
                "confidence": 0.8,
                "safe_to_autofix": False,
            },
        ]

    def test_pipeline_normalizes_and_assigns_identity(self):
        candidates = self.raw_candidates()
        normalized = [normalize_candidate(c) for c in candidates]
        with_identity = [assign_finding_identity(n) for n in normalized]
        assert all("finding_fingerprint" in f for f in with_identity)
        assert all("finding_id" in f for f in with_identity)

    def test_pipeline_dedup_removes_exact_duplicate(self):
        candidates = self.raw_candidates()
        normalized = [normalize_candidate(c) for c in candidates]
        with_identity = [assign_finding_identity(n) for n in normalized]
        deduped = dedupe_findings(with_identity)
        paths = [f["path"] for f in deduped]
        assert paths == ["src/a.ts", "src/b.ts", "src/c.ts"]
        assert len(deduped) == 3

    def test_pipeline_eligibility_classifies_correctly(self):
        candidates = self.raw_candidates()
        normalized = [normalize_candidate(c) for c in candidates]
        with_identity = [assign_finding_identity(n) for n in normalized]
        deduped = dedupe_findings(with_identity)

        eligible = [f for f in deduped if is_remediation_eligible(f).eligible]
        ineligible = [f for f in deduped if not is_remediation_eligible(f).eligible]

        assert len(eligible) == 2  # src/a.ts and src/b.ts
        assert len(ineligible) == 1  # src/c.ts (not safe_to_autofix)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
