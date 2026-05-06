#!/usr/bin/env python3
"""Tests for Phase F1 publish-state + reconciliation helpers.

Run with: python -m pytest tests/test_review_publish_state.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa_agent.models import PublishStatus
from qa_agent.review import (
    reconcile_publish_state,
    build_publish_entry,
    compute_run_publish_status,
    build_run_publish_entry,
    build_review_summary_comment,
    ReconciliationResult,
)

# ---------------------------------------------------------------------------
# Test fixtures / shared data
# ---------------------------------------------------------------------------

PRIOR_PUBLISH_STATE_BASE = {
    "version": 1,
    "updated_at": "2026-03-29T00:00:00Z",
    "findings": {},
    "runs": {},
}


def make_candidate(finding_id: str, fingerprint: str = "") -> dict:
    """Minimal candidate finding dict for reconciliation."""
    return {"finding_id": finding_id, "finding_fingerprint": fingerprint or f"fp-{finding_id}"}


def make_prior_entry(
    status: PublishStatus | str,
    run_id: str = "run-prior",
    fingerprint: str = "",
    error: str = None,
) -> dict:
    entry: dict = {
        "status": status.value if isinstance(status, PublishStatus) else str(status),
        "updated_at": "2026-03-28T00:00:00Z",
        "run_id": run_id,
    }
    if fingerprint:
        entry["finding_fingerprint"] = fingerprint
    if error:
        entry["error"] = error
    return entry


# ---------------------------------------------------------------------------
# ReconciliationResult dataclass
# ---------------------------------------------------------------------------

class TestReconciliationResultDefaults:
    def test_empty_result_has_empty_lists(self):
        r = ReconciliationResult()
        assert r.new_findings == []
        assert r.already_published == []
        assert r.absent_findings == []
        assert r.superseded_findings == []
        assert r.pending_findings == []
        assert r.all_prior_findings == []

    def test_to_dict_round_trip(self):
        r = ReconciliationResult(
            new_findings=["rf-001"],
            already_published=["rf-002"],
            absent_findings=["rf-003"],
            superseded_findings=["rf-004"],
            pending_findings=["rf-005"],
            all_prior_findings=["rf-002", "rf-003", "rf-004", "rf-005"],
        )
        d = r.to_dict()
        r2 = ReconciliationResult(**d)
        assert r2.new_findings == ["rf-001"]
        assert r2.already_published == ["rf-002"]
        assert r2.absent_findings == ["rf-003"]
        assert r2.superseded_findings == ["rf-004"]
        assert r2.pending_findings == ["rf-005"]


# ---------------------------------------------------------------------------
# PublishStatus enum
# ---------------------------------------------------------------------------

class TestPublishStatusValues:
    def test_all_expected_values_present(self):
        expected = {"absent", "pending", "published", "failed", "skipped", "superseded"}
        actual = {s.value for s in PublishStatus}
        assert actual == expected

    def test_string_comparison_works_in_state_dicts(self):
        # This is the pattern used in review_publish_state.json
        entry = {"status": PublishStatus.PUBLISHED.value}
        assert entry["status"] == "published"
        assert entry["status"] == PublishStatus.PUBLISHED.value

    def test_is_valid_for_dict_value(self):
        statuses = [
            PublishStatus.ABSENT,
            PublishStatus.PENDING,
            PublishStatus.PUBLISHED,
            PublishStatus.FAILED,
            PublishStatus.SKIPPED,
            PublishStatus.SUPERSEDED,
        ]
        for s in statuses:
            assert isinstance(s.value, str)
            assert len(s.value) > 0


# ---------------------------------------------------------------------------
# build_publish_entry
# ---------------------------------------------------------------------------

class TestBuildPublishEntry:
    def test_required_fields(self):
        entry = build_publish_entry("rf-001", PublishStatus.PENDING)
        assert entry["status"] == "pending"
        assert "updated_at" in entry

    def test_optional_run_id(self):
        entry = build_publish_entry("rf-001", PublishStatus.PUBLISHED, run_id="run-42")
        assert entry["run_id"] == "run-42"

    def test_optional_error(self):
        entry = build_publish_entry("rf-001", PublishStatus.FAILED, error="rate limit")
        assert entry["error"] == "rate limit"

    def test_optional_fingerprint(self):
        fp = "abc123" + "0" * 56
        entry = build_publish_entry("rf-001", PublishStatus.PENDING, finding_fingerprint=fp)
        assert entry["finding_fingerprint"] == fp

    def test_all_options_together(self):
        fp = "fingerprint"
        entry = build_publish_entry(
            "rf-001",
            PublishStatus.FAILED,
            run_id="run-x",
            error="boom",
            finding_fingerprint=fp,
        )
        assert entry["status"] == "failed"
        assert entry["run_id"] == "run-x"
        assert entry["error"] == "boom"
        assert entry["finding_fingerprint"] == fp


# ---------------------------------------------------------------------------
# compute_run_publish_status
# ---------------------------------------------------------------------------

class TestComputeRunPublishStatus:
    def test_empty_is_pending(self):
        assert compute_run_publish_status([]) == PublishStatus.PENDING

    def test_all_published_is_published(self):
        assert compute_run_publish_status([
            PublishStatus.PUBLISHED,
            PublishStatus.PUBLISHED,
        ]) == PublishStatus.PUBLISHED

    def test_any_failed_wins(self):
        assert compute_run_publish_status([
            PublishStatus.PUBLISHED,
            PublishStatus.FAILED,
            PublishStatus.PUBLISHED,
        ]) == PublishStatus.FAILED

    def test_pending_wins_over_published(self):
        assert compute_run_publish_status([
            PublishStatus.PUBLISHED,
            PublishStatus.PENDING,
        ]) == PublishStatus.PENDING

    def test_skipped_wins_over_published(self):
        assert compute_run_publish_status([
            PublishStatus.PUBLISHED,
            PublishStatus.SKIPPED,
        ]) == PublishStatus.PENDING  # pending-like rollup

    def test_superseded_wins_over_published(self):
        assert compute_run_publish_status([
            PublishStatus.PUBLISHED,
            PublishStatus.SUPERSEDED,
        ]) == PublishStatus.PENDING  # pending-like rollup

    def test_mixed_all_published(self):
        assert compute_run_publish_status([
            PublishStatus.PUBLISHED,
            PublishStatus.PUBLISHED,
        ]) == PublishStatus.PUBLISHED

    def test_failed_alone(self):
        assert compute_run_publish_status([PublishStatus.FAILED]) == PublishStatus.FAILED

    def test_absent_alone(self):
        assert compute_run_publish_status([PublishStatus.ABSENT]) == PublishStatus.ABSENT

    def test_absent_and_published(self):
        # Absent is "resolved" so rollup follows published
        assert compute_run_publish_status([
            PublishStatus.PUBLISHED,
            PublishStatus.ABSENT,
        ]) == PublishStatus.PUBLISHED


# ---------------------------------------------------------------------------
# build_run_publish_entry
# ---------------------------------------------------------------------------

class TestBuildRunPublishEntry:
    def test_required_fields(self):
        entry = build_run_publish_entry(PublishStatus.PENDING)
        assert entry["status"] == "pending"
        assert "updated_at" in entry

    def test_findings_counts(self):
        entry = build_run_publish_entry(
            PublishStatus.PUBLISHED,
            run_id="run-42",
            findings_total=10,
            findings_published=8,
            findings_failed=2,
        )
        assert entry["findings_total"] == 10
        assert entry["findings_published"] == 8
        assert entry["findings_failed"] == 2

    def test_optional_error(self):
        entry = build_run_publish_entry(PublishStatus.FAILED, error="network timeout")
        assert entry["error"] == "network timeout"

    def test_optional_run_id(self):
        entry = build_run_publish_entry(PublishStatus.PUBLISHED, run_id="run-001")
        assert entry["run_id"] == "run-001"

    def test_operator_action_fields(self):
        entry = build_run_publish_entry(
            PublishStatus.FAILED,
            auto_rollback_active=True,
            auto_rollback_reason="negative-feedback-ratio-0.67-over-threshold-0.30",
            auto_rollback_triggered_at="2026-04-12T06:00:00Z",
            operator_action_required=True,
            operator_action_summary="disable-guarded-live-review-and-fallback-to-shadow",
            suggested_review_care_patch={
                "guarded_live_review": False,
                "live_rollout_mode": "shadow",
            },
        )
        assert entry["auto_rollback_active"] is True
        assert entry["operator_action_required"] is True
        assert entry["suggested_review_care_patch"]["live_rollout_mode"] == "shadow"


# ---------------------------------------------------------------------------
# reconcile_publish_state — core scenarios
# ---------------------------------------------------------------------------

class TestReconcilePublishStateEmptyPrior:
    """All current candidates are new when prior state is empty."""

    def test_empty_prior_all_new(self):
        prior = dict(PRIOR_PUBLISH_STATE_BASE)
        candidates = [
            make_candidate("rf-001"),
            make_candidate("rf-002"),
        ]
        result = reconcile_publish_state(candidates, prior)

        assert result.new_findings == ["rf-001", "rf-002"]
        assert result.already_published == []
        assert result.absent_findings == []
        assert result.superseded_findings == []
        assert result.pending_findings == []

    def test_empty_findings_dict_all_new(self):
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={})
        candidates = [make_candidate("rf-001")]
        result = reconcile_publish_state(candidates, prior)
        assert result.new_findings == ["rf-001"]


class TestReconcilePublishStatePublishedFindings:
    """Findings with matching fingerprint that were published stay published."""

    def test_same_fingerprint_stays_published(self):
        fp = "abc123" + "0" * 56
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry("published", fingerprint=fp),
        })
        candidates = [make_candidate("rf-001", fingerprint=fp)]
        result = reconcile_publish_state(candidates, prior)

        assert result.already_published == ["rf-001"]
        assert result.new_findings == []
        assert result.superseded_findings == []

    def test_different_fingerprint_is_superseded(self):
        prior_fp = "aaa111" + "0" * 56
        current_fp = "bbb222" + "0" * 56
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry("published", fingerprint=prior_fp),
        })
        candidates = [make_candidate("rf-001", fingerprint=current_fp)]
        result = reconcile_publish_state(candidates, prior)

        assert result.superseded_findings == ["rf-001"]
        assert result.already_published == []
        assert result.new_findings == []

    def test_prior_pending_is_pending(self):
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry("pending"),
        })
        candidates = [make_candidate("rf-001")]
        result = reconcile_publish_state(candidates, prior)

        assert result.pending_findings == ["rf-001"]
        assert result.new_findings == []
        assert result.already_published == []

    def test_prior_failed_is_pending(self):
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry("failed", error="rate limit"),
        })
        candidates = [make_candidate("rf-001")]
        result = reconcile_publish_state(candidates, prior)

        assert result.pending_findings == ["rf-001"]

    def test_prior_skipped_is_pending(self):
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry("skipped"),
        })
        candidates = [make_candidate("rf-001")]
        result = reconcile_publish_state(candidates, prior)

        assert result.pending_findings == ["rf-001"]

    def test_prior_superseded_is_pending(self):
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry("superseded"),
        })
        candidates = [make_candidate("rf-001")]
        result = reconcile_publish_state(candidates, prior)

        assert result.pending_findings == ["rf-001"]


class TestReconcilePublishStateAbsent:
    """Findings present in prior but absent from current candidates."""

    def test_published_absent_from_current_is_absent(self):
        fp = "abc123" + "0" * 56
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry("published", fingerprint=fp),
        })
        # No candidates match rf-001
        candidates = [make_candidate("rf-002")]
        result = reconcile_publish_state(candidates, prior)

        assert result.absent_findings == ["rf-001"]
        assert result.all_prior_findings == ["rf-001"]

    def test_pending_absent_from_current_is_absent(self):
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry("pending"),
        })
        candidates = [make_candidate("rf-002")]
        result = reconcile_publish_state(candidates, prior)

        # Prior pending that is absent — treated as absent
        assert result.absent_findings == ["rf-001"]

    def test_multiple_absent_findings(self):
        fp = "abc123" + "0" * 56
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry(PublishStatus.PUBLISHED, fingerprint=fp),
            "rf-002": make_prior_entry(PublishStatus.PUBLISHED, fingerprint=fp),
            "rf-003": make_prior_entry(PublishStatus.PENDING),
        })
        # Only rf-002 appears in current candidates — must use SAME fingerprint
        candidates = [make_candidate("rf-002", fingerprint=fp)]
        result = reconcile_publish_state(candidates, prior)

        assert result.absent_findings == ["rf-001", "rf-003"]
        assert result.already_published == ["rf-002"]


class TestReconcilePublishStateMixed:
    """Mix of new, published, superseded, and absent findings."""

    def test_full_mixed_scenario(self):
        fp_a = "aaa111" + "0" * 56
        fp_b = "bbb222" + "0" * 56
        fp_c = "ccc333" + "0" * 56
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry(PublishStatus.PUBLISHED, fingerprint=fp_a),   # same fp → published
            "rf-002": make_prior_entry(PublishStatus.PUBLISHED, fingerprint=fp_b),  # diff fp → superseded
            "rf-003": make_prior_entry(PublishStatus.PENDING),                     # pending; absent from current → absent
            "rf-004": make_prior_entry(PublishStatus.PUBLISHED, fingerprint=fp_c),  # absent from current → absent
        })
        candidates = [
            make_candidate("rf-001", fingerprint=fp_a),        # re-confirmed published
            make_candidate("rf-002", fingerprint="new-fp-bbb222" + "0" * 51),  # superseded (diff fp)
            make_candidate("rf-new-1"),                        # brand new
            make_candidate("rf-new-2"),                        # brand new
        ]
        result = reconcile_publish_state(candidates, prior)

        assert result.new_findings == ["rf-new-1", "rf-new-2"]
        assert result.already_published == ["rf-001"]
        assert result.superseded_findings == ["rf-002"]
        # rf-003: prior=pending, absent from current → absent (not pending, since it didn't appear)
        assert result.pending_findings == []
        assert result.absent_findings == ["rf-003", "rf-004"]
        assert set(result.all_prior_findings) == {"rf-001", "rf-002", "rf-003", "rf-004"}


class TestReconcilePublishStateIdempotency:
    """Running reconciliation twice with the same data is stable."""

    def test_rerun_with_same_candidates_is_idempotent(self):
        fp = "abc123" + "0" * 56
        prior_v1 = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry(PublishStatus.PENDING),
        })
        candidates = [
            make_candidate("rf-001", fingerprint=fp),
            make_candidate("rf-002"),
        ]

        # First reconciliation: rf-001=pending (still present), rf-002=new
        r1 = reconcile_publish_state(candidates, prior_v1)
        assert r1.new_findings == ["rf-002"]
        assert r1.pending_findings == ["rf-001"]

        # Second reconciliation against updated prior (rf-001 now published)
        prior_v2 = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry(PublishStatus.PUBLISHED, fingerprint=fp),
            "rf-002": make_prior_entry(PublishStatus.PENDING),
        })
        r2 = reconcile_publish_state(candidates, prior_v2)
        # rf-001: was published, same fp → already_published
        # rf-002: was pending (not new in v1), still pending → pending_findings
        assert r2.already_published == ["rf-001"]
        assert r2.pending_findings == ["rf-002"]
        assert r2.new_findings == []
        assert r2.absent_findings == []

    def test_adding_new_findings_on_rerun(self):
        fp = "abc123" + "0" * 56
        prior_v1 = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry("published", fingerprint=fp),
        })
        candidates_v1 = [make_candidate("rf-001", fingerprint=fp)]
        r1 = reconcile_publish_state(candidates_v1, prior_v1)
        assert r1.already_published == ["rf-001"]
        assert r1.new_findings == []

        # Add a new finding in v2
        candidates_v2 = [
            make_candidate("rf-001", fingerprint=fp),
            make_candidate("rf-002"),
        ]
        prior_v2 = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry("published", fingerprint=fp),
        })
        r2 = reconcile_publish_state(candidates_v2, prior_v2)
        assert r2.already_published == ["rf-001"]
        assert r2.new_findings == ["rf-002"]


class TestReconcilePublishStateEdgeCases:
    """Edge cases and boundary conditions."""

    def test_candidates_with_missing_fingerprint(self):
        """Candidates missing fingerprint key use empty string as fallback."""
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={})
        candidates = [{"finding_id": "rf-001"}]  # no fingerprint
        result = reconcile_publish_state(candidates, prior)
        assert result.new_findings == ["rf-001"]

    def test_prior_entry_missing_status_key(self):
        """Prior entries without explicit status default to pending."""
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": {"run_id": "run-x"},  # no 'status' key
        })
        candidates = [make_candidate("rf-001")]
        result = reconcile_publish_state(candidates, prior)
        assert result.pending_findings == ["rf-001"]

    def test_empty_candidates_with_prior_state(self):
        """All prior findings become absent when no current candidates."""
        fp = "abc123" + "0" * 56
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-001": make_prior_entry("published", fingerprint=fp),
            "rf-002": make_prior_entry("pending"),
        })
        result = reconcile_publish_state([], prior)
        assert set(result.absent_findings) == {"rf-001", "rf-002"}
        assert result.already_published == []
        assert result.new_findings == []

    def test_prior_finding_ids_are_sorted(self):
        fp = "abc123" + "0" * 56
        prior = dict(PRIOR_PUBLISH_STATE_BASE, findings={
            "rf-c": make_prior_entry("published", fingerprint=fp),
            "rf-a": make_prior_entry("published", fingerprint=fp),
            "rf-b": make_prior_entry("published", fingerprint=fp),
        })
        candidates = []
        result = reconcile_publish_state(candidates, prior)
        assert result.absent_findings == ["rf-a", "rf-b", "rf-c"]


# ---------------------------------------------------------------------------
# build_review_summary_comment — structural + determinism tests
# ---------------------------------------------------------------------------

def _minimal_reconciles() -> ReconciliationResult:
    """Shared minimal reconciliation for comment tests."""
    return ReconciliationResult(
        new_findings=["rf-001", "rf-002"],
        already_published=["rf-003"],
        absent_findings=["rf-004"],
        superseded_findings=["rf-005"],
        pending_findings=["rf-006"],
        all_prior_findings=["rf-003", "rf-004", "rf-005", "rf-006"],
    )


class TestBuildReviewSummaryComment:
    def test_produces_non_empty_string(self):
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=_minimal_reconciles(),
            run_status="completed",
        )
        assert isinstance(comment, str)
        assert len(comment) > 0

    def test_contains_header(self):
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=_minimal_reconciles(),
        )
        assert "QA-Agent Autonomous Review Summary" in comment

    def test_contains_repo_and_run_id(self):
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-42",
            reconciliation=_minimal_reconciles(),
        )
        assert "my-repo" in comment
        assert "run-42" in comment

    def test_contains_run_status(self):
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=_minimal_reconciles(),
            run_status="failed",
        )
        assert "failed" in comment

    def test_includes_error_when_present(self):
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=_minimal_reconciles(),
            run_status="failed",
            run_error="rate limit exceeded",
        )
        assert "rate limit" in comment

    def test_shows_new_findings(self):
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=_minimal_reconciles(),
        )
        assert "rf-001" in comment
        assert "rf-002" in comment

    def test_shows_already_published_findings(self):
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=_minimal_reconciles(),
        )
        assert "rf-003" in comment
        assert "Already Published" in comment

    def test_shows_absent_findings_when_included(self):
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=_minimal_reconciles(),
            include_absent=True,
        )
        assert "rf-004" in comment
        assert "Absent" in comment or "Resolved" in comment

    def test_excludes_absent_when_flag_false(self):
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=_minimal_reconciles(),
            include_absent=False,
        )
        # Absent findings should not appear in the body
        assert "rf-004" not in comment

    def test_shows_superseded_findings(self):
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=_minimal_reconciles(),
            include_superseded=True,
        )
        assert "rf-005" in comment

    def test_excludes_superseded_when_flag_false(self):
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=_minimal_reconciles(),
            include_superseded=False,
        )
        assert "rf-005" not in comment

    def test_exhausted_empty_reconciliation(self):
        empty = ReconciliationResult()
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=empty,
        )
        assert "my-repo" in comment
        assert "run-001" in comment
        assert "0 total" in comment or "0" in comment

    def test_deterministic_same_inputs_same_output(self):
        r = _minimal_reconciles()
        c1 = build_review_summary_comment("repo", "run-1", r, "completed")
        c2 = build_review_summary_comment("repo", "run-1", r, "completed")
        assert c1 == c2

    def test_max_finding_lines_truncates(self):
        r = ReconciliationResult(
            new_findings=[f"rf-{i:03d}" for i in range(20)],
            already_published=[],
            absent_findings=[],
            superseded_findings=[],
            pending_findings=[],
            all_prior_findings=[],
        )
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=r,
            max_finding_lines=3,
        )
        assert "rf-000" in comment
        assert "+17 more" in comment or "…" in comment

    def test_output_guards_against_runaway_lines(self):
        # Create a reconciliation with many findings to push past COMMENT_MAX_LINES
        many_new = [f"rf-new-{i}" for i in range(100)]
        many_absent = [f"rf-abs-{i}" for i in range(100)]
        r = ReconciliationResult(
            new_findings=many_new,
            already_published=[],
            absent_findings=many_absent,
            superseded_findings=[],
            pending_findings=[],
            all_prior_findings=[],
        )
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=r,
            max_finding_lines=100,
        )
        lines = comment.splitlines()
        assert len(lines) <= 65  # COMMENT_MAX_LINES + buffer

    def test_footer_present(self):
        comment = build_review_summary_comment(
            repo="my-repo",
            run_id="run-001",
            reconciliation=_minimal_reconciles(),
        )
        assert "Generated by QA-Agent" in comment or "QA-Agent" in comment


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
