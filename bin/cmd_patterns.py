"""Patterns command handler.

Extracted from bin/bluei.py to reduce its surface area."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from bin.help_text import BOLD, GREEN, HELP_TEXT, RED, RESET
from bin.cmd_utils import parse_option, parse_repo_arg

# Backward-compat aliases
_parse_repo_arg = parse_repo_arg
_parse_option = parse_option


def _cmd_patterns(rest: list[str]) -> int:
    """Handle patterns subcommands: list, show, deactivate, reactivate, exclude, unexclude."""
    from bluei.engine.pattern_store import FixPatternStore, DEACTIVATION_THRESHOLD
    from bluei.engine.pattern_replay import PROMPT_HINT_THRESHOLD
    from bluei.app.config import ConfigManager
    from bluei.app.state import StateManager

    if not rest or rest[0] in ("-h", "--help"):
        print(HELP_TEXT["patterns"])
        return 0

    subcmd = rest[0]
    sub_rest = rest[1:]

    repo = _parse_repo_arg(sub_rest)
    if not repo:
        print(
            "bluei: patterns requires --repo <name>. Try 'bluei help patterns'.",
            file=sys.stderr,
        )
        return 1

    config = ConfigManager()
    state = StateManager(config.repos_dir)
    patterns_file = state.get_fix_patterns_file(repo)

    if not patterns_file.exists() and subcmd != "list":
        print(f"bluei: no patterns file for repo '{repo}'.", file=sys.stderr)
        return 1

    store = FixPatternStore(patterns_file)

    if subcmd == "list":
        return _patterns_list(store)
    elif subcmd == "show":
        return _patterns_show(sub_rest, store)
    elif subcmd == "deactivate":
        return _patterns_deactivate(sub_rest, store)
    elif subcmd == "reactivate":
        return _patterns_reactivate(sub_rest, store)
    elif subcmd == "exclude":
        return _patterns_exclude(sub_rest, store)
    elif subcmd == "unexclude":
        return _patterns_unexclude(sub_rest, store)
    else:
        print(
            f"bluei: unknown patterns subcommand '{subcmd}'. Try 'bluei help patterns'.",
            file=sys.stderr,
        )
        return 1


def _patterns_list(store) -> int:
    from bluei.engine.pattern_store import DEACTIVATION_THRESHOLD

    patterns = store.load_active()
    if not patterns:
        print("No active patterns found.")
        return 0

    patterns.sort(key=lambda p: p.confidence, reverse=True)
    print(
        f"{'PATTERN ID':<20} {'RULE':<25} {'CONFIDENCE':>10} {'HIT':>5} {'MISS':>5} {'FAIL':>5} {'LAST USED'}"
    )
    print("-" * 91)
    for p in patterns:
        last_used = p.last_used_at[:19] if p.last_used_at else "never"
        print(
            f"{p.pattern_id:<20} {p.rule:<25} {p.confidence:>10.3f} {p.success_count:>5} {p.skip_count:>5} {p.failure_count:>5} {last_used}"
        )
    print()
    print(f"Total: {len(patterns)} active patterns")
    return 0


def _patterns_show(rest: list[str], store) -> int:
    pattern_id = None
    for i, arg in enumerate(rest):
        if not arg.startswith("-") and i == 0:
            pattern_id = arg
            break
    if not pattern_id:
        print(
            "bluei: patterns show requires a pattern_id. Try 'bluei help patterns'.",
            file=sys.stderr,
        )
        return 1

    pattern = store.get_pattern(pattern_id)
    if not pattern:
        print(f"bluei: pattern '{pattern_id}' not found.", file=sys.stderr)
        return 1

    p = pattern
    print(f"Pattern:    {p.pattern_id}")
    print(f"Rule:       {p.rule}")
    print(f"Language:   {p.language}")
    print(f"File:       {p.file_path}")
    print(f"Source:     {p.source}")
    print(f"Confidence: {p.confidence:.3f}")
    print(
        f"Replay:    HIT={p.success_count}  MISS={p.skip_count}  FAILURE={p.failure_count}"
    )
    if p.excluded_paths:
        print(f"Excluded:   {', '.join(p.excluded_paths)}")
    print(f"Created:    {p.created_at[:19]}")
    print(f"Last used:  {(p.last_used_at or 'never')[:19]}")
    print(f"Last verified: {(p.last_verified_at or 'never')[:19]}")
    print(f"Last failed: {(p.last_failed_at or 'never')[:19]}")
    if p.source_finding_ids:
        print(f"Findings:   {', '.join(p.source_finding_ids[:5])}")
    print()
    print(f"{BOLD}Before:{RESET}")
    for line in p.before_snippet.splitlines():
        print(f"  {line}")
    print()
    print(f"{BOLD}After:{RESET}")
    for line in p.after_snippet.splitlines():
        print(f"  {line}")
    print()
    if p.diff_patch:
        print(f"{BOLD}Diff patch:{RESET}")
        for line in p.diff_patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                print(f"  {GREEN}{line}{RESET}")
            elif line.startswith("-") and not line.startswith("---"):
                print(f"  {RED}{line}{RESET}")
            else:
                print(f"  {line}")
    return 0


def _patterns_deactivate(rest: list[str], store) -> int:
    pattern_id = None
    for i, arg in enumerate(rest):
        if not arg.startswith("-") and i == 0:
            pattern_id = arg
            break
    if not pattern_id:
        print(
            "bluei: patterns deactivate requires a pattern_id. Try 'bluei help patterns'.",
            file=sys.stderr,
        )
        return 1

    pattern = store.get_pattern(pattern_id)
    if not pattern:
        print(f"bluei: pattern '{pattern_id}' not found.", file=sys.stderr)
        return 1

    delta = 0.0 - pattern.confidence
    store.update_confidence(pattern_id, delta)
    print(f"Pattern {pattern_id} deactivated (confidence set to 0.0).")
    return 0


def _patterns_reactivate(rest: list[str], store) -> int:
    from bluei.engine.pattern_replay import PROMPT_HINT_THRESHOLD

    pattern_id = None
    for i, arg in enumerate(rest):
        if not arg.startswith("-") and i == 0:
            pattern_id = arg
            break
    if not pattern_id:
        print(
            "bluei: patterns reactivate requires a pattern_id. Try 'bluei help patterns'.",
            file=sys.stderr,
        )
        return 1

    pattern = store.get_pattern(pattern_id)
    if not pattern:
        print(f"bluei: pattern '{pattern_id}' not found.", file=sys.stderr)
        return 1

    delta = PROMPT_HINT_THRESHOLD - pattern.confidence
    store.update_confidence(pattern_id, delta)
    print(
        f"Pattern {pattern_id} reactivated (confidence set to {PROMPT_HINT_THRESHOLD})."
    )
    return 0


def _parse_two_positionals(rest: list[str]):
    """Extract the first two non-flag positionals (pattern_id, glob)."""
    positionals = [arg for arg in rest if not arg.startswith("-")]
    return positionals[0] if len(positionals) >= 1 else None, (
        positionals[1] if len(positionals) >= 2 else None
    )


def _patterns_exclude(rest: list[str], store) -> int:
    pattern_id, glob = _parse_two_positionals(rest)
    if not pattern_id or not glob:
        print(
            "bluei: patterns exclude requires <pattern_id> <glob>. Try 'bluei help patterns'.",
            file=sys.stderr,
        )
        return 1

    if not store.get_pattern(pattern_id):
        print(f"bluei: pattern '{pattern_id}' not found.", file=sys.stderr)
        return 1

    if store.add_excluded_path(pattern_id, glob):
        print(f"Pattern {pattern_id}: excluded path '{glob}' added.")
        return 0
    print(f"Pattern {pattern_id}: path '{glob}' already excluded (no change).")
    return 0


def _patterns_unexclude(rest: list[str], store) -> int:
    pattern_id, glob = _parse_two_positionals(rest)
    if not pattern_id or not glob:
        print(
            "bluei: patterns unexclude requires <pattern_id> <glob>. Try 'bluei help patterns'.",
            file=sys.stderr,
        )
        return 1

    if not store.get_pattern(pattern_id):
        print(f"bluei: pattern '{pattern_id}' not found.", file=sys.stderr)
        return 1

    if store.remove_excluded_path(pattern_id, glob):
        print(f"Pattern {pattern_id}: excluded path '{glob}' removed.")
        return 0
    print(f"Pattern {pattern_id}: path '{glob}' was not excluded (no change).")
    return 0
