import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from bluei.engine.pattern_store import (
    INITIAL_CONFIDENCE,
    FixPattern,
    FixPatternStore,
)
from bluei.app.models import FeedbackSentiment
from bluei.app.pattern_confidence import adjust_from_feedback
from bluei.review.feedback import record_feedback
from bluei.app.state import StateManager

# Phase D update: record_feedback no longer imports adjust_from_feedback
# directly; callers must wire it via the confidence_adjuster callback.
# These tests verify the wiring explicitly by passing the legacy adjuster.
_ADJUSTER = adjust_from_feedback


def _make_pattern(**overrides):
    defaults = {
        "pattern_id": "fp-testconf001",
        "rule": "broad-except",
        "language": "python",
        "file_path": "src/*.py",
        "before_snippet": "except:\n    pass",
        "after_snippet": "except Exception:\n    pass",
        "diff_patch": "--- a\n+++ b\n-except:\n+except Exception:\n",
        "confidence": 0.5,
        "success_count": 1,
        "failure_count": 0,
        "skip_count": 0,
        "source": "claude",
        "created_at": "2026-05-17T00:00:00+00:00",
        "last_used_at": None,
        "last_verified_at": "2026-05-17T00:00:00+00:00",
        "source_finding_ids": ["finding-abc123"],
    }
    defaults.update(overrides)
    return FixPattern(**defaults)


@pytest.fixture
def state_mgr(tmp_path):
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()
    state = StateManager(repos_dir)
    repo_state_dir = state._get_state_dir("test-repo")
    repo_state_dir.mkdir(parents=True, exist_ok=True)
    return state


def _seed_pattern(state_mgr, repo_name="test-repo", **overrides):
    patterns_file = state_mgr.get_fix_patterns_file(repo_name)
    store = FixPatternStore(patterns_file)
    pid = store.append(_make_pattern(**overrides))
    # Confidence is now preserved by _prepare_new_pattern (no longer reset
    # to INITIAL_CONFIDENCE), so no update_confidence adjustment is needed.
    return pid, patterns_file


def _load_pattern_by_finding(patterns_file, finding_id):
    store = FixPatternStore(patterns_file)
    return store.lookup_by_finding(finding_id)


class TestConfidenceWiringPositiveSentiment:
    def test_positive_feedback_triggers_confidence_boost(self, state_mgr):
        fid = "finding-abc123"
        pid, pf = _seed_pattern(state_mgr, source_finding_ids=[fid], confidence=0.5)

        record_feedback(
            state=state_mgr,
            repo_name="test-repo",
            feedback_input={"comment": "Looks great!"},
            input_class="comment",
            finding_id=fid,
            confidence_adjuster=_ADJUSTER,
        )

        pattern = _load_pattern_by_finding(pf, fid)
        assert pattern is not None
        assert pattern.confidence == pytest.approx(0.6)


class TestConfidenceWiringNegativeSentiment:
    def test_negative_feedback_triggers_confidence_decay(self, state_mgr):
        fid = "finding-abc123"
        pid, pf = _seed_pattern(state_mgr, source_finding_ids=[fid], confidence=0.5)

        record_feedback(
            state=state_mgr,
            repo_name="test-repo",
            feedback_input={"comment": "This is wrong, please fix"},
            input_class="comment",
            finding_id=fid,
            confidence_adjuster=_ADJUSTER,
        )

        pattern = _load_pattern_by_finding(pf, fid)
        assert pattern is not None
        assert pattern.confidence == pytest.approx(0.3)


class TestConfidenceWiringConceptualSentiment:
    def test_conceptual_feedback_no_confidence_change(self, state_mgr):
        fid = "finding-abc123"
        pid, pf = _seed_pattern(state_mgr, source_finding_ids=[fid], confidence=0.5)

        record_feedback(
            state=state_mgr,
            repo_name="test-repo",
            feedback_input={"comment": "I have a suggestion"},
            input_class="comment",
            finding_id=fid,
            confidence_adjuster=_ADJUSTER,
        )

        pattern = _load_pattern_by_finding(pf, fid)
        assert pattern is not None
        assert pattern.confidence == pytest.approx(0.5)


class TestConfidenceWiringContradictorySentiment:
    def test_contradictory_feedback_no_confidence_change(self, state_mgr):
        fid = "finding-abc123"
        pid, pf = _seed_pattern(state_mgr, source_finding_ids=[fid], confidence=0.5)

        record_feedback(
            state=state_mgr,
            repo_name="test-repo",
            feedback_input={"comment": "This looks good but also wrong"},
            input_class="comment",
            finding_id=fid,
            confidence_adjuster=_ADJUSTER,
        )

        pattern = _load_pattern_by_finding(pf, fid)
        assert pattern is not None
        assert pattern.confidence == pytest.approx(0.5)


class TestConfidenceWiringExceptionSafety:
    def test_adjust_from_feedback_exception_does_not_break_recording(self, state_mgr):
        fid = "finding-abc123"
        _seed_pattern(state_mgr, source_finding_ids=[fid], confidence=0.5)

        # Phase D update: callback is now passed explicitly; the legacy
        # patch target bluei.review.feedback.adjust_from_feedback no longer
        # exists, so we pass a raising stub directly.
        def _raising_adjuster(*args, **kwargs):
            raise RuntimeError("boom")

        result = record_feedback(
            state=state_mgr,
            repo_name="test-repo",
            feedback_input={"comment": "Looks great!"},
            input_class="comment",
            finding_id=fid,
            confidence_adjuster=_raising_adjuster,
        )

        events = state_mgr.load_feedback_events("test-repo")
        assert len(events) == 1
        assert events[0]["finding_id"] == fid

    def test_no_fix_patterns_file_skips_confidence_gracefully(self, state_mgr):
        fid = "finding-abc123"
        result = record_feedback(
            state=state_mgr,
            repo_name="test-repo",
            feedback_input={"comment": "Looks great!"},
            input_class="comment",
            finding_id=fid,
            confidence_adjuster=_ADJUSTER,
        )

        events = state_mgr.load_feedback_events("test-repo")
        assert len(events) == 1
        assert events[0]["finding_id"] == fid


class TestConfidenceWiringIntegration:
    def test_full_positive_feedback_cycle(self, state_mgr):
        fid = "finding-cycle-pos"
        pid, pf = _seed_pattern(state_mgr, source_finding_ids=[fid], confidence=0.4)

        record_feedback(
            state=state_mgr,
            repo_name="test-repo",
            feedback_input={"comment": "LGTM, approved"},
            input_class="comment",
            finding_id=fid,
            confidence_adjuster=_ADJUSTER,
        )

        pattern = _load_pattern_by_finding(pf, fid)
        assert pattern is not None
        assert pattern.confidence == pytest.approx(0.5)

        events = state_mgr.load_feedback_events("test-repo")
        assert len(events) == 1
        assert events[0]["sentiment"] == "positive"

    def test_full_negative_feedback_cycle(self, state_mgr):
        fid = "finding-cycle-neg"
        pid, pf = _seed_pattern(state_mgr, source_finding_ids=[fid], confidence=0.5)

        record_feedback(
            state=state_mgr,
            repo_name="test-repo",
            feedback_input={"comment": "Not correct, must fix"},
            input_class="comment",
            finding_id=fid,
            confidence_adjuster=_ADJUSTER,
        )

        pattern = _load_pattern_by_finding(pf, fid)
        assert pattern is not None
        assert pattern.confidence == pytest.approx(0.3)

        events = state_mgr.load_feedback_events("test-repo")
        assert len(events) == 1
        assert events[0]["sentiment"] == "negative"
