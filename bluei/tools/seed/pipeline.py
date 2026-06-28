"""Pipeline driver — orchestrates synthesize → validate → package.

For each topic: the synthesizer generates a candidate, the validator checks
it against the linter, and package_rule converts validated candidates into
product fixtures. Parallelizable across topics.

ADR-0018: synthesize original rules, validate with linter as oracle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from bluei.tools.seed.models import ParsedRule
from bluei.tools.seed.synthesizer import Topic, synthesize_rule
from bluei.tools.seed.validator import validate_candidate, ValidationResult
from bluei.tools.seed.package import package_rule


@dataclass
class PipelineSummary:
    """Summary of a pipeline run."""

    topics_total: int = 0
    synthesized: int = 0
    validated: int = 0
    rejected: int = 0
    packaged_bundles: int = 0
    packaged_patterns: int = 0
    rejected_topics: List[str] = field(default_factory=list)


def run_pipeline(
    topics: List[Topic],
    llm_callback: Callable[[str], str],
    bundles_dir: Optional[Path] = None,
    seeded_patterns_dir: Optional[Path] = None,
    ruff_executable: str = "ruff",
    eslint_executable: str = "eslint",
) -> PipelineSummary:
    """Run synthesize → validate → package for each topic.

    Args:
        topics: List of rule topics to synthesize.
        llm_callback: Function that takes a prompt and returns LLM response.
        bundles_dir: Output directory for Golden Bundles (default: product dir).
        seeded_patterns_dir: Output directory for seeded Patterns (default: product dir).

    Returns:
        PipelineSummary with counts and rejection details.
    """
    summary = PipelineSummary(topics_total=len(topics))

    for topic in topics:
        # 1. Synthesize
        candidate = synthesize_rule(topic, llm_callback)
        if candidate is None:
            summary.rejected += 1
            summary.rejected_topics.append(
                f"{topic.rule_id}: synthesis failed (no output)"
            )
            continue
        summary.synthesized += 1

        # 2. Validate
        result = validate_candidate(candidate, ruff_executable, eslint_executable)
        if not result.valid:
            summary.rejected += 1
            summary.rejected_topics.append(f"{topic.rule_id}: {result.reason}")
            continue
        summary.validated += 1

        # 3. Package
        manifest = package_rule(
            candidate,
            bundles_dir=bundles_dir,
            seeded_patterns_dir=seeded_patterns_dir,
        )
        if manifest.get("bundle"):
            summary.packaged_bundles += 1
        if manifest.get("pattern"):
            summary.packaged_patterns += 1

    return summary


# Topic lists for batch runs

PYTHON_TOPICS: List[Topic] = [
    Topic(
        "ruff-b904",
        "python",
        "Raise in except should use `from err` or `from None`",
        "B904",
        "ruff",
    ),
    Topic(
        "ruff-b006",
        "python",
        "Mutable default argument (list, dict, set)",
        "B006",
        "ruff",
    ),
    Topic("ruff-b007", "python", "Unused loop variable in enumerate", "B007", "ruff"),
    Topic(
        "ruff-b017", "python", "assertRaises(Exception) is too broad", "B017", "ruff"
    ),
    Topic("ruff-f401", "python", "Imported but unused", "F401", "ruff"),
    Topic("ruff-f541", "python", "f-string without any placeholders", "F541", "ruff"),
    Topic(
        "ruff-f841", "python", "Local variable assigned but never used", "F841", "ruff"
    ),
    Topic("ruff-f601", "python", "Dictionary key literal repeated", "F601", "ruff"),
    Topic(
        "ruff-e711",
        "python",
        "Comparison to None should be `is` not `==`",
        "E711",
        "ruff",
    ),
    Topic(
        "ruff-e712",
        "python",
        "Comparison to True/False should be `is` not `==`",
        "E712",
        "ruff",
    ),
    Topic(
        "ruff-e722",
        "python",
        "Bare except — use `except Exception` instead",
        "E722",
        "ruff",
    ),
    Topic(
        "ruff-sim101",
        "python",
        "Use tuple instead of duplicate isinstance calls",
        "SIM101",
        "ruff",
    ),
    Topic(
        "ruff-sim118",
        "python",
        "Use `key in dict` instead of `key in dict.keys()`",
        "SIM118",
        "ruff",
    ),
    Topic("ruff-sim201", "python", "Use `!=` instead of `not ==`", "SIM201", "ruff"),
    Topic("ruff-sim222", "python", "Use `or` instead of `or True`", "SIM222", "ruff"),
    Topic(
        "ruff-up031",
        "python",
        "Use f-string instead of percent formatting",
        "UP031",
        "ruff",
    ),
    Topic(
        "ruff-c400",
        "python",
        "Unnecessary list comprehension (use list())",
        "C400",
        "ruff",
    ),
    Topic(
        "ruff-c416",
        "python",
        "Unnecessary list comprehension (identity)",
        "C416",
        "ruff",
    ),
    Topic(
        "ruff-s105",
        "python",
        "Possible hardcoded password assigned to variable",
        "S105",
        "ruff",
    ),
    Topic(
        "ruff-s324",
        "python",
        "Use of insecure hash function (md5, sha1)",
        "S324",
        "ruff",
    ),
]

JAVASCRIPT_TOPICS: List[Topic] = [
    Topic(
        "eslint-no-var",
        "javascript",
        "Use let or const instead of var",
        "no-var",
        "eslint",
    ),
    Topic("eslint-eqeqeq", "javascript", "Use === instead of ==", "eqeqeq", "eslint"),
    Topic(
        "eslint-no-console",
        "javascript",
        "Unexpected console statement (use logging)",
        "no-console",
        "eslint",
    ),
    Topic(
        "eslint-no-unused-vars",
        "javascript",
        "Variable declared but never used",
        "no-unused-vars",
        "eslint",
    ),
    Topic(
        "eslint-no-unreachable",
        "javascript",
        "Code after return/throw/break is unreachable",
        "no-unreachable",
        "eslint",
    ),
    Topic(
        "eslint-no-debugger",
        "javascript",
        "Debugger statement found",
        "no-debugger",
        "eslint",
    ),
    Topic(
        "eslint-prefer-const",
        "javascript",
        "let variable never reassigned, use const",
        "prefer-const",
        "eslint",
    ),
    Topic(
        "eslint-no-dupe-keys",
        "javascript",
        "Duplicate keys in object literal",
        "no-dupe-keys",
        "eslint",
    ),
    Topic(
        "eslint-no-empty", "javascript", "Empty block statement", "no-empty", "eslint"
    ),
    Topic(
        "eslint-no-constant-condition",
        "javascript",
        "Constant condition in if/while/for",
        "no-constant-condition",
        "eslint",
    ),
]
