"""LLM synthesizer — generates canonical rule candidates from topics.

Takes a Topic (rule code, description, language), constructs a prompt for
an LLM, and parses the response into a ParsedRule candidate. The LLM
callback is injectable — tests mock it, the real pipeline uses Claude or
equivalent.

ADR-0018: the LLM synthesizes ORIGINAL content. The linter validates it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Optional

from bluei.tools.seed.models import ParsedRule


@dataclass
class Topic:
    """A category to synthesize a canonical rule for."""

    rule_id: str  # e.g. "ruff-b904"
    language: str  # "python", "typescript", "javascript"
    description: str  # Human-readable description of what the rule checks
    linter_code: str  # The rule code for validation: "B904"
    linter: str  # "ruff" | "eslint"


def synthesize_rule(
    topic: Topic,
    llm_callback: Callable[[str], str],
) -> Optional[ParsedRule]:
    """Generate a ParsedRule candidate for a topic using an LLM.

    Args:
        topic: The rule category to generate an example for.
        llm_callback: A function that takes a prompt string and returns the
            LLM's text response.

    Returns:
        ParsedRule candidate, or None if synthesis failed.
    """
    prompt = _build_prompt(topic)
    response = llm_callback(prompt)
    return _parse_response(response, topic)


def _build_prompt(topic: Topic) -> str:
    """Construct the LLM prompt for synthesizing a canonical rule example."""
    lang_name = {
        "python": "Python",
        "typescript": "TypeScript",
        "javascript": "JavaScript",
    }.get(topic.language, topic.language)
    return f"""Generate a minimal, focused {lang_name} code example that triggers linting rule {topic.linter_code}.

Rule description: {topic.description}

Requirements:
- The "before" example must be 3-10 lines of REAL code that clearly triggers this rule. No placeholder comments.
- The "after" example must be the corrected version — minimal change to fix the violation.
- The "negative" example must be code that looks SIMILAR to the violation but does NOT trigger this rule.
- Keep examples as small as possible. One violation per example.

Respond with ONLY a JSON object (no markdown fences, no explanation):
{{"before": "the violation code", "after": "the corrected code", "negative": "a similar but non-triggering example", "description": "one-line summary"}}
"""


def _parse_response(response: str, topic: Topic) -> Optional[ParsedRule]:
    """Parse the LLM's JSON response into a ParsedRule candidate."""
    # Strip markdown fences if present
    response = response.strip()
    if response.startswith("```"):
        response = re.sub(r"^```(?:json)?\s*\n?", "", response)
        response = re.sub(r"\n?```\s*$", "", response)

    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        # Try to extract JSON from surrounding text
        match = re.search(r"\{[^{}]*\}", response, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return None

    before = data.get("before", "").strip()
    after = data.get("after", "").strip()
    negative = data.get("negative", "").strip()
    description = data.get("description", topic.description)

    if not before or not after:
        return None

    return ParsedRule(
        rule=topic.rule_id,
        language=topic.language,
        before=before,
        after=after,
        detector_before="",  # populated by validator
        detector_after="",
        negative_examples=[negative] if negative else [],
        has_autofix=False,  # auto-detected by validator
        source_linter=topic.linter,
        validation_command=f"{topic.linter} check --select {topic.linter_code}"
        if topic.linter == "ruff"
        else f"eslint --rule {topic.linter_code}",
        description=description,
    )
