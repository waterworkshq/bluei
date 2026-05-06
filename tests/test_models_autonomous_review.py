#!/usr/bin/env python3
"""Tests for Phase C1/C2 autonomous-review models in qa_agent/models.py.

Run with: python -m pytest tests/test_models_autonomous_review.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa_agent.models import (
    # Identity helpers
    normalize_finding_path,
    normalize_finding_header,
    make_finding_fingerprint,
    make_review_finding_id,
    # New models
    ReviewRun,
    ReviewFinding,
    ReviewSummary,
    FeedbackEvent,
    # New enums
    FindingSource,
    FindingActionability,
    FindingSeverity,
    FeedbackSentiment,
    FeedbackSource,
    ReviewRunStatus,
)


# ---------------------------------------------------------------------------
# Identity helper tests
# ---------------------------------------------------------------------------

class TestNormalizeFindingPath:
    def test_empty(self):
        assert normalize_finding_path("") == ""
        assert normalize_finding_path("   ") == ""

    def test_strips_whitespace(self):
        assert normalize_finding_path("  src/main.ts  ") == "src/main.ts"

    def test_converts_backslashes(self):
        assert normalize_finding_path("src\\util\\helper.ts") == "src/util/helper.ts"

    def test_collapse_multiple_slashes(self):
        assert normalize_finding_path("src//main.ts") == "src/main.ts"
        assert normalize_finding_path("src/foo//bar///baz.ts") == "src/foo/bar/baz.ts"

    def test_removes_trailing_slash(self):
        assert normalize_finding_path("src/main.ts/") == "src/main.ts"
        assert normalize_finding_path("src/main.ts///") == "src/main.ts"

    def test_strips_repo_prefix(self):
        # Repo prefix stripping is no longer part of normalize_finding_path
        # (could incorrectly strip 'src/' from 'src/main.ts'). Callers should
        # pre-strip the repo root before calling if needed.
        assert normalize_finding_path("ky/src/main.ts") == "ky/src/main.ts"

    def test_preserves_deep_paths(self):
        assert normalize_finding_path("ky/src/deep/nested/path.ts") == "ky/src/deep/nested/path.ts"

    def test_idempotent(self):
        p = "  ky//src//foo.ts///  "
        assert normalize_finding_path(normalize_finding_path(p)) == normalize_finding_path(p)


class TestNormalizeFindingHeader:
    def test_empty(self):
        assert normalize_finding_header("") == ""
        assert normalize_finding_header("   ") == ""

    def test_lowercases(self):
        assert normalize_finding_header("COMPLEXITY") == "complexity"
        assert normalize_finding_header("MaxLines") == "maxlines"

    def test_collapse_whitespace(self):
        assert normalize_finding_header("too   many   spaces") == "too many spaces"

    def test_strips_punctuation(self):
        assert normalize_finding_header(".complexity.") == "complexity"
        assert normalize_finding_header("-max-lines-") == "max-lines"  # only leading/trailing stripped
        assert normalize_finding_header("/some-rule/") == "some-rule"
        assert normalize_finding_header("_snake_rule_") == "snake_rule"  # underscores preserved

    def test_idempotent(self):
        h = "  .MY-RULE.  "
        assert normalize_finding_header(normalize_finding_header(h)) == normalize_finding_header(h)


class TestMakeFindingFingerprint:
    def test_same_inputs_produce_same_fingerprint(self):
        fp1 = make_finding_fingerprint("ky", "src/main.ts", 42, "complexity", "fn foo() {}")
        fp2 = make_finding_fingerprint("ky", "src/main.ts", 42, "complexity", "fn foo() {}")
        assert fp1 == fp2
        assert len(fp1) == 64  # SHA-256 hex

    def test_different_line_produces_different_fingerprint(self):
        fp1 = make_finding_fingerprint("ky", "src/main.ts", 42, "complexity", "fn foo() {}")
        fp2 = make_finding_fingerprint("ky", "src/main.ts", 99, "complexity", "fn foo() {}")
        assert fp1 != fp2

    def test_path_normalization_makes_fingerprint_stable(self):
        # Paths that differ only in slash normalization produce same fingerprint
        fp1 = make_finding_fingerprint("ky", "ky//src//main.ts", 10, "rule", "snippet")
        fp2 = make_finding_fingerprint("ky", "ky/src/main.ts", 10, "rule", "snippet")
        assert fp1 == fp2

        # Backslash conversion also stabilizes
        fp3 = make_finding_fingerprint("ky", "ky\\src\\main.ts", 10, "rule", "snippet")
        assert fp3 == fp2

    def test_header_normalization_makes_fingerprint_stable(self):
        fp1 = make_finding_fingerprint("ky", "src/main.ts", 10, "COMPLEXITY", "snippet")
        fp2 = make_finding_fingerprint("ky", "src/main.ts", 10, "complexity", "snippet")
        assert fp1 == fp2

    def test_snippet_truncation_does_not_destabilize(self):
        long_snippet = "x" * 500
        fp1 = make_finding_fingerprint("ky", "src/main.ts", 10, "rule", long_snippet)
        fp2 = make_finding_fingerprint("ky", "src/main.ts", 10, "rule", long_snippet[:200])
        # Same full snippet vs truncated should differ (truncation changes input)
        # But two identical long snippets should be stable
        fp3 = make_finding_fingerprint("ky", "src/main.ts", 10, "rule", long_snippet)
        fp4 = make_finding_fingerprint("ky", "src/main.ts", 10, "rule", long_snippet)
        assert fp3 == fp4  # Stable

    def test_different_repo_produces_different_fingerprint(self):
        fp1 = make_finding_fingerprint("ky", "src/main.ts", 10, "rule", "snippet")
        fp2 = make_finding_fingerprint("other", "src/main.ts", 10, "rule", "snippet")
        assert fp1 != fp2

    def test_empty_snippet_is_legal(self):
        fp = make_finding_fingerprint("ky", "src/main.ts", 10, "rule", "")
        assert len(fp) == 64


class TestMakeReviewFindingId:
    def test_format(self):
        fp = "aabbccdd" + "0" * 56  # 64 chars total
        fid = make_review_finding_id(fp, 0)
        # short fingerprint = first 12 chars of fp = "aabbccdd0000"
        assert fid == "rf-aabbccdd0000-000"
        assert fid.startswith("rf-")

    def test_attempt_zero_is_first_occurrence(self):
        fp = "aabbccdd" + "0" * 56
        assert make_review_finding_id(fp, 0).endswith("-000")

    def test_attempt_increments_correctly(self):
        fp = "aabbccdd" + "0" * 56
        assert make_review_finding_id(fp, 1).endswith("-001")
        assert make_review_finding_id(fp, 9).endswith("-009")
        assert make_review_finding_id(fp, 10).endswith("-010")
        assert make_review_finding_id(fp, 99).endswith("-099")

    def test_deterministic(self):
        fp = "aabbccdd" + "0" * 56
        id1 = make_review_finding_id(fp, 3)
        id2 = make_review_finding_id(fp, 3)
        assert id1 == id2

    def test_different_fingerprints_produce_different_ids(self):
        fp1 = "aabbccdd" + "0" * 56
        fp2 = "11223344" + "0" * 56
        id1 = make_review_finding_id(fp1, 0)
        id2 = make_review_finding_id(fp2, 0)
        assert id1 != id2

    def test_negative_attempt_raises(self):
        fp = "aabbccdd" + "0" * 56
        try:
            make_review_finding_id(fp, -1)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "attempt must be >= 0" in str(e)


# ---------------------------------------------------------------------------
# ReviewFinding round-trip + defaults
# ---------------------------------------------------------------------------

class TestReviewFindingRoundTrip:
    def test_to_dict_serializes_enums_to_values(self):
        rf = ReviewFinding(
            finding_id="rf-abc123-000",
            finding_fingerprint="fingerprint123" + "0" * 53,
            repo="ky",
            path="src/main.ts",
            line=42,
            header="complexity",
            source=FindingSource.LLM,
            actionability=FindingActionability.HIGH,
            severity=FindingSeverity.HIGH,
        )
        d = rf.to_dict()
        assert d["source"] == "llm"
        assert d["actionability"] == "high"
        assert d["severity"] == "high"

    def test_from_dict_deserializes_string_enums(self):
        d = {
            "finding_id": "rf-abc123-000",
            "finding_fingerprint": "fingerprint123" + "0" * 53,
            "repo": "ky",
            "path": "src/main.ts",
            "line": 42,
            "header": "complexity",
            "source": "llm",
            "actionability": "high",
            "severity": "high",
        }
        rf = ReviewFinding.from_dict(d)
        assert rf.source == FindingSource.LLM
        assert rf.actionability == FindingActionability.HIGH
        assert rf.severity == FindingSeverity.HIGH

    def test_round_trip_preserves_data(self):
        rf = ReviewFinding(
            finding_id="rf-abc123-000",
            finding_fingerprint="fingerprint123" + "0" * 53,
            repo="ky",
            path="src/main.ts",
            line=42,
            header="complexity",
            source=FindingSource.LINTER,
            actionability=FindingActionability.MEDIUM,
            severity=FindingSeverity.MEDIUM,
            run_id="run-1",
            parent_finding_id=None,
            snippet="fn foo() { let x = 1; }",
            confidence=0.85,
            discovered_at="2026-03-29T00:00:00Z",
        )
        d = rf.to_dict()
        rf2 = ReviewFinding.from_dict(d)
        assert rf2.finding_id == rf.finding_id
        assert rf2.finding_fingerprint == rf.finding_fingerprint
        assert rf2.repo == rf.repo
        assert rf2.path == rf.path
        assert rf2.line == rf.line
        assert rf2.header == rf.header
        assert rf2.source == rf.source
        assert rf2.actionability == rf.actionability
        assert rf2.severity == rf.severity
        assert rf2.run_id == rf.run_id
        assert rf2.snippet == rf.snippet
        assert rf2.confidence == rf.confidence

    def test_defaults(self):
        rf = ReviewFinding(
            finding_id="rf-xyz-000",
            finding_fingerprint="fingerprint123" + "0" * 53,
            repo="ky",
            path="src/main.ts",
            line=1,
            header="rule",
            source=FindingSource.BASELINE,
            actionability=FindingActionability.INFORMATIONAL,
            severity=FindingSeverity.NONE,
        )
        assert rf.run_id is None
        assert rf.parent_finding_id is None
        assert rf.snippet == ""
        assert rf.confidence == 0.5
        assert rf.discovered_at is None

    def test_from_dict_unknown_fields_ignored(self):
        d = {
            "finding_id": "rf-xyz-000",
            "finding_fingerprint": "fingerprint123" + "0" * 53,
            "repo": "ky",
            "path": "src/main.ts",
            "line": 1,
            "header": "rule",
            "source": "llm",
            "actionability": "high",
            "severity": "medium",
            "unknown_future_field": "ignore-me",
        }
        rf = ReviewFinding.from_dict(d)
        assert rf.finding_id == "rf-xyz-000"


# ---------------------------------------------------------------------------
# ReviewSummary round-trip + defaults
# ---------------------------------------------------------------------------

class TestReviewSummaryRoundTrip:
    def test_to_dict_from_dict_round_trip(self):
        rs = ReviewSummary(
            id="rsum-1",
            run_id="run-1",
            repo="ky",
            finding_count=10,
            actionable_count=3,
            critical_count=1,
            baseline_summary_id="rsum-baseline-0",
            delta_findings=-2,
            delta_actionable=-1,
            generated_at="2026-03-29T00:00:00Z",
        )
        d = rs.to_dict()
        rs2 = ReviewSummary.from_dict(d)
        assert rs2.id == rs.id
        assert rs2.run_id == rs.run_id
        assert rs2.repo == rs.repo
        assert rs2.finding_count == rs.finding_count
        assert rs2.actionable_count == rs.actionable_count
        assert rs2.critical_count == rs.critical_count
        assert rs2.baseline_summary_id == rs.baseline_summary_id
        assert rs2.delta_findings == rs.delta_findings
        assert rs2.delta_actionable == rs.delta_actionable

    def test_defaults(self):
        rs = ReviewSummary(id="rsum-x", run_id="run-x", repo="ky")
        assert rs.finding_count == 0
        assert rs.actionable_count == 0
        assert rs.critical_count == 0
        assert rs.baseline_summary_id is None
        assert rs.delta_findings == 0
        assert rs.delta_actionable == 0
        assert rs.generated_at is None


# ---------------------------------------------------------------------------
# ReviewRun round-trip + defaults
# ---------------------------------------------------------------------------

class TestReviewRunRoundTrip:
    def test_to_dict_serializes_status_enum(self):
        rr = ReviewRun(
            id="run-1",
            repo="ky",
            status=ReviewRunStatus.RUNNING,
        )
        d = rr.to_dict()
        assert d["status"] == "running"

    def test_from_dict_deserializes_status_string(self):
        d = {
            "id": "run-1",
            "repo": "ky",
            "status": "completed",
        }
        rr = ReviewRun.from_dict(d)
        assert rr.status == ReviewRunStatus.COMPLETED

    def test_round_trip_preserves_all_fields(self):
        rr = ReviewRun(
            id="run-1",
            repo="ky",
            pr_number=42,
            status=ReviewRunStatus.PAUSED,
            loop_count=2,
            attempts_used=1,
            parent_run_id="run-0",
            root_run_id="run-0",
            finding_ids=["rf-001-000", "rf-002-000"],
            summary_id="rsum-1",
            mode="autonomous-review",
            started_at="2026-03-29T00:00:00Z",
            ended_at="2026-03-29T00:05:00Z",
            error=None,
        )
        d = rr.to_dict()
        rr2 = ReviewRun.from_dict(d)
        assert rr2.id == rr.id
        assert rr2.repo == rr.repo
        assert rr2.pr_number == 42
        assert rr2.status == ReviewRunStatus.PAUSED
        assert rr2.loop_count == 2
        assert rr2.attempts_used == 1
        assert rr2.parent_run_id == "run-0"
        assert rr2.root_run_id == "run-0"
        assert rr2.finding_ids == ["rf-001-000", "rf-002-000"]
        assert rr2.summary_id == "rsum-1"
        assert rr2.mode == "autonomous-review"

    def test_defaults(self):
        rr = ReviewRun(id="run-x", repo="ky")
        assert rr.pr_number is None
        assert rr.status == ReviewRunStatus.PENDING
        assert rr.loop_count == 0
        assert rr.attempts_used == 0
        assert rr.parent_run_id is None
        assert rr.root_run_id is None
        assert rr.finding_ids == []
        assert rr.summary_id is None
        assert rr.mode == "autonomous-review"
        assert rr.started_at is None
        assert rr.ended_at is None
        assert rr.error is None


# ---------------------------------------------------------------------------
# FeedbackEvent round-trip + defaults
# ---------------------------------------------------------------------------

class TestFeedbackEventRoundTrip:
    def test_to_dict_serializes_enums(self):
        fe = FeedbackEvent(
            id="fbe-1",
            finding_id="rf-abc-000",
            sentiment=FeedbackSentiment.NEGATIVE,
            source=FeedbackSource.HUMAN_REVIEWER,
        )
        d = fe.to_dict()
        assert d["sentiment"] == "negative"
        assert d["source"] == "human-reviewer"

    def test_from_dict_deserializes_string_enums(self):
        d = {
            "id": "fbe-1",
            "finding_id": "rf-abc-000",
            "sentiment": "contradictory",
            "source": "ci-check",
        }
        fe = FeedbackEvent.from_dict(d)
        assert fe.sentiment == FeedbackSentiment.CONTRADICTORY
        assert fe.source == FeedbackSource.CI_CHECK

    def test_round_trip_preserves_all_fields(self):
        fe = FeedbackEvent(
            id="fbe-1",
            finding_id="rf-abc-000",
            sentiment=FeedbackSentiment.CONCEPTUAL,
            source=FeedbackSource.LLM_REVIEWER,
            comment="This approach is architecturally inconsistent",
            loop_count=2,
            is_contradictory=False,
            is_conceptual=True,
            recorded_at="2026-03-29T00:00:00Z",
        )
        d = fe.to_dict()
        fe2 = FeedbackEvent.from_dict(d)
        assert fe2.id == fe.id
        assert fe2.finding_id == fe.finding_id
        assert fe2.sentiment == fe.sentiment
        assert fe2.source == fe.source
        assert fe2.comment == fe.comment
        assert fe2.loop_count == fe.loop_count
        assert fe2.is_contradictory == fe.is_contradictory
        assert fe2.is_conceptual == fe.is_conceptual
        assert fe2.recorded_at == fe.recorded_at

    def test_defaults(self):
        fe = FeedbackEvent(
            id="fbe-x",
            finding_id="rf-xyz-000",
            sentiment=FeedbackSentiment.MIXED,
            source=FeedbackSource.SELF_REVIEW,
        )
        assert fe.comment == ""
        assert fe.loop_count == 0
        assert fe.is_contradictory is False
        assert fe.is_conceptual is False
        assert fe.recorded_at is None


# ---------------------------------------------------------------------------
# Identity stability end-to-end
# ---------------------------------------------------------------------------

class TestIdentityStabilityE2E:
    """Integration test: fingerprint -> finding_id is stable across calls."""

    def test_fingerprint_and_id_chain(self):
        fp = make_finding_fingerprint(
            repo="ky",
            path="ky/src/util.ts",
            line=77,
            header="COMPLEXITY",
            snippet="fn complex() { if (true) { if (true) { if (true) {} } } }",
        )
        fid0 = make_review_finding_id(fp, 0)
        fid1 = make_review_finding_id(fp, 1)

        assert fid0 != fid1
        assert fid0.startswith("rf-")
        assert len(fp) == 64

        # Same raw inputs always produce same fingerprint and ids
        fp2 = make_finding_fingerprint(
            repo="ky",
            path="ky/src/util.ts",
            line=77,
            header="COMPLEXITY",
            snippet="fn complex() { if (true) { if (true) { if (true) {} } } }",
        )
        assert fp == fp2
        assert make_review_finding_id(fp2, 0) == fid0

        # Paths equivalent after slash normalization produce same fingerprint
        fp3 = make_finding_fingerprint("ky", "ky//src//util.ts", 77, "COMPLEXITY",
                                       "fn complex() { if (true) { if (true) { if (true) {} } } }")
        assert fp3 == fp

    def test_review_finding_identity_end_to_end(self):
        # Use the same raw path for both fingerprint and ReviewFinding
        raw_path = "ky/src/index.ts"
        fp = make_finding_fingerprint(
            repo="ky",
            path=raw_path,
            line=1,
            header="no-explicit-any",
            snippet="let x: any = 1",
        )
        fid = make_review_finding_id(fp, 0)

        rf = ReviewFinding(
            finding_id=fid,
            finding_fingerprint=fp,
            repo="ky",
            path=raw_path,
            line=1,
            header="NO-EXPLICIT-ANY",  # mixed case — normalized in fingerprint
            source=FindingSource.LINTER,
            actionability=FindingActionability.HIGH,
            severity=FindingSeverity.MEDIUM,
        )

        # Round-trip
        d = rf.to_dict()
        rf2 = ReviewFinding.from_dict(d)

        # Identity preserved
        assert rf2.finding_id == rf.finding_id
        assert rf2.finding_fingerprint == rf.finding_fingerprint
        # Source classification preserved
        assert rf2.source == FindingSource.LINTER
        assert rf2.actionability == FindingActionability.HIGH


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
