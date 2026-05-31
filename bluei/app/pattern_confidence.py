"""Confidence management for learned fix patterns based on feedback and replay outcomes.

Adjusts pattern confidence upward on positive feedback and downward on
negative feedback or replay failures.  Patterns that fall below the
deactivation threshold are zeroed out.  Stale patterns (not verified within
STALENESS_DAYS) are also deactivated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bluei.engine.pattern_store import FixPatternStore
    from bluei.app.models import FeedbackSentiment

CONFIDENCE_BOOST_POSITIVE = 0.1
CONFIDENCE_DECAY_NEGATIVE = -0.2
CONFIDENCE_DECAY_REPLAY_FAIL = -0.15
DEACTIVATION_THRESHOLD = 0.3
AUTO_REPLAY_THRESHOLD = 0.9
STALENESS_DAYS = 60


def adjust_from_feedback(
    finding_id: str,
    sentiment: FeedbackSentiment,
    store: FixPatternStore,
) -> Optional[float]:
    """Adjust a pattern's confidence based on user feedback sentiment.

    Positive feedback boosts confidence; negative feedback decays it.  If the
    pattern is stale or falls below the deactivation threshold it is zeroed
    out in the store.  Neutral sentiment returns the current confidence
    without modification.

    Args:
        finding_id: Finding that triggered the feedback.
        sentiment: POSITIVE, NEGATIVE, or NEUTRAL.
        store: Pattern store to look up and update.

    Returns:
        New confidence value, or None if no matching pattern was found.
    """
    from bluei.app.models import FeedbackSentiment

    if sentiment == FeedbackSentiment.POSITIVE:
        delta = CONFIDENCE_BOOST_POSITIVE
    elif sentiment == FeedbackSentiment.NEGATIVE:
        delta = CONFIDENCE_DECAY_NEGATIVE
    else:
        pattern = store.lookup_by_finding(finding_id)
        if pattern is None:
            return None
        return pattern.confidence

    pattern = store.lookup_by_finding(finding_id)
    if pattern is None:
        return None

    if _check_staleness(pattern.pattern_id, pattern.last_verified_at, store):
        store.update_confidence(pattern.pattern_id, -pattern.confidence)
        return 0.0

    new_confidence = min(1.0, max(0.0, pattern.confidence + delta))

    if new_confidence < DEACTIVATION_THRESHOLD:
        store.update_confidence(pattern.pattern_id, -pattern.confidence)
    else:
        store.update_confidence(pattern.pattern_id, delta)

    return new_confidence


def adjust_from_replay_failure(
    pattern_id: str,
    store: FixPatternStore,
) -> Optional[float]:
    """Decay a pattern's confidence after a replay attempt fails.

    Args:
        pattern_id: ID of the pattern that failed replay.
        store: Pattern store to look up and update.

    Returns:
        New confidence value, or None if the pattern was not found among
        active patterns.
    """
    for pattern in store.load_active():
        if pattern.pattern_id == pattern_id:
            new_confidence = min(1.0, max(0.0, pattern.confidence + CONFIDENCE_DECAY_REPLAY_FAIL))

            if new_confidence < DEACTIVATION_THRESHOLD:
                store.update_confidence(pattern_id, -pattern.confidence)
            else:
                store.update_confidence(pattern_id, CONFIDENCE_DECAY_REPLAY_FAIL)

            return new_confidence
    return None


def _check_staleness(
    pattern_id: str,
    last_verified_at: Optional[str],
    store: FixPatternStore,
) -> bool:
    """Return True if the pattern has not been verified within STALENESS_DAYS."""
    if last_verified_at is None:
        return True
    try:
        verified_dt = datetime.fromisoformat(last_verified_at)
        if verified_dt.tzinfo is None:
            verified_dt = verified_dt.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(days=STALENESS_DAYS)
        return verified_dt < cutoff
    except (ValueError, TypeError):
        return True
