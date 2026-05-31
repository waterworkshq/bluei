import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from bluei.engine.pattern_store import (
    DEACTIVATION_THRESHOLD as STORE_DEACTIVATION_THRESHOLD,
)
from bluei.engine.pattern_store import (
    INITIAL_CONFIDENCE,
    FixPattern,
    FixPatternStore,
)
from bluei.app.models import FeedbackSentiment
from bluei.app.pattern_confidence import (
    AUTO_REPLAY_THRESHOLD,
    CONFIDENCE_BOOST_POSITIVE,
    CONFIDENCE_DECAY_NEGATIVE,
    CONFIDENCE_DECAY_REPLAY_FAIL,
    DEACTIVATION_THRESHOLD,
    STALENESS_DAYS,
    _check_staleness,
    adjust_from_feedback,
    adjust_from_replay_failure,
)


def make_pattern(**overrides):
    defaults = {
        'pattern_id': 'fp-test123456',
        'rule': 'broad-except',
        'language': 'python',
        'file_path': 'src/*.py',
        'before_snippet': 'except:\n    pass',
        'after_snippet': 'except Exception:\n    pass',
        'diff_patch': '--- a\n+++ b\n-except:\n+except Exception:\n',
        'confidence': 0.5,
        'success_count': 1,
        'failure_count': 0,
        'skip_count': 0,
        'source': 'claude',
        'created_at': '2026-05-17T00:00:00+00:00',
        'last_used_at': None,
        'last_verified_at': datetime.now(timezone.utc).isoformat(),
        'source_finding_ids': ['finding-abc123'],
    }
    defaults.update(overrides)
    return FixPattern(**defaults)


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / 'state' / 'fix_patterns.jsonl'


def _setup_pattern_at_confidence(store_path, confidence, **pattern_overrides):
    store = FixPatternStore(store_path)
    pid = store.append(make_pattern(**pattern_overrides))
    if confidence != INITIAL_CONFIDENCE:
        store.update_confidence(pid, confidence - INITIAL_CONFIDENCE)
    return store


def test_positive_feedback_boosts_confidence(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.5, source_finding_ids=['finding-abc123'])

    result = adjust_from_feedback('finding-abc123', FeedbackSentiment.POSITIVE, store)

    expected = 0.5 + CONFIDENCE_BOOST_POSITIVE
    assert result == pytest.approx(expected)


def test_negative_feedback_decays_confidence(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.5, source_finding_ids=['finding-abc123'])

    result = adjust_from_feedback('finding-abc123', FeedbackSentiment.NEGATIVE, store)

    expected = 0.5 + CONFIDENCE_DECAY_NEGATIVE
    assert result == pytest.approx(expected)


def test_conceptual_feedback_no_change(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.5, source_finding_ids=['finding-abc123'])

    result = adjust_from_feedback('finding-abc123', FeedbackSentiment.CONCEPTUAL, store)

    assert result == pytest.approx(0.5)


def test_contradictory_feedback_no_change(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.5, source_finding_ids=['finding-abc123'])

    result = adjust_from_feedback('finding-abc123', FeedbackSentiment.CONTRADICTORY, store)

    assert result == pytest.approx(0.5)


def test_mixed_feedback_no_change(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.5, source_finding_ids=['finding-abc123'])

    result = adjust_from_feedback('finding-abc123', FeedbackSentiment.MIXED, store)

    assert result == pytest.approx(0.5)


def test_confidence_clamped_at_max_1(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.95, source_finding_ids=['finding-abc123'])

    result = adjust_from_feedback('finding-abc123', FeedbackSentiment.POSITIVE, store)

    assert result == pytest.approx(1.0)


def test_confidence_clamped_at_min_0(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.15, source_finding_ids=['finding-low'])

    result = adjust_from_feedback('finding-low', FeedbackSentiment.NEGATIVE, store)

    assert result == pytest.approx(0.0)


def test_confidence_below_threshold_deactivates(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.35, source_finding_ids=['finding-deact'])

    result = adjust_from_feedback('finding-deact', FeedbackSentiment.NEGATIVE, store)

    assert result is not None
    assert result < DEACTIVATION_THRESHOLD
    pattern = store.lookup_by_finding('finding-deact')
    assert pattern is not None
    assert pattern.confidence < DEACTIVATION_THRESHOLD


def test_no_pattern_for_finding_returns_none(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.5, source_finding_ids=['finding-abc123'])

    result = adjust_from_feedback('finding-nonexistent', FeedbackSentiment.POSITIVE, store)

    assert result is None


def test_multiple_positive_events_reach_auto_replay_threshold(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.5, source_finding_ids=['finding-multi'])

    confidence = 0.5
    for _ in range(5):
        result = adjust_from_feedback('finding-multi', FeedbackSentiment.POSITIVE, store)
        confidence = result

    assert confidence >= AUTO_REPLAY_THRESHOLD


def test_staleness_detected_for_old_last_verified(store_path):
    old_date = (datetime.now(timezone.utc) - timedelta(days=STALENESS_DAYS + 10)).isoformat()
    store = _setup_pattern_at_confidence(
        store_path, 0.6,
        source_finding_ids=['finding-stale'],
        last_verified_at=old_date,
    )

    is_stale = _check_staleness('fp-test123456', old_date, store)
    assert is_stale is True


def test_no_staleness_for_recent_last_verified(store_path):
    recent_date = datetime.now(timezone.utc).isoformat()
    store = _setup_pattern_at_confidence(
        store_path, 0.6,
        source_finding_ids=['finding-recent'],
        last_verified_at=recent_date,
    )

    is_stale = _check_staleness('fp-test123456', recent_date, store)
    assert is_stale is False


def test_staleness_with_none_last_verified(store_path):
    store = _setup_pattern_at_confidence(
        store_path, 0.6,
        source_finding_ids=['finding-none'],
        last_verified_at=None,
    )

    is_stale = _check_staleness('fp-test123456', None, store)
    assert is_stale is True


def test_adjust_from_feedback_calls_update_confidence_with_correct_delta(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.5, source_finding_ids=['finding-abc123'])

    adjust_from_feedback('finding-abc123', FeedbackSentiment.POSITIVE, store)

    records = store._load_records_unlocked()
    updated = None
    for r in records:
        if 'finding-abc123' in str(r.get('source_finding_ids', [])):
            updated = r
            break
    assert updated is not None
    assert updated['confidence'] == pytest.approx(0.5 + CONFIDENCE_BOOST_POSITIVE)


def test_replay_failure_decay(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.6, source_finding_ids=['finding-replay'])

    pid = next(iter(store._patterns.keys()))
    result = adjust_from_replay_failure(pid, store)

    expected = 0.6 + CONFIDENCE_DECAY_REPLAY_FAIL
    assert result == pytest.approx(expected)


def test_replay_failure_deactivates_below_threshold(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.35, source_finding_ids=['finding-replay-deact'])

    pid = next(iter(store._patterns.keys()))
    result = adjust_from_replay_failure(pid, store)

    assert result < DEACTIVATION_THRESHOLD


def test_replay_failure_nonexistent_pattern_returns_none(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.5, source_finding_ids=['finding-abc123'])

    result = adjust_from_replay_failure('fp-nonexistent', store)

    assert result is None


def test_staleness_checked_during_adjust_from_feedback(store_path):
    old_date = (datetime.now(timezone.utc) - timedelta(days=STALENESS_DAYS + 5)).isoformat()
    store = _setup_pattern_at_confidence(
        store_path, 0.7,
        source_finding_ids=['finding-stale-feedback'],
        last_verified_at=old_date,
    )

    with patch('bluei.app.pattern_confidence._check_staleness', return_value=True) as mock_stale:
        result = adjust_from_feedback('finding-stale-feedback', FeedbackSentiment.POSITIVE, store)
        mock_stale.assert_called_once()
        assert result == 0.0


def test_staleness_deactivates_pattern_in_store(store_path):
    """When a stale pattern receives feedback, it should be deactivated (confidence zeroed)."""
    old_date = (datetime.now(timezone.utc) - timedelta(days=STALENESS_DAYS + 10)).isoformat()
    store = _setup_pattern_at_confidence(
        store_path, 0.7,
        source_finding_ids=['finding-stale-deact'],
        last_verified_at=old_date,
    )

    result = adjust_from_feedback('finding-stale-deact', FeedbackSentiment.POSITIVE, store)

    assert result == 0.0
    pattern = store.lookup_by_finding('finding-stale-deact')
    assert pattern is not None
    assert pattern.confidence == pytest.approx(0.0)


def test_staleness_deactivates_even_on_positive_feedback(store_path):
    """Positive feedback on a stale pattern should still deactivate it."""
    old_date = (datetime.now(timezone.utc) - timedelta(days=STALENESS_DAYS + 1)).isoformat()
    store = _setup_pattern_at_confidence(
        store_path, 0.9,
        source_finding_ids=['finding-stale-positive'],
        last_verified_at=old_date,
    )

    result = adjust_from_feedback('finding-stale-positive', FeedbackSentiment.POSITIVE, store)

    assert result == 0.0
    pattern = store.lookup_by_finding('finding-stale-positive')
    assert pattern.confidence == pytest.approx(0.0)


def test_no_staleness_allows_normal_feedback_adjustment(store_path):
    """Recent patterns should still get normal feedback adjustments."""
    recent_date = datetime.now(timezone.utc).isoformat()
    store = _setup_pattern_at_confidence(
        store_path, 0.5,
        source_finding_ids=['finding-recent-feedback'],
        last_verified_at=recent_date,
    )

    result = adjust_from_feedback('finding-recent-feedback', FeedbackSentiment.POSITIVE, store)

    assert result == pytest.approx(0.5 + CONFIDENCE_BOOST_POSITIVE)
    pattern = store.lookup_by_finding('finding-recent-feedback')
    assert pattern.confidence == pytest.approx(0.5 + CONFIDENCE_BOOST_POSITIVE)


def test_staleness_with_invalid_date_returns_true(store_path):
    store = _setup_pattern_at_confidence(store_path, 0.6, source_finding_ids=['finding-x'])

    is_stale = _check_staleness('fp-test', 'not-a-date', store)
    assert is_stale is True


def test_deactivation_zeros_out_store_confidence_single_update(store_path):
    """Deactivation should use a single update that zeroes out the confidence,
    not a double update that can produce wrong values."""
    store = _setup_pattern_at_confidence(store_path, 0.35, source_finding_ids=['finding-deact'])

    adjust_from_feedback('finding-deact', FeedbackSentiment.NEGATIVE, store)

    pattern = store.lookup_by_finding('finding-deact')
    assert pattern is not None
    assert pattern.confidence == pytest.approx(0.0)


def test_no_deactivation_applies_delta_directly_to_store(store_path):
    """When confidence stays above threshold, the store should reflect delta exactly."""
    store = _setup_pattern_at_confidence(store_path, 0.5, source_finding_ids=['finding-ok'])

    adjust_from_feedback('finding-ok', FeedbackSentiment.NEGATIVE, store)

    pattern = store.lookup_by_finding('finding-ok')
    assert pattern is not None
    assert pattern.confidence == pytest.approx(0.5 + CONFIDENCE_DECAY_NEGATIVE)


def test_replay_failure_deactivation_zeros_out_store_confidence(store_path):
    """Replay failure deactivation should also use a single zeroing update."""
    store = _setup_pattern_at_confidence(store_path, 0.35, source_finding_ids=['finding-replay-deact'])

    pid = next(iter(store._patterns.keys()))
    adjust_from_replay_failure(pid, store)

    pattern = store.get_pattern(pid)
    assert pattern is not None
    assert pattern.confidence == pytest.approx(0.0)


def test_replay_failure_no_deactivation_applies_delta_to_store(store_path):
    """Replay failure that doesn't cross threshold should apply delta exactly."""
    store = _setup_pattern_at_confidence(store_path, 0.6, source_finding_ids=['finding-replay-ok'])

    pid = next(iter(store._patterns.keys()))
    adjust_from_replay_failure(pid, store)

    pattern = store.get_pattern(pid)
    assert pattern is not None
    assert pattern.confidence == pytest.approx(0.6 + CONFIDENCE_DECAY_REPLAY_FAIL)
