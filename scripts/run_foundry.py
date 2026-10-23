#!/usr/bin/env python3
"""Run the Recipe Foundry against seeded Patterns to produce canonical Recipes.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/run_foundry.py

Reads seeded Patterns from bluei/engine/seeded_patterns/, authors Recipe YAML
via the configured LLM command, validates via detector oracle, and writes to
bluei/engine/recipes/staged/. Output is then curated and promoted to
built-in/ as the canonical seeded-Recipe set.

ADR-0020 authoring gate (D3-05): the LLM authoring command is the workspace
config key ``foundry.claude_author_cmd_template`` — the top-level ``foundry``
mapping of the global config.yaml (``load_global_config()``). ADR-0020 names
the key but does not pin its config path; the top-level ``foundry`` section
is used and no default is assumed. Absent or empty template -> the Foundry is
DISABLED (message to stderr, exit 0), never crashed, and never silently run
against a hardcoded command.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bluei.engine.pattern_store import FixPattern  # noqa: E402
from bluei.engine.bundle_loader import load_bundles  # noqa: E402
from bluei.engine.jsonl import read_jsonl  # noqa: E402
from bluei.tools.foundry.pipeline import run_foundry  # noqa: E402

FOUNDRY_CONFIG_SECTION = "foundry"
FOUNDRY_CONFIG_KEY = "claude_author_cmd_template"
PROMPT_PLACEHOLDER = "{prompt}"
LLM_TIMEOUT_SECONDS = 120

DISABLED_MESSAGE = (
    f"foundry: no {FOUNDRY_CONFIG_KEY} configured — Foundry disabled (ADR-0020)"
)


def resolve_author_cmd_template(config: Mapping[str, Any]) -> Optional[str]:
    """Return the configured ``foundry.claude_author_cmd_template``, or None.

    ADR-0020's gate: the template's EXISTENCE enables the Foundry. Absent,
    empty, blank, or non-string values all resolve to None (disabled) — no
    hardcoded fallback, because a default command would defeat the gate.
    """
    section = config.get(FOUNDRY_CONFIG_SECTION) if isinstance(config, Mapping) else None
    template = (
        section.get(FOUNDRY_CONFIG_KEY) if isinstance(section, Mapping) else None
    )
    if isinstance(template, str) and template.strip():
        return template
    return None


def build_author_command(template: str, prompt: str) -> List[str]:
    """Build the LLM authoring argv from the template and the prompt.

    Contract (ADR-0020 names the key but not the substitution; this is the
    documented choice): the template is a shell-style command line. If it
    contains a standalone ``{prompt}`` token, that token is replaced by the
    WHOLE prompt as a single argv element (robust for multi-line prompts —
    never re-split). Otherwise the prompt is appended as the last positional
    argument, e.g. template ``claude -p`` -> ``["claude", "-p", <prompt>]``
    (the claude CLI accepts the prompt as its sole positional argument).
    """
    try:
        argv = shlex.split(template)
    except ValueError:
        # Unbalanced quotes in the template: fall back to whitespace split
        # rather than crash (the gate's contract is disabled-not-crashed;
        # a malformed template still yields a runnable-looking argv).
        argv = template.split()
    if PROMPT_PLACEHOLDER in argv:
        return [prompt if tok == PROMPT_PLACEHOLDER else tok for tok in argv]
    return argv + [prompt]


def make_claude_callback(template: str) -> Callable[[str], str]:
    """Build the llm_callback that shells out via the configured template."""

    def callback(prompt: str) -> str:
        try:
            result = subprocess.run(
                build_author_command(template, prompt),
                capture_output=True,
                text=True,
                timeout=LLM_TIMEOUT_SECONDS,
            )
            return result.stdout
        except Exception as exc:
            print(f"  claude callback error: {exc}", file=sys.stderr)
            return ""

    return callback


def load_seeded_patterns() -> list[FixPattern]:
    patterns: list[FixPattern] = []
    seeded_dir = REPO_ROOT / "bluei" / "engine" / "seeded_patterns"
    for jsonl_path in sorted(seeded_dir.glob("*.jsonl")):
        for record in read_jsonl(jsonl_path):
            if isinstance(record, dict):
                patterns.append(FixPattern.from_dict(record))
    return patterns


def main(config: Optional[Dict[str, Any]] = None) -> int:
    # ADR-0020 / D3-05 gate FIRST, before any work: no configured authoring
    # template -> Foundry disabled, exit 0 (not crashed, not hardcoded).
    if config is None:
        from bluei.app.config import load_global_config

        config = load_global_config()

    template = resolve_author_cmd_template(config)
    if template is None:
        print(DISABLED_MESSAGE, file=sys.stderr)
        return 0

    patterns = load_seeded_patterns()
    bundles = load_bundles()
    staged_dir = REPO_ROOT / "bluei" / "engine" / "recipes" / "staged"

    print(f"Loaded {len(patterns)} seeded Patterns, {len(bundles)} bundles")
    print(f"Output dir: {staged_dir}")
    print()

    callback = make_claude_callback(template)
    summary = run_foundry(
        patterns=patterns,
        bundles=bundles,
        llm_callback=callback,
        staged_dir=staged_dir,
        config={},
        approval_records_path=None,
    )

    print()
    print("=== Foundry Summary ===")
    print(f"  Proposed:     {summary.proposed}")
    print(f"  Rejected:     {summary.rejected}")
    print(f"  Written:      {summary.written}")
    print(f"  Auto-promoted:{summary.auto_promoted}")
    if summary.rejected_topics:
        print(f"  Rejected topics:")
        for topic in summary.rejected_topics:
            print(f"    - {topic}")

    return 0 if summary.written > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
