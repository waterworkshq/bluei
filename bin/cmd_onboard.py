"""Onboarding command family.

Extracted from bin/bluei.py to reduce its surface area. Groups the
"init/onboard/upgrade" workflow: init wizard, onboarding, preflight
readiness checks, config upgrades, and safety-summary helpers.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from bin.cmd_utils import (
    has_flag,
    parse_option,
    parse_repo_arg,
)

# Backward-compat aliases for tests that import these names from bin.bluei
_parse_repo_arg = parse_repo_arg
_parse_option = parse_option
_has_flag = has_flag


def _parse_init_args(rest: List[str]) -> tuple:
    """Parse --path and --name from init arguments."""
    path = None
    name = None
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("--path", "-p"):
            i += 1
            if i < len(rest) and not rest[i].startswith("-"):
                path = rest[i]
        elif arg in ("--name", "-n"):
            i += 1
            if i < len(rest) and not rest[i].startswith("-"):
                name = rest[i]
        i += 1
    return path, name


def _cmd_init(rest: Optional[List[str]] = None):
    """Initialize a project: quick auto-register with --path, or interactive wizard."""
    from bluei.app.config import ConfigManager
    from bluei.app.registry import RepoRegistry
    from bluei.app.onboarding import detect_git_remote, detect_language

    path, name = _parse_init_args(rest or [])

    if path:
        # ── Quick auto-registration flow ──
        repo_path = Path(path).expanduser().resolve()
        if not repo_path.is_dir():
            print(f"bluei: path not found: {repo_path}", file=sys.stderr)
            return 1

        # Auto-detect git remote
        remote = detect_git_remote(repo_path)
        repo_name = name or remote.get("name") or repo_path.name

        print(f"→ Detecting project at: {repo_path}")
        if remote["detected"] == "true":
            print(f"  Git remote: {remote['url']}")
            print(f"  Platform:   {remote['platform'] or 'unknown'}")
            print(f"  Owner:      {remote.get('owner', '')}")
        else:
            print("  No git remote detected (local-only project)")

        # Auto-detect language
        lang = detect_language(repo_path)
        print(f"  Language:   {lang}")

        # Prompt for care level (default: watch-only)
        default_mode = "watch-only"
        mode = os.environ.get("BLUEI_MODE", "").lower()
        if mode not in (
            "observe",
            "issue-only",
            "pr",
            "merge",
            "watch-only",
            "note-only",
            "offer-fixes",
            "full-care",
        ):
            print()
            print("Care level (how autonomous should bluei be?):")
            print("  1) Watch only    — dry-run only (default)")
            print("  2) Note only     — create issues, no PRs")
            print("  3) Offer fixes   — issues + PRs, no merges")
            print("  4) Full care     — full autonomy")
            choice = input("Choice [1/2/3/4, default=1]: ").strip()
            mode_map = {
                "1": "watch-only",
                "2": "note-only",
                "3": "offer-fixes",
                "4": "full-care",
            }
            mode = mode_map.get(choice, "watch-only")

        print(f"→ Care level: {mode}")

        # F2: Confirm high-risk modes
        auto_yes = "--yes" in (rest or []) or os.environ.get(
            "BLUEI_YES", ""
        ).lower() in (
            "1",
            "true",
            "yes",
        )
        if not _confirm_high_risk_mode(mode, auto_yes=auto_yes):
            print("Init cancelled.", file=sys.stderr)
            return 1

        # Register in registry
        config = ConfigManager()
        registry = RepoRegistry(config)

        from bluei.app.models import SafetyMode

        internal_mode = SafetyMode.from_brand(mode).value

        try:
            registry.create_from_path(repo_path, name=repo_name, mode=internal_mode)
        except ValueError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1

        print()
        print(f"  Project '{repo_name}' registered.")
        print(f"   Language:     {lang}")
        print(f"   Care level:   {mode}")

        # F3: Print safety summary
        registered = registry.find_by_name(repo_name)
        if registered:
            _print_safety_summary(registered.config)

        print()
        print(f"   Run 'bluei scan {repo_name}' when ready.")
        return 0

    # ── Interactive wizard (no --path provided) ──
    print("bluei init — interactive setup wizard")
    print()

    # Prompt for path
    default_path = os.getcwd()
    raw_path = input(f"Project path [{default_path}]: ").strip()
    repo_path_str = raw_path or default_path
    repo_path = Path(repo_path_str).expanduser().resolve()

    if not repo_path.is_dir():
        print(f"bluei: path not found: {repo_path}", file=sys.stderr)
        return 1

    # Prompt for name
    default_name = repo_path.name
    raw_name = input(f"Project name [{default_name}]: ").strip()
    repo_name = raw_name or default_name

    # Re-enter the auto-registration flow with the collected values
    return _cmd_init(["--path", str(repo_path), "--name", repo_name])


def _confirm_high_risk_mode(mode: str, auto_yes: bool = False) -> bool:
    """F2: Require confirmation for high-risk safety modes.

    Returns True if the user confirms (or auto_yes is set), False otherwise.
    """
    from bluei.app.models import SafetyMode

    try:
        internal = SafetyMode.from_brand(mode)
    except (ValueError, KeyError):
        return True  # Unknown mode — don't block

    high_risk_messages = {
        SafetyMode.MERGE: (
            "You selected full-care mode. This will AUTO-MERGE PRs without human review.\n"
            "  bluei will create issues, open PRs, and merge them automatically."
        ),
    }

    message = high_risk_messages.get(internal)
    if not message:
        return True  # Not a high-risk mode

    if auto_yes:
        return True

    print()
    print(f"⚠  {message}")
    response = input("Continue? [y/N]: ").strip().lower()
    return response in ("y", "yes")


def _print_safety_summary(config) -> None:
    """F3: Print a dedicated safety summary section after onboarding."""
    safety = config.safety or {}
    mode = safety.get("mode", "unknown")
    profile = safety.get("profile", "unknown")
    protected = safety.get("protected_branches", [])
    fix_engine = getattr(config, "fix_engine", "unknown")
    live_actions = config.github.get("live_actions", False) if config.github else False

    mode_labels = {
        "observe": "Watch only (dry-run)",
        "issue-only": "Note only (issues, no PRs)",
        "pr": "Offer fixes (issues + PRs, no merges)",
        "merge": "Full care (full autonomy incl. auto-merge)",
    }
    mode_label = mode_labels.get(mode, mode)

    print()
    print("── Safety Policy ──────────────────────────────")
    print(f"  Mode:               {mode_label}")
    print(f"  Profile:            {profile}")
    print(f"  Protected branches: {', '.join(protected) if protected else '(none)'}")
    print(f"  Fix engine:         {fix_engine}")
    print(
        f"  GitHub live actions: {'enabled' if live_actions else 'disabled (dry-run)'}"
    )
    if safety.get("require_clean_worktree"):
        print(f"  Clean worktree:     required for live runs")
    print("───────────────────────────────────────────────")


def _cmd_onboard(rest: list[str]):
    from bluei.app.config import ConfigManager
    from bluei.app.health import HealthEngine
    from bluei.app.onboarding import OnboardEngine, OnboardOptions
    from bluei.app.plugins import PluginLoader
    from bluei.app.registry import RepoRegistry
    from bluei.app.state import StateManager

    # BLUEI_DIR imported lazily to avoid circular import with bin.bluei
    from bin.bluei import BLUEI_DIR

    repo_path = _parse_repo_arg(rest) or _parse_option(rest, "--path")
    if not repo_path:
        print("bluei: onboard requires --repo <path>.", file=sys.stderr)
        return 1

    workspace = Path(BLUEI_DIR)
    config_mgr = ConfigManager(workspace)
    engine = OnboardEngine(
        RepoRegistry(config_mgr),
        PluginLoader(workspace / "plugins"),
        HealthEngine(),
        StateManager(config_mgr.repos_dir),
    )
    options = OnboardOptions(
        name=_parse_option(rest, "--name"),
        language=_parse_option(rest, "--language"),
        framework=_parse_option(rest, "--framework"),
        plugin_id=_parse_option(rest, "--plugin"),
        template=_parse_option(rest, "--template"),
        mode=_parse_option(rest, "--mode") or "observe",
        profile=_parse_option(rest, "--profile") or "conservative",
        allow_dirty_worktree=_has_flag(rest, "--allow-dirty-worktree"),
        skip_preflight=_has_flag(rest, "--skip-preflight"),
        fix_engine=_parse_option(rest, "--fix-engine"),
    )

    # F2: Confirm high-risk modes unless --yes
    if not _confirm_high_risk_mode(options.mode, auto_yes=_has_flag(rest, "--yes")):
        print("Onboarding cancelled.", file=sys.stderr)
        return 1
    try:
        result = engine.onboard(Path(repo_path), options)
    except ValueError as exc:
        print(f"bluei: {exc}", file=sys.stderr)
        return 1

    print(f"Onboarded {result.repo.config.name}")
    print(f"Language: {result.language.name}")
    print(f"Findings: {result.findings_count}")
    if result.review_items:
        print("\nReview items:")
        for item in result.review_items:
            marker = {"info": "•", "warn": "⚠", "action-required": "⚠"}.get(
                item.severity, "•"
            )
            print(f"  {marker} [{item.severity}] {item.message}")
            if item.suggested_action:
                print(f"      → {item.suggested_action}")

    # F3: Print safety summary
    _print_safety_summary(result.repo.config)
    return 0


def _cmd_upgrade_config(rest: list[str]) -> int:
    from bluei.app.onboarding import OnboardEngine
    from bluei.app.registry import RepoRegistry
    from bluei.app.config import ConfigManager
    from bluei.app.state import StateManager
    from bluei.app.plugins import PluginLoader
    from bluei.app.health import HealthEngine

    # BLUEI_DIR imported lazily to avoid circular import with bin.bluei
    from bin.bluei import BLUEI_DIR

    workspace = Path(BLUEI_DIR)
    config_mgr = ConfigManager(workspace)
    registry = RepoRegistry(config_mgr)
    plugin_loader = PluginLoader(workspace / "plugins")
    state = StateManager(config_mgr.repos_dir)
    engine = OnboardEngine(registry, plugin_loader, HealthEngine(), state)
    repo = _parse_repo_arg(rest)
    if not repo:
        print("bluei: upgrade-config requires --repo <name>.", file=sys.stderr)
        return 1
    result = engine.upgrade_config(repo)
    print(json.dumps(result, indent=2))
    return 0


def _cmd_preflight(rest: list[str]):
    """Assess a project's readiness before onboarding."""
    from bluei.app.preflight import run_preflight

    repo_path = None
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("--repo", "--path", "-p"):
            i += 1
            if i < len(rest) and not rest[i].startswith("-"):
                repo_path = rest[i]
        i += 1

    if not repo_path:
        print("bluei: preflight requires --repo <path>", file=sys.stderr)
        print("Usage: bluei preflight --repo /path/to/project", file=sys.stderr)
        return 1

    resolved = Path(repo_path).expanduser().resolve()
    if not resolved.is_dir():
        print(f"bluei: path not found: {resolved}", file=sys.stderr)
        return 1

    result = run_preflight(resolved)

    print(f"Preflight: {resolved}")
    print(f"  Language:    {result.language}")
    if result.secondary_languages:
        print(f"  Secondary:   {', '.join(result.secondary_languages)}")
    print(f"  Plugin:      {result.plugin_id or '(none found)'}")
    print()

    # Tool checks
    print("Tool availability:")
    if result.tool_checks:
        for tc in result.tool_checks:
            status = f"✓ found ({tc.path})" if tc.found else "✗ MISSING"
            print(f"  {tc.tool:<20} {status}")
    else:
        print("  (no tools to check)")
    print()

    # Validation checks
    if result.validation_checks:
        print("Validation dry-run:")
        for vc in result.validation_checks:
            cmd_str = " ".join(vc.command)
            status = "✓ pass" if vc.passed else f"✗ fail (rc={vc.returncode})"
            print(f"  {cmd_str:<50} {status}")
            if vc.stderr_snippet and not vc.passed:
                print(f"    {vc.stderr_snippet[:100]}")
        print()

    # Errors
    for err in result.errors:
        print(f"  ⚠ {err}")

    if result.ready:
        print("→ Ready for onboarding.")
        return 0
    else:
        print("→ Not ready. Fix the issues above before onboarding.")
        return 1
