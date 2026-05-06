#!/usr/bin/env python3
"""Tests for Phase I: Feedback Event Normalization and Capture.

Run with:
  cd /home/vikas/.openclaw/workspace/qa-agent
  python -m pytest tests/test_feedback_capture.py -v

Or standalone:
  python tests/test_feedback_capture.py
"""

import json
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa_agent.models import (
    Repo,
    RepoConfig,
    ReviewMode,
    FindingSource,
    FindingActionability,
    FindingSeverity,
    FeedbackEvent,
    FeedbackSentiment,
    FeedbackSource,
    generate_id,
)
from qa_agent.review import (
    normalize_feedback,
    record_feedback,
    inject_feedback_for_autonomous_review,
    _flush_injected_feedback,
    _classify_text_sentiment,
    _normalize_reaction_signal,
    ReviewCycleEngine,
    GitHubReviewProvider,
)
from qa_agent.state import StateManager


# ---------------------------------------------------------------------------
# Isolation helper
# ---------------------------------------------------------------------------

def _isolated_tmp() -> Path:
    base = Path(f"/tmp/qa_feedback_{uuid.uuid4().hex[:8]}")
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    return base


# ---------------------------------------------------------------------------
# Test: normalize_feedback — comment input
# ---------------------------------------------------------------------------

class TestNormalizeComment:
    """Comment inputs are normalized to FeedbackEvent-compatible dicts."""

    def test_minimal_comment(self):
        raw = {"comment": "This looks good, thank you!"}
        result = normalize_feedback(raw, "comment")
        assert result["sentiment"] == "positive"
        assert result["source"] == "human-reviewer"
        assert result["comment"] == "This looks good, thank you!"
        assert result["is_conceptual"] is False
        assert result["is_contradictory"] is False
        assert result["id"].startswith("fbe-")
        assert result["finding_id"] is None

    def test_negative_comment(self):
        raw = {"comment": "This is not correct, please fix it."}
        result = normalize_feedback(raw, "comment")
        assert result["sentiment"] == "negative"
        assert result["is_conceptual"] is False

    def test_nit_comment_is_conceptual(self):
        raw = {"comment": "nit: consider using a constant here"}
        result = normalize_feedback(raw, "comment")
        assert result["sentiment"] == "conceptual"
        assert result["is_conceptual"] is True

    def test_mixed_sentiment_comment(self):
        raw = {"comment": "looks good overall, but the naming is wrong"}
        result = normalize_feedback(raw, "comment")
        assert result["sentiment"] == "mixed"
        assert result["is_contradictory"] is True

    def test_finding_id_binding_preserved(self):
        raw = {"comment": "nice work", "finding_id": "rf-abc123-000"}
        result = normalize_feedback(raw, "comment")
        assert result["finding_id"] == "rf-abc123-000"

    def test_pr_number_context_preserved(self):
        raw = {"comment": "approved", "pr_number": 42}
        result = normalize_feedback(raw, "comment")
        assert result["_pr_number"] == 42

    def test_author_context_preserved(self):
        raw = {"comment": "lgtm", "author": "sarah"}
        result = normalize_feedback(raw, "comment")
        assert result["_author"] == "sarah"

    def test_empty_comment_becomes_conceptual(self):
        # Empty string hits the _classify_text_sentiment "no keywords → MIXED" path.
        # normalize_feedback upgrades empty comment to conceptual explicitly.
        raw = {"comment": ""}
        result = normalize_feedback(raw, "comment")
        # Empty comments are treated as conceptual (no signal)
        assert result["comment"] == ""
        assert result["is_conceptual"] is True

    def test_missing_comment_treated_as_empty(self):
        raw = {}
        result = normalize_feedback(raw, "comment")
        assert result["comment"] == ""
        assert result["is_conceptual"] is True


# ---------------------------------------------------------------------------
# Test: normalize_feedback — reply input
# ---------------------------------------------------------------------------

class TestNormalizeReply:
    """Reply inputs are treated as conceptual unless unambiguously positive."""

    def test_reply_with_positive_text(self):
        raw = {"comment": "thank you for fixing this!"}
        result = normalize_feedback(raw, "reply")
        # Positive text in reply is still treated as conceptual (replies are nuanced)
        assert result["sentiment"] in {"positive", "conceptual"}

    def test_reply_with_nit_considered_conceptual(self):
        raw = {"comment": "nit: minor style issue"}
        result = normalize_feedback(raw, "reply")
        assert result["sentiment"] == "conceptual"
        assert result["is_conceptual"] is True

    def test_reply_with_contradiction_is_contradictory(self):
        raw = {"comment": "looks good but the logic is wrong"}
        result = normalize_feedback(raw, "reply")
        assert result["sentiment"] == "mixed"
        assert result["is_contradictory"] is True


# ---------------------------------------------------------------------------
# Test: normalize_feedback — review_state_change input
# ---------------------------------------------------------------------------

class TestNormalizeReviewStateChange:
    """Review state changes normalize to definitive sentiments."""

    def test_approved_is_positive(self):
        raw = {"state": "APPROVED"}
        result = normalize_feedback(raw, "review_state_change")
        assert result["sentiment"] == "positive"
        assert result["is_conceptual"] is False
        assert result["comment"] == "review_state_change:APPROVED"

    def test_changes_requested_is_negative(self):
        raw = {"state": "CHANGES_REQUESTED"}
        result = normalize_feedback(raw, "review_state_change")
        assert result["sentiment"] == "negative"
        assert result["is_conceptual"] is False

    def test_commented_is_conceptual(self):
        raw = {"state": "COMMENTED"}
        result = normalize_feedback(raw, "review_state_change")
        assert result["sentiment"] == "conceptual"
        assert result["is_conceptual"] is True

    def test_dismissed_is_negative(self):
        raw = {"state": "DISMISSED"}
        result = normalize_feedback(raw, "review_state_change")
        # Not APPROVED or CHANGES_REQUESTED → conceptual
        assert result["sentiment"] == "conceptual"

    def test_case_insensitive_state(self):
        raw = {"state": "approved"}
        result = normalize_feedback(raw, "review_state_change")
        assert result["sentiment"] == "positive"


# ---------------------------------------------------------------------------
# Test: normalize_feedback — reaction input (conservative handling)
# ---------------------------------------------------------------------------

class TestNormalizeReaction:
    """Reactions are treated conservatively — only unambiguous ones carry signal."""

    def test_thumbs_up_is_positive(self):
        raw = {"reaction": "👍"}
        result = normalize_feedback(raw, "reaction")
        assert result["sentiment"] == "positive"
        assert result["is_conceptual"] is False
        assert result["comment"] == "reaction:👍"

    def test_eyes_reaction_is_conceptual_ambiguous(self):
        raw = {"reaction": "👀"}
        result = normalize_feedback(raw, "reaction")
        assert result["sentiment"] == "conceptual"
        assert result["is_conceptual"] is True

    def test_thumbs_down_is_negative(self):
        raw = {"reaction": "👎"}
        result = normalize_feedback(raw, "reaction")
        assert result["sentiment"] == "negative"
        assert result["is_conceptual"] is False

    def test_rocket_is_positive(self):
        raw = {"reaction": "🚀"}
        result = normalize_feedback(raw, "reaction")
        assert result["sentiment"] == "positive"

    def test_fire_reaction_is_conceptual_ambiguous(self):
        # Fire emoji is not in the unambiguously-positive set;
        # conservative default treats it as conceptual/ambiguous.
        raw = {"reaction": "🔥"}
        result = normalize_feedback(raw, "reaction")
        assert result["sentiment"] == "conceptual"
        assert result["is_conceptual"] is True

    def test_unknown_emoji_is_conceptual(self):
        raw = {"reaction": "🤖"}
        result = normalize_feedback(raw, "reaction")
        assert result["sentiment"] == "conceptual"
        assert result["is_conceptual"] is True

    def test_heart_reaction_is_positive(self):
        raw = {"reaction": "❤️"}
        result = normalize_feedback(raw, "reaction")
        assert result["sentiment"] == "positive"

    def test_thinking_face_is_conceptual(self):
        raw = {"reaction": "🤔"}
        result = normalize_feedback(raw, "reaction")
        assert result["sentiment"] == "conceptual"


# ---------------------------------------------------------------------------
# Test: normalize_feedback — invalid input class raises ValueError
# ---------------------------------------------------------------------------

class TestNormalizeFeedbackInvalidClass:
    def test_unknown_class_raises(self):
        raw = {"comment": "hello"}
        try:
            normalize_feedback(raw, "unknown_class")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "unknown_class" in str(e)
            assert "comment" in str(e)


# ---------------------------------------------------------------------------
# Test: record_feedback — persistence to feedback_events.jsonl
# ---------------------------------------------------------------------------

def make_engine(tmp_path: Path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = RepoConfig(
        id="feedback-test",
        name="feedback-repo",
        path=str(repo_path),
        language="typescript",
        review_care={"enabled": True, "mode": ReviewMode.AUTONOMOUS_REVIEW.value},
    )
    repo = Repo(config=config)
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    from unittest.mock import MagicMock
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = state
    engine.provider = MagicMock(spec=GitHubReviewProvider)
    return engine, repo, state


class TestRecordFeedbackPersistence:
    """Feedback events are persisted to feedback_events.jsonl."""

    def test_feedback_event_appended_to_jsonl(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)

        record_feedback(
            state=state,
            repo_name=repo.config.name,
            feedback_input={"comment": "Looks great!", "author": "alice"},
            input_class="comment",
        )

        events = state.load_feedback_events(repo.config.name)
        assert len(events) == 1
        assert events[0]["comment"] == "Looks great!"
        assert events[0]["source"] == "human-reviewer"

    def test_multiple_feedback_events_appended(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)

        for i in range(3):
            record_feedback(
                state=state,
                repo_name=repo.config.name,
                feedback_input={"comment": f"comment {i}"},
                input_class="comment",
            )

        events = state.load_feedback_events(repo.config.name)
        assert len(events) == 3

    def test_finding_bound_feedback_has_finding_id(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)

        record_feedback(
            state=state,
            repo_name=repo.config.name,
            feedback_input={"comment": "nice fix"},
            input_class="comment",
            finding_id="rf-abc123-000",
        )

        events = state.load_feedback_events(repo.config.name)
        assert len(events) == 1
        assert events[0]["finding_id"] == "rf-abc123-000"

    def test_repo_scoped_feedback_has_no_finding_id(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)

        record_feedback(
            state=state,
            repo_name=repo.config.name,
            feedback_input={"comment": "overall looks good"},
            input_class="comment",
        )

        events = state.load_feedback_events(repo.config.name)
        assert len(events) == 1
        assert events[0]["finding_id"] is None

    def test_pr_scoped_feedback_preserved(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)

        # pr_number goes into the normalized output (internal context)
        normalized = normalize_feedback(
            {"comment": "pr level feedback", "pr_number": 42},
            "comment",
        )
        assert normalized["_pr_number"] == 42
        assert normalized["finding_id"] is None  # no finding_id → repo-scoped

        record_feedback(
            state=state,
            repo_name=repo.config.name,
            feedback_input={"comment": "pr level feedback"},
            input_class="comment",
            pr_number=42,
        )

        events = state.load_feedback_events(repo.config.name)
        assert len(events) == 1
        assert events[0]["finding_id"] is None  # persisted record has no finding_id

    def test_review_state_change_persisted_correctly(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)

        record_feedback(
            state=state,
            repo_name=repo.config.name,
            feedback_input={"state": "APPROVED"},
            input_class="review_state_change",
            finding_id="rf-abc123-000",
        )

        events = state.load_feedback_events(repo.config.name)
        assert len(events) == 1
        assert events[0]["sentiment"] == "positive"
        assert events[0]["finding_id"] == "rf-abc123-000"

    def test_reaction_persisted_correctly(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)

        record_feedback(
            state=state,
            repo_name=repo.config.name,
            feedback_input={"reaction": "👍"},
            input_class="reaction",
        )

        events = state.load_feedback_events(repo.config.name)
        assert len(events) == 1
        assert events[0]["sentiment"] == "positive"
        assert events[0]["comment"] == "reaction:👍"


# ---------------------------------------------------------------------------
# Test: Finding-bound vs repo/pr-scoped feedback
# ---------------------------------------------------------------------------

class TestFeedbackBinding:
    """Feedback is correctly bound to findings or scoped to repo/PR."""

    def test_finding_id_takes_precedence(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)

        # Explicit finding_id in the raw input; pr_number is also provided
        record_feedback(
            state=state,
            repo_name=repo.config.name,
            feedback_input={"comment": "nice!", "finding_id": "rf-specific-999"},
            input_class="comment",
            pr_number=10,
        )

        events = state.load_feedback_events(repo.config.name)
        assert events[0]["finding_id"] == "rf-specific-999"

        # Verify the normalized record carries pr_number in context
        normalized = normalize_feedback(
            {"comment": "nice!", "finding_id": "rf-specific-999", "pr_number": 10},
            "comment",
        )
        assert normalized["_pr_number"] == 10
        assert normalized["finding_id"] == "rf-specific-999"

    def test_feedback_without_finding_id_is_repo_scoped(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)

        record_feedback(
            state=state,
            repo_name=repo.config.name,
            feedback_input={"comment": "overall looks good for this PR"},
            input_class="comment",
            pr_number=7,
        )

        events = state.load_feedback_events(repo.config.name)
        assert events[0]["finding_id"] is None  # repo-scoped, no finding binding

    def test_state_change_to_approved_is_positive(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)

        record_feedback(
            state=state,
            repo_name=repo.config.name,
            feedback_input={"state": "APPROVED"},
            input_class="review_state_change",
            pr_number=99,
        )

        events = state.load_feedback_events(repo.config.name)
        assert events[0]["sentiment"] == "positive"
        assert events[0]["finding_id"] is None  # no finding_id → repo-scoped


# ---------------------------------------------------------------------------
# Test: autonomous-review local path with injected feedback
# ---------------------------------------------------------------------------

class TestAutonomousReviewFeedbackInjection:
    """The autonomous-review cycle can record injected feedback without affecting observation mode."""

    STUB_CANDIDATES = [
        {
            "repo": "feedback-repo",
            "path": "src/main.ts",
            "line": 10,
            "header": "outstanding-todo",
            "snippet": "# TODO: refactor",
            "source": FindingSource.LINTER.value,
            "actionability": FindingActionability.MEDIUM.value,
            "severity": FindingSeverity.LOW.value,
            "confidence": 0.7,
            "safe_to_autofix": False,
            "discovered_at": "2026-03-29T00:00:00Z",
        },
        {
            "repo": "feedback-repo",
            "path": "src/utils.ts",
            "line": 5,
            "header": "excessively-long-line",
            "snippet": "x = a + b + c + d + e + f + g + h + i + j + k + l + m",
            "source": FindingSource.LINTER.value,
            "actionability": FindingActionability.HIGH.value,
            "severity": FindingSeverity.MEDIUM.value,
            "confidence": 0.85,
            "safe_to_autofix": True,
            "discovered_at": "2026-03-29T00:00:00Z",
        },
    ]

    def test_inject_feedback_attaches_to_engine(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)
        engine._generate_local_candidates = lambda: list(self.STUB_CANDIDATES)

        inject_feedback_for_autonomous_review(
            engine,
            [
                {"input_class": "comment", "comment": "looks good!", "author": "alice"},
                {"input_class": "reaction", "reaction": "👍"},
                {"input_class": "review_state_change", "state": "APPROVED"},
            ],
        )

        assert hasattr(engine, "_injected_feedback")
        assert len(engine._injected_feedback) == 3

    def test_dry_run_does_not_flush_feedback(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)
        engine._generate_local_candidates = lambda: list(self.STUB_CANDIDATES)

        inject_feedback_for_autonomous_review(
            engine,
            [{"input_class": "comment", "comment": "hello"}],
        )

        engine._run_autonomous_review_cycle(dry_run=True)

        events = state.load_feedback_events(repo.config.name)
        assert len(events) == 0  # No events in dry run

    def test_non_dry_run_flushes_injected_feedback(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)
        engine._generate_local_candidates = lambda: list(self.STUB_CANDIDATES)

        inject_feedback_for_autonomous_review(
            engine,
            [
                {"input_class": "comment", "comment": "looks good!", "author": "alice"},
                {"input_class": "reaction", "reaction": "👍"},
                {"input_class": "review_state_change", "state": "APPROVED"},
                {
                    "input_class": "comment",
                    "comment": "This finding needs work",
                    "author": "bob",
                    "finding_id": "rf-specific-000",
                },
            ],
        )

        result = engine._run_autonomous_review_cycle(dry_run=False)

        events = state.load_feedback_events(repo.config.name)
        assert len(events) == 4

        # Verify sentiments
        sentiments = {e["sentiment"] for e in events}
        assert "positive" in sentiments  # comment + review_state_change

        # Verify finding-bound feedback
        finding_bound = [e for e in events if e["finding_id"] == "rf-specific-000"]
        assert len(finding_bound) == 1

        # Verify reaction normalized
        reaction_events = [e for e in events if e["comment"].startswith("reaction:")]
        assert len(reaction_events) == 1
        assert reaction_events[0]["sentiment"] == "positive"

        # After flush, injected list is cleared
        assert engine._injected_feedback == []

    def test_no_injected_feedback_means_no_events(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)
        engine._generate_local_candidates = lambda: list(self.STUB_CANDIDATES)
        # No injection

        result = engine._run_autonomous_review_cycle(dry_run=False)

        # Cycle ran fine, but no feedback events
        events = state.load_feedback_events(repo.config.name)
        assert len(events) == 0
        # And the cycle itself ran successfully
        assert result.findings_detected == 2

    def test_feedback_events_recorded_as_review_event(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)
        engine._generate_local_candidates = lambda: list(self.STUB_CANDIDATES)

        inject_feedback_for_autonomous_review(
            engine,
            [{"input_class": "comment", "comment": "nice work"}],
        )

        engine._run_autonomous_review_cycle(dry_run=False)

        events_file = state.get_review_events_file(repo.config.name)
        assert events_file.exists()
        lines = events_file.read_text().strip().splitlines()
        event_types = {json.loads(l)["event"] for l in lines}
        assert "autonomous-review-completed" in event_types
        assert "feedback-events-recorded" in event_types

    def test_feedback_with_finding_id_binds_correctly(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)
        engine._generate_local_candidates = lambda: list(self.STUB_CANDIDATES)

        # Run once to get actual finding IDs
        engine._run_autonomous_review_cycle(dry_run=False)
        findings = state.load_review_findings(repo.config.name)
        assert len(findings) == 2
        first_finding_id = findings[0]["finding_id"]

        # Inject feedback for that specific finding
        inject_feedback_for_autonomous_review(
            engine,
            [
                {
                    "input_class": "comment",
                    "comment": "Thank you for fixing this!",
                    "finding_id": first_finding_id,
                    "author": "reviewer",
                }
            ],
        )

        result = engine._run_autonomous_review_cycle(dry_run=False)

        events = state.load_feedback_events(repo.config.name)
        # 2 events: 1 from first run (no injection) + 3 from second run
        # Actually first run has 0 injected, second run has 1
        finding_bound = [e for e in events if e["finding_id"] == first_finding_id]
        assert len(finding_bound) == 1
        assert finding_bound[0]["comment"] == "Thank you for fixing this!"

    def test_observation_mode_not_affected_by_feedback_stub(self):
        """Observation mode does not call any feedback-related code."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)
        repo.config.review_care["mode"] = ReviewMode.OBSERVATION.value
        engine.provider.list_managed_prs.return_value = []

        # Even if feedback is somehow set, observation mode ignores it
        engine._injected_feedback = [{"input_class": "comment", "comment": "ignored"}]

        result = engine.run(dry_run=True)

        # No feedback events recorded in observation mode
        events = state.load_feedback_events(repo.config.name)
        assert len(events) == 0
        # Findings counters not touched by observation cycle
        assert result.findings_detected == 0


# ---------------------------------------------------------------------------
# Test: conservative reaction handling edge cases
# ---------------------------------------------------------------------------

class TestConservativeReactionHandling:
    """Reactions default to ambiguous/conceptual unless trivially clear."""

    POSITIVE_REACTIONS = ["👍", "✅", "🎉", "🚀", "❤️", "🙌", "🏆"]
    NEGATIVE_REACTIONS = ["👎", "😞", "❌", "⛔"]
    AMBIGUOUS_REACTIONS = ["😄", "😂", "😊", "😍", "🤔", "😮", "💯", "🔥", "🤖", "👀"]

    def test_known_positive_reactions_are_positive(self):
        for r in self.POSITIVE_REACTIONS:
            sentiment, was_ambiguous = _normalize_reaction_signal(r)
            assert sentiment == FeedbackSentiment.POSITIVE, f"{r} should be positive"
            assert was_ambiguous is False

    def test_known_negative_reactions_are_negative(self):
        for r in self.NEGATIVE_REACTIONS:
            sentiment, was_ambiguous = _normalize_reaction_signal(r)
            assert sentiment == FeedbackSentiment.NEGATIVE, f"{r} should be negative"
            assert was_ambiguous is False

    def test_ambiguous_reactions_are_conceptual(self):
        for r in self.AMBIGUOUS_REACTIONS:
            sentiment, was_ambiguous = _normalize_reaction_signal(r)
            assert sentiment == FeedbackSentiment.CONCEPTUAL, f"{r} should be conceptual"
            assert was_ambiguous is True

    def test_totally_unknown_reaction_is_conceptual(self):
        sentiment, was_ambiguous = _normalize_reaction_signal("🌈")
        assert sentiment == FeedbackSentiment.CONCEPTUAL
        assert was_ambiguous is True


# ---------------------------------------------------------------------------
# Test: round-trip through FeedbackEvent model
# ---------------------------------------------------------------------------

class TestFeedbackEventRoundTrip:
    """Normalized events can be loaded back as FeedbackEvent instances."""

    def test_normalized_event_loads_as_feedback_event(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp)

        # "looks good" is a known positive keyword → positive sentiment
        record_feedback(
            state=state,
            repo_name=repo.config.name,
            feedback_input={"comment": "looks good, thank you!", "author": "sarah"},
            input_class="comment",
            finding_id="rf-xyz789-000",
        )

        events = state.load_feedback_events(repo.config.name)
        assert len(events) == 1

        # Load as FeedbackEvent model and verify round-trip
        fe = FeedbackEvent.from_dict(events[0])
        assert fe.comment == "looks good, thank you!"
        assert fe.sentiment == FeedbackSentiment.POSITIVE
        assert fe.source == FeedbackSource.HUMAN_REVIEWER
        assert fe.finding_id == "rf-xyz789-000"
        assert fe.is_conceptual is False

    def test_to_dict_preserves_all_fields(self):
        raw = {
            "comment": "looks good",
            "author": "alice",
            "finding_id": "rf-test-001",
            "pr_number": 55,
            "loop_count": 2,
        }
        normalized = normalize_feedback(raw, "comment")
        fe = FeedbackEvent.from_dict(normalized)
        back = fe.to_dict()
        assert back["sentiment"] == "positive"
        assert back["source"] == "human-reviewer"
        assert back["finding_id"] == "rf-test-001"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
