#!/usr/bin/env python3
"""Feedback normalization and capture — extracted from review.cycle for single-responsibility."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from bluei.app.models import (
    FeedbackEvent,
    FeedbackSentiment,
    FeedbackSource,
    generate_id,
    now_iso,
)
from bluei.app.pattern_confidence import adjust_from_feedback

if TYPE_CHECKING:
    from bluei.app.state import StateManager
    from bluei.review.cycle import ReviewCycleEngine

_logger = logging.getLogger(__name__)

# Sentiment keywords for text-based sentiment classification
_POSITIVE_KEYWORDS = frozenset(
    {
        "looks good",
        "looks great",
        "lgtm",
        "approved",
        "thank you",
        "nice work",
        "good job",
        "awesome",
        "perfect",
        "excellent",
        "resolved",
        "fixed",
    }
)
_NEGATIVE_KEYWORDS = frozenset(
    {
        "not correct",
        "wrong",
        "broken",
        "bad",
        "please fix",
        "must fix",
        "needs work",
        "incomplete",
        "missing",
        "incorrect",
        "fails",
        "failed",
    }
)
_CHANGE_REQUEST_KEYWORDS = frozenset(
    {
        "change",
        "changes requested",
        "reconsider",
        "suggestion",
        "nit:",
        "nitpick",
    }
)


def _classify_text_sentiment(comment_body: str) -> FeedbackSentiment:
    """
    Classify a comment body into a coarse sentiment category.

    This is a lightweight heuristic for normalization purposes only.
    It is deliberately conservative — it only claims positive or negative
    when the signal is unambiguous; otherwise it returns MIXED or CONCEPTUAL.

    Args:
        comment_body: Raw comment text (will be lowercased internally).

    Returns:
        FeedbackSentiment enum value.
    """
    if not comment_body:
        return FeedbackSentiment.MIXED

    text = comment_body.lower()

    pos_hits = sum(1 for kw in _POSITIVE_KEYWORDS if kw in text)
    neg_hits = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in text)
    change_hits = sum(1 for kw in _CHANGE_REQUEST_KEYWORDS if kw in text)

    if pos_hits > 0 and neg_hits == 0 and change_hits == 0:
        return FeedbackSentiment.POSITIVE
    if neg_hits > 0 and pos_hits == 0:
        return FeedbackSentiment.NEGATIVE
    if change_hits > 0 and pos_hits == 0 and neg_hits == 0:
        # Isolated change-request markers (e.g. "nit:") without clear approval/disapproval
        return FeedbackSentiment.CONCEPTUAL
    if pos_hits > 0 and neg_hits > 0:
        return FeedbackSentiment.MIXED
    return FeedbackSentiment.CONCEPTUAL


# Reaction emoji that carry unambiguous positive signal
_UNAMBIGUOUSLY_POSITIVE_REACTIONS = frozenset({"👍", "✅", "🎉", "🚀", "❤️", "🙌", "🏆"})
# Reaction emoji that carry unambiguous negative signal
_UNAMBIGUOUSLY_NEGATIVE_REACTIONS = frozenset({"👎", "😞", "❌", "⛔"})
# All other reactions are treated as ambiguous — conservative default
_ALL_KNOWN_REACTIONS = (
    _UNAMBIGUOUSLY_POSITIVE_REACTIONS
    | _UNAMBIGUOUSLY_NEGATIVE_REACTIONS
    | frozenset({"😄", "😂", "😊", "😍", "🤔", "😮", "🎉", "🚀", "💯", "🔥"})
)


def _normalize_reaction_signal(reaction: str) -> tuple[FeedbackSentiment, bool]:
    """
    Normalize a reaction emoji into a sentiment, conservatively.

    Only reactions in the unambiguously-positive or unambiguously-negative sets
    produce a definitive sentiment.  All others are treated as CONCEPTUAL
    (ambiguous) to avoid false signals.

    Args:
        reaction: The reaction emoji string.

    Returns:
        A (sentiment, was_ambiguous) tuple.
    """
    if reaction in _UNAMBIGUOUSLY_POSITIVE_REACTIONS:
        return FeedbackSentiment.POSITIVE, False
    if reaction in _UNAMBIGUOUSLY_NEGATIVE_REACTIONS:
        return FeedbackSentiment.NEGATIVE, False
    return FeedbackSentiment.CONCEPTUAL, True  # Conservative default


def normalize_feedback(
    raw: Dict[str, Any],
    input_class: str,
) -> Dict[str, Any]:
    """
    Normalize a raw provider-style feedback input into a FeedbackEvent-compatible dict.

    Supported ``input_class`` values:
    - ``"comment"``: A review comment body.  Classified for sentiment from text.
                     Attached to a finding_id if provided, otherwise repo/PR-scoped.
    - ``"reply"``:    A reply to a review thread or comment.  Treated as conceptual
                      feedback unless the reply body is unambiguously positive/negative.
    - ``"review_state_change"``: A reviewer changing their review state
                      (e.g. APPROVED, CHANGES_REQUESTED, COMMENTED).
                      Positive for APPROVED, negative for CHANGES_REQUESTED,
                      conceptual for COMMENTED.
    - ``"reaction"``: A reaction emoji on a comment or thread.  Ambiguous by default
                      unless the reaction is an unambiguously positive/negative emoji.

    Args:
        raw: A dict with at minimum ``comment`` (str) for comment/reply,
             ``state`` (str) for review_state_change, or ``reaction`` (str) for reaction.
             Optional ``author``, ``created_at``, ``pr_number``, ``finding_id``.
        input_class: One of ``"comment"``, ``"reply"``, ``"review_state_change"``,
                     or ``"reaction"``.

    Returns:
        A dict with FeedbackEvent-compatible fields:
        ``id``, ``finding_id`` (or ``None``), ``sentiment``, ``source``,
        ``comment``, ``loop_count``, ``is_contradictory``, ``is_conceptual``,
        ``recorded_at``.
        All fields are primitives (no enum instances) for JSON serialization.

    Raises:
        ValueError: If ``input_class`` is not recognized.
    """
    if input_class not in {"comment", "reply", "review_state_change", "reaction"}:
        raise ValueError(
            f"Unknown input_class: {input_class!r}. "
            f"Must be one of: comment, reply, review_state_change, reaction"
        )

    finding_id = str(raw["finding_id"]) if raw.get("finding_id") else None
    author = str(raw.get("author", "")) or ""
    pr_number = raw.get("pr_number")
    if pr_number is not None:
        pr_number = int(pr_number)
    recorded_at = raw.get("created_at") or raw.get("recorded_at") or now_iso()

    comment = ""
    sentiment: FeedbackSentiment = FeedbackSentiment.CONCEPTUAL
    is_conceptual = False
    is_contradictory = False
    source = FeedbackSource.HUMAN_REVIEWER

    if input_class == "comment":
        comment = str(raw.get("comment", "")).strip()
        if not comment:
            # Empty comments carry no signal — conservatively mark as conceptual
            sentiment = FeedbackSentiment.CONCEPTUAL
            is_conceptual = True
            is_contradictory = False
        else:
            sentiment = _classify_text_sentiment(comment)
            is_conceptual = sentiment == FeedbackSentiment.CONCEPTUAL
            is_contradictory = sentiment == FeedbackSentiment.MIXED

    elif input_class == "reply":
        comment = str(raw.get("comment", "")).strip()
        sentiment = _classify_text_sentiment(comment)
        # Replies are generally more nuanced; treat MIXED as conceptual
        is_conceptual = sentiment in {
            FeedbackSentiment.MIXED,
            FeedbackSentiment.CONCEPTUAL,
        }
        is_contradictory = sentiment == FeedbackSentiment.MIXED
        # Reply to a reviewer is typically informational; no strong signal
        if sentiment == FeedbackSentiment.POSITIVE and not comment:
            sentiment = FeedbackSentiment.CONCEPTUAL

    elif input_class == "review_state_change":
        state = str(raw.get("state", "")).strip().upper()
        if state == "APPROVED":
            sentiment = FeedbackSentiment.POSITIVE
            is_conceptual = False
        elif state == "CHANGES_REQUESTED":
            sentiment = FeedbackSentiment.NEGATIVE
            is_conceptual = False
        else:
            # COMMENTED or any other state — informational signal
            sentiment = FeedbackSentiment.CONCEPTUAL
            is_conceptual = True
        comment = f"review_state_change:{state}"

    elif input_class == "reaction":
        reaction = str(raw.get("reaction", "")).strip()
        sentiment, was_ambiguous = _normalize_reaction_signal(reaction)
        is_conceptual = was_ambiguous
        is_contradictory = False
        comment = f"reaction:{reaction}"

    event_id = generate_id("fbe")
    return {
        "id": event_id,
        "finding_id": finding_id,
        "sentiment": sentiment.value,
        "source": source.value,
        "comment": comment,
        "loop_count": int(raw.get("loop_count", 0)),
        "is_contradictory": bool(is_contradictory),
        "is_conceptual": bool(is_conceptual),
        "recorded_at": recorded_at,
        # Additional context fields (not FeedbackEvent fields but useful for storage)
        "_pr_number": pr_number,
        "_author": author,
        "_input_class": input_class,
    }


def record_feedback(
    state: "StateManager",
    repo_name: str,
    feedback_input: Dict[str, Any],
    input_class: str,
    *,
    finding_id: Optional[str] = None,
    pr_number: Optional[int] = None,
    source: FeedbackSource = FeedbackSource.HUMAN_REVIEWER,
    loop_count: int = 0,
) -> Dict[str, Any]:
    """
    Normalize and persist a provider-style feedback input as a FeedbackEvent.

    If ``finding_id`` is provided the feedback is bound to that finding.
    Otherwise it is recorded as repo/PR-scoped feedback (no finding binding).

    The event is appended to ``feedback_events.jsonl`` via
    ``state.append_feedback_event``.

    Args:
        state: StateManager instance for the repo.
        repo_name: Repository name.
        feedback_input: Raw feedback dict (shape depends on ``input_class``).
        input_class: Feedback input class (``"comment"``, ``"reply"``,
                      ``"review_state_change"``, ``"reaction"``).
        finding_id: Optional finding ID to bind this feedback to.
        pr_number: Optional PR number for context.
        source: FeedbackSource enum value (default HUMAN_REVIEWER).
        loop_count: Current loop iteration (default 0).

    Returns:
        The normalized event dict that was persisted.
    """
    # Build enriched raw input with binding info
    enriched = dict(feedback_input)
    if finding_id is not None:
        enriched["finding_id"] = finding_id
    if pr_number is not None:
        enriched["pr_number"] = pr_number
    enriched["loop_count"] = loop_count
    enriched["source"] = source.value

    normalized = normalize_feedback(enriched, input_class)

    # Strip internal context fields before persisting as FeedbackEvent
    event_record: Dict[str, Any] = {
        "id": normalized["id"],
        "finding_id": normalized["finding_id"],
        "sentiment": normalized["sentiment"],
        "source": normalized["source"],
        "comment": normalized["comment"],
        "loop_count": normalized["loop_count"],
        "is_contradictory": normalized["is_contradictory"],
        "is_conceptual": normalized["is_conceptual"],
        "recorded_at": normalized["recorded_at"],
    }

    state.append_feedback_event(repo_name, event_record)

    sentiment_enum = normalized.get("sentiment")
    if isinstance(sentiment_enum, str):
        try:
            sentiment_enum = FeedbackSentiment(sentiment_enum)
        except ValueError:
            sentiment_enum = None
    if sentiment_enum in (FeedbackSentiment.POSITIVE, FeedbackSentiment.NEGATIVE):
        try:
            from bluei.engine.pattern_store import FixPatternStore

            patterns_file = state.get_fix_patterns_file(repo_name)
            if patterns_file.exists():
                _store = FixPatternStore(patterns_file)
                new_conf = adjust_from_feedback(
                    normalized["finding_id"],
                    sentiment_enum,
                    _store,
                )
                if new_conf is not None and new_conf < 0.3:
                    _logger.warning(
                        "pattern-deactivated: finding_id=%s confidence=%.3f",
                        normalized["finding_id"],
                        new_conf,
                    )
        except Exception:
            _logger.debug("confidence adjustment skipped", exc_info=True)

    return normalized


def inject_feedback_for_autonomous_review(
    engine: "ReviewCycleEngine",
    feedback_inputs: List[Dict[str, Any]],
) -> None:
    """
    Inject explicit feedback inputs into an autonomous-review cycle engine.

    This is a **test-oriented stub** that attaches feedback data directly
    to the engine for use during autonomous-review execution.  It does NOT
    poll GitHub or any live provider — it purely records what is passed in.

    The ``feedback_inputs`` list contains dicts with:
      - ``input_class``: str — one of comment, reply, review_state_change, reaction
      - ``comment``: str (for comment/reply)
      - ``state``: str (for review_state_change)
      - ``reaction``: str (for reaction)
      - ``finding_id``: Optional[str] — if provided, feedback is bound to that finding
      - ``pr_number``: Optional[int]
      - ``author``: Optional[str]
      - ``loop_count``: Optional[int] (default 0)

    Usage in tests::

        inject_feedback_for_autonomous_review(
            engine,
            [
                {"input_class": "comment", "comment": "Looks good, thank you!", "finding_id": "rf-abc123-000"},
                {"input_class": "reaction", "reaction": "👍"},
                {"input_class": "review_state_change", "state": "APPROVED"},
            ]
        )
        result = engine._run_autonomous_review_cycle(dry_run=False)

    Args:
        engine: A ReviewCycleEngine instance.
        feedback_inputs: List of raw feedback input dicts.
    """
    engine._injected_feedback = list(feedback_inputs)


def _flush_injected_feedback(
    engine: "ReviewCycleEngine",
    repo_name: str,
    state: "StateManager",
) -> int:
    """Persist any feedback inputs injected via inject_feedback_for_autonomous_review.

    Args:
        engine: ReviewCycleEngine with optional _injected_feedback attribute.
        repo_name: Repository name.
        state: StateManager for the repo.

    Returns:
        Count of feedback events recorded.
    """
    feedback_inputs = getattr(engine, "_injected_feedback", None)
    if not feedback_inputs:
        return 0

    recorded = 0
    for raw in feedback_inputs:
        input_class = raw.get("input_class", "comment")
        finding_id = raw.get("finding_id")
        pr_number = raw.get("pr_number")
        loop_count = int(raw.get("loop_count", 0))
        source_str = raw.get("source", "human-reviewer")
        try:
            source = FeedbackSource(source_str)
        except Exception:
            source = FeedbackSource.HUMAN_REVIEWER

        record_feedback(
            state=state,
            repo_name=repo_name,
            feedback_input=raw,
            input_class=input_class,
            finding_id=finding_id,
            pr_number=pr_number,
            source=source,
            loop_count=loop_count,
        )
        recorded += 1

    # Clear injected feedback after flushing
    engine._injected_feedback = []
    return recorded
