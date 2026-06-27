"""bluei learn — read-only governance surface + Golden Bundle creation.

Per ADR-0015: the read path is unified under ``learn`` (inbox, status, audit),
and bundle creation lives here too. Write-verbs (approve, reject, pause,
reactivate) stay native — they live in ``patterns`` / ``emergent`` because
they mutate the ApprovalRecord trail and the underlying store.

Subcommands:
  - inbox:  list pending governance approvals
  - status: render native state + Governance State + SPRT evidence
  - audit:  full decision history filtered by asset_ref
  - bundle: create a Golden Validation Bundle from a known-good fix
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from bin.help_text import HELP_TEXT
from bin.cmd_utils import parse_option, parse_repo_arg

# Backward-compat aliases (match sibling cmd_* modules)
_parse_repo_arg = parse_repo_arg
_parse_option = parse_option


# ---------------------------------------------------------------------------
# Detector derivation (PRD §7)
# ---------------------------------------------------------------------------


def _derive_detector_command(rule: str) -> Optional[list[str]]:
    """Map a rule id to its detector argv, or ``None`` if unknown.

    Simple prefix-based mapping. ``ruff-<CODE>`` becomes
    ``ruff check --select <CODE> --output-format=concise -`` (reading the file
    content from stdin so we never have to write a temp file just to inspect
    detector output). Unknown rule prefixes return ``None`` — the bundle then
    records empty ``detector_before`` / ``detector_after``.
    """
    if rule.startswith("ruff-"):
        code = rule[len("ruff-") :].upper()
        return [
            "ruff",
            "check",
            "--select",
            code,
            "--output-format=concise",
            "-",
        ]
    return None


def _run_detector_on_content(argv: list[str], content: str) -> str:
    """Run *argv* with *content* piped to stdin; return captured stdout.

    Returns the empty string on any failure (missing binary, timeout, OS
    error). Bundle creation is intentionally resilient: a missing detector
    must not block the operator — the bundle just records an empty result.
    """
    try:
        result = subprocess.run(
            argv,
            input=content,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout or ""


def _apply_before_after(content: str, before: str, after: str) -> Optional[str]:
    """Apply the first-occurrence ``before`` → ``after`` replacement.

    Uses the same normalized-snippet matcher as
    :func:`bluei.engine.pattern_replay._find_snippet_in_file` so indentation
    differences don't block matching. Returns the rewritten content, or
    ``None`` if the snippet wasn't found.
    """
    from bluei.engine.pattern_replay import _find_snippet_in_file

    match = _find_snippet_in_file(content, before)
    if match is None:
        return None
    offset, length = match
    return content[:offset] + after + content[offset + length :]


def _first_positional(rest: list[str]) -> Optional[str]:
    """Return the first positional argument in *rest*, skipping option values.

    Known value-consuming options in this namespace (``--repo``,
    ``--state-root``, ``--worktree``, ``--from-finding``) are skipped together
    with their values so that ``--repo myrepo`` is not mistaken for a
    positional asset_ref.
    """
    skip_next = False
    for arg in rest:
        if skip_next:
            skip_next = False
            continue
        if arg in _VALUE_OPTIONS:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


# Options in the ``learn`` namespace that consume the following positional as
# their value (and therefore must not be treated as positionals themselves).
_VALUE_OPTIONS = {"--repo", "--state-root", "--worktree", "--from-finding"}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _cmd_learn(rest: list[str]) -> int:
    """Top-level dispatcher for the ``bluei learn`` namespace."""
    if not rest or rest[0] in ("-h", "--help"):
        print(HELP_TEXT["learn"])
        return 0

    subcmd = rest[0]
    sub_rest = rest[1:]

    if subcmd not in {"inbox", "status", "audit", "bundle"}:
        print(
            f"bluei: unknown learn subcommand '{subcmd}'. Try 'bluei help learn'.",
            file=sys.stderr,
        )
        return 1

    repo = _parse_repo_arg(sub_rest)
    if not repo:
        print(
            f"bluei: learn {subcmd} requires --repo <name>. Try 'bluei help learn'.",
            file=sys.stderr,
        )
        return 1

    from bluei.app.config import ConfigManager
    from bluei.app.state import StateManager

    state_root = _parse_option(sub_rest, "--state-root")
    repos_dir = Path(state_root) if state_root else ConfigManager().repos_dir
    state = StateManager(repos_dir)
    state_dir = state.get_state_file(repo).parent

    if subcmd == "inbox":
        return _learn_inbox(repo, state_dir)

    if subcmd == "status":
        asset_ref = _first_positional(sub_rest)
        if not asset_ref:
            print(
                "bluei: learn status requires an <asset_ref>. Try 'bluei help learn'.",
                file=sys.stderr,
            )
            return 1
        return _learn_status(asset_ref, repo, state_dir, state)

    if subcmd == "audit":
        asset_ref = _first_positional(sub_rest)
        if not asset_ref:
            print(
                "bluei: learn audit requires an <asset_ref>. Try 'bluei help learn'.",
                file=sys.stderr,
            )
            return 1
        return _learn_audit(asset_ref, state_dir)

    # bundle
    pattern_id = _first_positional(sub_rest)
    if not pattern_id:
        print(
            "bluei: learn bundle requires a <pattern_id>. Try 'bluei help learn'.",
            file=sys.stderr,
        )
        return 1
    worktree = _parse_option(sub_rest, "--worktree")
    finding_id = _parse_option(sub_rest, "--from-finding")
    if not worktree or not finding_id:
        print(
            "bluei: learn bundle requires --worktree <path> "
            "--from-finding <id>. Try 'bluei help learn'.",
            file=sys.stderr,
        )
        return 1
    return _learn_bundle(pattern_id, Path(worktree), finding_id, repo, state_dir, state)


# ---------------------------------------------------------------------------
# inbox
# ---------------------------------------------------------------------------


def _learn_inbox(repo: str, state_dir: Path) -> int:
    """List pending governance approvals for the repo.

    An asset appears in the inbox only while it is CURRENTLY in the pending
    Governance State — i.e. its most recent decision is still
    ``pending_approval``. A subsequent ``approve`` / ``reject`` / ``pause``
    removes it from the inbox even though the historical ``pending_approval``
    record stays in the append-only trail (ADR-0008).
    """
    from bluei.engine.governance import project_governance_state
    from bluei.engine.jsonl import read_jsonl

    records = read_jsonl(state_dir / "approval_records.jsonl")
    gov_state = project_governance_state(records)

    pending_refs = {ref for ref, state in gov_state.items() if state == "pending"}
    if not pending_refs:
        print("No pending approvals.")
        return 0

    # Surface the most recent record for each pending asset_ref so the operator
    # sees a single line per pending approval with the freshest reason/timestamp.
    latest_by_ref: dict[str, dict] = {}
    for r in records:
        ref = r.get("asset_ref")
        if ref in pending_refs and r.get("decision") == "pending_approval":
            latest_by_ref[ref] = r

    pending_items = sorted(
        latest_by_ref.values(),
        key=lambda r: r.get("timestamp", ""),
    )
    print(f"Pending approvals for '{repo}' ({len(pending_items)}):\n")
    for r in pending_items:
        ref = r.get("asset_ref", "?")
        reason = r.get("reason", "")
        ts = r.get("timestamp", "?")
        print(f"  {ref}")
        print(f"    timestamp: {ts}")
        if reason:
            print(f"    reason:    {reason}")
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _learn_status(asset_ref: str, repo: str, state_dir: Path, state) -> int:
    """Render Governance State + native state + recent SPRT evidence."""
    from bluei.engine.governance import (
        parse_asset_ref,
        project_governance_state,
    )
    from bluei.engine.jsonl import read_jsonl

    records = read_jsonl(state_dir / "approval_records.jsonl")
    gov_state = project_governance_state(records)

    print(f"asset_ref:        {asset_ref}")
    print(f"governance_state: {gov_state.get(asset_ref, 'active')}")

    asset_class, asset_id = parse_asset_ref(asset_ref)

    # Native state for pattern assets
    if asset_class == "pattern":
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(state.get_fix_patterns_file(repo))
        pattern = store.get_pattern(asset_id)
        if pattern is None:
            print("native_state:     (pattern not found in store)")
        else:
            print(f"native_state:     active (confidence={pattern.confidence:.3f})")
            print(
                f"replay_counts:    HIT={pattern.success_count} "
                f"MISS={pattern.skip_count} "
                f"FAILURE={pattern.failure_count}"
            )

    # Recent SPRT / dry-replay evidence
    dr_path = state_dir / "dry_replay.jsonl"
    dr_records = read_jsonl(dr_path) if dr_path.exists() else []
    evidences = [r for r in dr_records if r.get("pattern_id") == asset_id]
    if not evidences:
        print("recent_evidence:  (no dry-replay records)")
        return 0

    recent = evidences[-10:]
    counts = {"HIT": 0, "MISS": 0, "FAILURE": 0}
    for r in recent:
        outcome = r.get("would_have_outcome", "?")
        counts[outcome] = counts.get(outcome, 0) + 1
    print(
        f"recent_evidence:  HIT={counts['HIT']} "
        f"MISS={counts['MISS']} "
        f"FAILURE={counts['FAILURE']} "
        f"(last {len(recent)} of {len(evidences)})"
    )
    for r in recent[-5:]:
        print(f"  {r.get('timestamp', '?')[:19]}  {r.get('would_have_outcome', '?')}")
    return 0


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def _learn_audit(asset_ref: str, state_dir: Path) -> int:
    """Show full decision history for ``asset_ref`` chronologically."""
    from bluei.engine.jsonl import read_jsonl

    records = read_jsonl(state_dir / "approval_records.jsonl")
    history = [r for r in records if r.get("asset_ref") == asset_ref]
    if not history:
        print(f"No decision history for '{asset_ref}'.")
        return 0

    print(f"Decision history for '{asset_ref}' ({len(history)} records):\n")
    for r in history:
        ts = r.get("timestamp", "?")
        decision = r.get("decision", "?")
        actor = r.get("actor", "")
        reason = r.get("reason", "")
        print(f"  {ts}")
        print(f"    decision: {decision}")
        if actor:
            print(f"    actor:    {actor}")
        if reason:
            print(f"    reason:   {reason}")
    return 0


# ---------------------------------------------------------------------------
# bundle
# ---------------------------------------------------------------------------


def _learn_bundle(
    pattern_id: str,
    worktree: Path,
    finding_id: str,
    repo: str,
    state_dir: Path,
    state,
) -> int:
    """Create a Golden Validation Bundle from a known-good fix.

    Reads the file at ``finding.path`` in *worktree* (this is ``before``),
    applies the Pattern's ``before_snippet`` → ``after_snippet`` to get
    ``after``, re-runs the derived detector on both, and writes a YAML bundle
    to ``<state_dir>/golden_bundles/<pattern_id>-<finding_id>.yaml``.
    """
    import yaml

    from bluei.engine.bundle_loader import GoldenBundle
    from bluei.engine.governance import format_asset_ref
    from bluei.engine.jsonl import read_jsonl
    from bluei.engine.models import now_iso
    from bluei.engine.pattern_store import FixPatternStore

    store = FixPatternStore(state.get_fix_patterns_file(repo))
    pattern = store.get_pattern(pattern_id)
    if pattern is None:
        print(f"bluei: pattern '{pattern_id}' not found.", file=sys.stderr)
        return 1

    findings_records = read_jsonl(state.get_findings_file(repo))
    finding_rec = next(
        (r for r in findings_records if r.get("finding_id") == finding_id),
        None,
    )
    if finding_rec is None:
        print(f"bluei: finding '{finding_id}' not found.", file=sys.stderr)
        return 1

    target_rel = finding_rec.get("path", "")
    target_file = Path(worktree) / target_rel
    if not target_file.exists():
        print(
            f"bluei: target file not found in worktree: {target_file}",
            file=sys.stderr,
        )
        return 1

    before_content = target_file.read_text(encoding="utf-8")
    after_content = _apply_before_after(
        before_content, pattern.before_snippet, pattern.after_snippet
    )
    if after_content is None:
        print(
            f"bluei: pattern before_snippet not found in {target_rel}.",
            file=sys.stderr,
        )
        return 1

    # Derive detector and run on both states. Unknown rule → empty outputs.
    detector_argv = _derive_detector_command(pattern.rule)
    detector_before = ""
    detector_after = ""
    if detector_argv is not None:
        detector_before = _run_detector_on_content(detector_argv, before_content)
        detector_after = _run_detector_on_content(detector_argv, after_content)

    rule_family = pattern.rule.split("-", 1)[0] if "-" in pattern.rule else pattern.rule
    bundle = GoldenBundle(
        id=f"{pattern_id}-{finding_id}",
        asset_class="pattern",
        asset_ref=format_asset_ref("pattern", pattern_id),
        rule=pattern.rule,
        rule_family=rule_family,
        language=pattern.language,
        before=pattern.before_snippet,
        after=pattern.after_snippet,
        detector_before=detector_before,
        detector_after=detector_after,
        validation_command=" ".join(detector_argv) if detector_argv else "",
        source_finding_id=finding_id,
        extracted_at=now_iso(),
    )

    bundles_dir = state_dir / "golden_bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)
    out_path = bundles_dir / f"{pattern_id}-{finding_id}.yaml"

    data = {
        "id": bundle.id,
        "asset_class": bundle.asset_class,
        "asset_ref": bundle.asset_ref,
        "rule": bundle.rule,
        "rule_family": bundle.rule_family,
        "language": bundle.language,
        "before": bundle.before,
        "after": bundle.after,
        "detector_before": bundle.detector_before,
        "detector_after": bundle.detector_after,
        "validation_command": bundle.validation_command,
        "source_finding_id": bundle.source_finding_id,
        "extracted_at": bundle.extracted_at,
    }
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)

    print(f"Bundle written: {out_path}")
    return 0
