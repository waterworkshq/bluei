"""Brand term mappings — internal → bluei brand expression.

Translates internal terminology (``finding``, ``health_score``, ``repo``, etc.)
into user-facing brand language (``speck``, ``vitality``, ``project``).  Also
provides display helpers for scores and safety-mode labels.
"""

TERM_MAP = {
    "finding": "speck",
    "findings": "specks",
    "health_score": "vitality",
    "health score": "vitality",
    "Health Score": "Vitality",
    "safety_mode": "care_level",
    "safety mode": "care level",
    "Safety Mode": "Care Level",
    "baseline": "starting point",
    "snapshot": "check-in",
    "onboard": "register",
    "repo": "project",
    "repository": "project",
}


def t(internal: str) -> str:
    """Translate an internal term to its branded equivalent.

    Args:
        internal: Internal term (e.g. ``"finding"``, ``"health_score"``).

    Returns:
        Branded term if a mapping exists; otherwise the input unchanged.
    """
    return TERM_MAP.get(internal, internal)


def display_health(score: float) -> str:
    """Format a health score as a branded vitality string.

    Args:
        score: Numeric health score (0–100).

    Returns:
        Formatted string like ``"Vitality: 82.3/100"``.
    """
    return f"Vitality: {score:.1f}/100"


def display_mode(mode_value: str) -> str:
    """Convert a branded mode string to its display label.

    Args:
        mode_value: Branded mode identifier (e.g. ``"watch-only"``).

    Returns:
        Human-readable label from SafetyMode.brand_label.
    """
    from bluei.app.models import SafetyMode

    sm = SafetyMode.from_brand(mode_value)
    return sm.brand_label


MODE_CHOICES_OLD = ["observe", "issue-only", "pr", "merge"]
MODE_CHOICES_BRAND = ["watch-only", "note-only", "offer-fixes", "full-care"]
MODE_CHOICES_ALL = MODE_CHOICES_OLD + MODE_CHOICES_BRAND
