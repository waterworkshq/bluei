"""Canonical tuple format that linter parsers emit and package.py consumes."""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ParsedRule:
    """A single linter rule parsed from a fixture/test, ready for packaging.

    Parsers (ruff_parser, eslint_parser) produce these; package.py converts
    them into Golden Bundles + seeded Patterns + seeded Recipes.
    """

    rule: str  # e.g. "ruff-b904", "no-unused-vars"
    language: str  # "python", "typescript", "javascript"
    before: str  # violation code (the trigger case)
    after: Optional[str]  # fixed code; None if no autofix
    detector_before: str  # linter diagnostic on the violation
    detector_after: str  # linter diagnostic after fix ("" = clean)
    negative_examples: List[str] = field(default_factory=list)  # non-triggering code
    has_autofix: bool = False  # whether the linter can auto-fix
    source_linter: str = ""  # "ruff" | "eslint" | ...
    validation_command: str = ""  # e.g. "ruff check --select B904"
    description: str = ""  # human-readable rule summary (for Recipe metadata)
