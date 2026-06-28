"""Derive a rule family from a linter rule name for envelope tagging."""

import re

# Letter-coded rule suffixes: single letter + digits (ruff's b904, c901, f401, etc.)
_LETTER_CODED = re.compile(r"^[a-zA-Z]\d+$")


def derive_rule_family(rule: str) -> str:
    """Derive a rule family from a linter rule name.

    ruff-b904 -> "ruff-b"      (letter-coded: linter prefix + code letter)
    ruff-c901 -> "ruff-c"
    ruff-f401 -> "ruff-f"
    eslint-no-unused-vars -> "eslint-no-unused"  (descriptive: all but last segment)
    no-undef -> "no"           (bare: first segment)

    Rules without a hyphen return the full rule.
    """
    segments = rule.split("-")
    if len(segments) <= 1:
        return rule
    # Letter-coded suffix (e.g. b904, c901): family is linter prefix + code letter.
    # This keeps ruff's B/E/F/S/C4 groups distinct for per-family SPRT calibration.
    if _LETTER_CODED.match(segments[-1]):
        return segments[0] + "-" + segments[-1][0].lower()
    # Descriptive suffix: family is all but the last segment.
    return "-".join(segments[:-1])
