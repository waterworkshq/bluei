"""Review policy helpers — classify feedback, decide loop permission."""

from typing import List, Tuple


def classify_review_feedback(feedback: str) -> str:
    """Classify review feedback as either 'needs-human' or 'actionable'."""
    text = feedback.lower()
    conceptual_markers = [
        "architecture",
        "product decision",
        "design change",
        "conceptual",
        "rethink",
    ]
    for marker in conceptual_markers:
        if marker in text:
            return "needs-human"
    return "actionable"


def review_loop_allowed(
    pr_author: str,
    pr_tags: List[str],
    bot_author: str,
    explicit_tag: str,
) -> Tuple[bool, str]:
    """Decide whether a review loop is permitted for a PR."""
    author_ok = pr_author == bot_author
    tag_ok = explicit_tag and explicit_tag in pr_tags
    if author_ok or tag_ok:
        return True, "review-loop-policy-pass"
    return False, "review-loop-policy-block: non-bot PR without explicit tag"
