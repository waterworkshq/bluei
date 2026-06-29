"""Emergent rule command handler.

Extracted from bin/bluei.py to reduce its surface area."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

from bin.help_text import HELP_TEXT
from bin.cmd_utils import (
    has_flag,
    parse_option,
    parse_positive_int,
    parse_repo_arg,
)

# Backward-compat aliases
_parse_repo_arg = parse_repo_arg
_parse_option = parse_option
_has_flag = has_flag
_parse_positive_int = parse_positive_int


def _cmd_emergent(rest: list[str]) -> int:
    if not rest or rest[0] in ("-h", "--help"):
        print(HELP_TEXT["emergent"])
        return 0

    subcmd = rest[0]
    sub_rest = rest[1:]
    if subcmd not in {
        "propose",
        "validate",
        "scan",
        "discover",
        "shadow",
        "reject",
        "retire",
        "approve",
        "promote",
        "gc",
        "list",
        "show",
    }:
        print(
            f"bluei: unknown emergent subcommand '{subcmd}'. Try 'bluei help emergent'.",
            file=sys.stderr,
        )
        return 1

    repo = _parse_repo_arg(sub_rest)
    if not repo:
        print(
            f"bluei: emergent {subcmd} requires --repo <name>. Try 'bluei help emergent'.",
            file=sys.stderr,
        )
        return 1

    from bluei.app.config import ConfigManager
    from bluei.app.state import StateManager
    from bluei.app.emergent_rules import EmergentRuleStore

    state_root = _parse_option(sub_rest, "--state-root")
    repos_dir = Path(state_root) if state_root else ConfigManager().repos_dir
    state = StateManager(repos_dir)
    store = EmergentRuleStore(state.get_emergent_rules_file(repo))

    if subcmd == "propose":
        run_id = _parse_option(sub_rest, "--run-id") or "manual"
        if _has_flag(sub_rest, "--from-patterns"):
            from bluei.engine.pattern_store import open_pattern_store

            patterns_file = state.get_fix_patterns_file(repo)
            patterns = open_pattern_store(patterns_file).load_active()
            result = store.observe_fix_patterns(
                patterns,
                run_id=run_id,
                min_success_count=_parse_positive_int(
                    _parse_option(sub_rest, "--min-success-count"),
                    default=5,
                ),
            )
        else:
            findings = state.load_findings(repo)
            min_observations = _parse_positive_int(
                _parse_option(sub_rest, "--min-observations"),
                default=5,
            )
            result = store.observe_findings(
                findings,
                run_id=run_id,
                min_observations=min_observations,
                existing_rule_ids=_detector_catalog_rule_ids(),
            )
        print(
            "Emergent proposals: "
            f"created={result['created']} updated={result['updated']} skipped={result['skipped']}"
        )
        return 0

    if subcmd == "validate":
        result = store.validate_proposals(
            existing_rule_ids=_detector_catalog_rule_ids()
        )
        print(
            "Emergent validation: "
            f"candidate={result['candidate']} rejected={result['rejected']} unchanged={result['unchanged']}"
        )
        return 0

    if subcmd == "scan":
        worktree = _parse_option(sub_rest, "--worktree")
        if not worktree:
            print(
                "bluei: emergent scan requires --worktree <path>. Try 'bluei help emergent'.",
                file=sys.stderr,
            )
            return 1
        from bluei.app.emergent_rules import scan_shadow_rules

        matches = scan_shadow_rules(store.load(), Path(worktree))
        print(f"Emergent scan: matches={len(matches)}")
        for match in matches:
            print(f"{match.rule_id} {match.path}:{match.line} {match.snippet}")
        return 0

    if subcmd == "discover":
        worktree = _parse_option(sub_rest, "--worktree")
        if not worktree:
            print(
                "bluei: emergent discover requires --worktree <path>. Try 'bluei help emergent'.",
                file=sys.stderr,
            )
            return 1
        from bluei.app.emergent_rules import discover_active_rule_findings

        findings = discover_active_rule_findings(
            store.load(), Path(worktree), repo_name=repo
        )
        written = state.append_findings(repo, findings)
        print(f"Emergent discover: findings={len(findings)} written={written}")
        return 0

    if subcmd == "shadow":
        rule_id = _positional_arg(sub_rest)
        if not rule_id:
            print(
                "bluei: emergent shadow requires a rule_id. Try 'bluei help emergent'.",
                file=sys.stderr,
            )
            return 1
        try:
            from bluei.app.emergent_rules import measure_false_positives

            rule = next((r for r in store.load() if r.rule_id == rule_id), None)
            measured_fps = measure_false_positives(rule) if rule is not None else 0
            result = store.record_shadow_run(
                rule_id,
                matches=_parse_positive_int(
                    _parse_option(sub_rest, "--matches"), default=0
                ),
                false_positives=measured_fps,
                min_shadow_runs=_parse_positive_int(
                    _parse_option(sub_rest, "--min-shadow-runs"),
                    default=3,
                ),
            )
        except KeyError:
            print(f"bluei: emergent rule '{rule_id}' not found.", file=sys.stderr)
            return 1
        print(
            "Emergent shadow: "
            f"rule={rule_id} status={result['status']} "
            f"shadow_runs={result.get('shadow_runs', 0)} "
            f"false_positive_rate={result.get('false_positive_rate', 0.0):.3f}"
        )
        return 0

    if subcmd == "approve":
        rule_id = _positional_arg(sub_rest)
        if not rule_id:
            print(
                "bluei: emergent approve requires a rule_id. Try 'bluei help emergent'.",
                file=sys.stderr,
            )
            return 1
        try:
            result = store.approve_rule(rule_id)
        except KeyError:
            print(f"bluei: emergent rule '{rule_id}' not found.", file=sys.stderr)
            return 1
        if not result.get("updated"):
            print(
                f"Emergent approve: rule={rule_id} status={result['status']} reason={result.get('reason', '')}"
            )
        else:
            print(f"Emergent approve: rule={rule_id} status={result['status']}")
        return 0

    if subcmd == "promote":
        rule_id = _positional_arg(sub_rest)
        if not rule_id:
            print(
                "bluei: emergent promote requires a rule_id. Try 'bluei help emergent'.",
                file=sys.stderr,
            )
            return 1
        plugin_dir_arg = _parse_option(sub_rest, "--plugin-dir")
        plugin_dir = Path(plugin_dir_arg) if plugin_dir_arg else None
        try:
            result = store.promote_rule(rule_id, plugin_dir=plugin_dir)
        except KeyError:
            print(f"bluei: emergent rule '{rule_id}' not found.", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0

    if subcmd == "gc":
        result = store.retire_stale_rules()
        print(f"Emergent gc: retired={result['retired']}")
        return 0

    if subcmd in {"reject", "retire"}:
        rule_id = _positional_arg(sub_rest)
        if not rule_id:
            print(
                f"bluei: emergent {subcmd} requires a rule_id. Try 'bluei help emergent'.",
                file=sys.stderr,
            )
            return 1
        reason = _parse_option(sub_rest, "--reason") or "manual"
        try:
            if subcmd == "reject":
                result = store.reject_rule(rule_id, reason=reason)
            else:
                result = store.retire_rule(rule_id, reason=reason)
        except KeyError:
            print(f"bluei: emergent rule '{rule_id}' not found.", file=sys.stderr)
            return 1
        print(f"Emergent {subcmd}: rule={rule_id} status={result['status']}")
        return 0

    if subcmd == "list":
        return _emergent_list(store)

    rule_id = _positional_arg(sub_rest)
    if not rule_id:
        print(
            "bluei: emergent show requires a rule_id. Try 'bluei help emergent'.",
            file=sys.stderr,
        )
        return 1
    return _emergent_show(store, rule_id)


def _positional_arg(rest: list[str]) -> str | None:
    for arg in rest:
        if arg.startswith("-"):
            return None
        return arg
    return None


def _detector_catalog_rule_ids() -> set[str]:
    try:
        from bluei.engine.constants import DETECTOR_CATALOG
    except Exception:
        return set()
    return {str(entry.get("rule")) for entry in DETECTOR_CATALOG if entry.get("rule")}


def _emergent_list(store) -> int:
    rules = store.load()
    if not rules:
        print("No emergent rules found.")
        return 0
    print(f"{'RULE ID':<16} {'STATUS':<10} {'OBS':>4} {'PATTERN'}")
    for rule in rules:
        pattern = rule.detection_pattern
        print(
            f"{rule.rule_id:<16} {rule.status.value:<10} "
            f"{rule.observation_count:>4} {pattern.search_pattern} in {pattern.file_glob}"
        )
    return 0


def _emergent_show(store, rule_id: str) -> int:
    for rule in store.load():
        if rule.rule_id != rule_id:
            continue
        pattern = rule.detection_pattern
        print(f"Rule: {rule.rule_id}")
        print(f"Status: {rule.status.value}")
        print(f"Header: {rule.header}")
        print(f"Pattern: {pattern.search_pattern} in {pattern.file_glob}")
        print(f"Language: {rule.language}")
        print(f"Category: {rule.category}")
        print(f"Observations: {rule.observation_count}")
        print(f"Shadow runs: {rule.shadow_runs}")
        print(f"False positive rate: {rule.false_positive_rate:.3f}")
        if rule.evidence_runs:
            print(f"Evidence runs: {', '.join(rule.evidence_runs)}")
        if rule.source_finding_ids:
            print(f"Source findings: {', '.join(rule.source_finding_ids)}")
        if rule.rejected_reason:
            print(f"Rejected reason: {rule.rejected_reason}")
        if rule.notes:
            print(f"Notes: {rule.notes}")
        return 0
    print(f"bluei: emergent rule '{rule_id}' not found.", file=sys.stderr)
    return 1
