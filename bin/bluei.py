#!/usr/bin/env python3
"""
bluei — Trust the silence.

Thin, smart dispatcher. Handles venv bootstrap, lexicon mapping,
help text, and dispatch — then hands off to the engine.
"""

import os
import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

# ── Resolve paths ──────────────────────────────────────────────
BLUEI_DIR = Path(__file__).resolve().parent.parent
ENGINE_MODULE = "bluei.engine"
VENV_PYTHON = BLUEI_DIR / ".venv" / "bin" / "python3"
BLUEI_SECRETS = BLUEI_DIR / "bin" / "bluei_secrets.py"
if str(BLUEI_DIR) in sys.path:
    sys.path.remove(str(BLUEI_DIR))
sys.path.insert(0, str(BLUEI_DIR))

from bluei.version import __version__ as BLUEI_VERSION

try:
    from bluei.campaigns.types import CampaignStatus, PhaseStatus
except ImportError:
    CampaignStatus = None
    PhaseStatus = None


# ── Colors and help text ──────────────────────────────────────
from bin.help_text import BOLD, CYAN, DIM, GREEN, HELP_TEXT, RED, RESET, YELLOW


# ── Venv bootstrap ──────────────────────────────────────────────
def _has_venv() -> bool:
    return VENV_PYTHON.exists()


def _python_works() -> bool:
    try:
        subprocess.run([str(VENV_PYTHON), "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def _bootstrap() -> bool:
    print("bluei: Python environment not found, running bootstrap...", file=sys.stderr)
    bootstrap_script = BLUEI_DIR / "scripts" / "bootstrap.sh"
    try:
        subprocess.run(["bash", str(bootstrap_script)], cwd=str(BLUEI_DIR), check=True)
        return True
    except subprocess.CalledProcessError:
        print(
            f"bluei: bootstrap failed. Run 'bash {bootstrap_script}' manually.",
            file=sys.stderr,
        )
        return False


def _ensure_python() -> str:
    """Return a working python3 path. Bootstraps if needed."""
    if _has_venv() and _python_works():
        return str(VENV_PYTHON)
    if not _has_venv() and _bootstrap() and _python_works():
        return str(VENV_PYTHON)
    for candidate in ["python3", "python"]:
        try:
            subprocess.run([candidate, "--version"], capture_output=True, text=True)
            return candidate
        except FileNotFoundError:
            continue
    print("bluei: no Python interpreter found.", file=sys.stderr)
    sys.exit(1)


def _load_review_care_diagnostics(state, repo_name: str) -> dict:
    """Build review-care diagnostics from persisted active/review state."""
    active_state = state.load_active_prs(repo_name)
    review_state = state.load_review_state(repo_name)
    prs = active_state.get("prs", {}) if isinstance(active_state, dict) else {}
    review_prs = review_state.get("prs", {}) if isinstance(review_state, dict) else {}

    pending_push = []
    failed_push = []
    for pr_key, pr in prs.items():
        execution = pr.get("execution_result") or {}
        push_result = execution.get("push_result") or {}
        review = review_prs.get(str(pr_key), {})
        detail = {
            "pr_number": pr.get("pr_number") or int(pr_key),
            "branch": pr.get("branch", ""),
            "changed_files": execution.get("changed_files", []),
            "push_target_branch": push_result.get("target_branch", ""),
            "attempts_used": review.get("attempts_used", 0),
            "reason": review.get("last_action_reason", ""),
        }
        if pr.get("status") == "retry_pending_push":
            pending_push.append(detail)
        elif pr.get("status") == "retry_failed_push":
            failed_push.append(detail)

    return {
        "pending_push_prs": len(pending_push),
        "failed_push_prs": len(failed_push),
        "pending_push_prs_detail": pending_push,
        "failed_push_prs_detail": failed_push,
    }


# ── Command dispatch ───────────────────────────────────────────
def _run_engine(args: list[str]) -> int:
    """Run the engine with given args, returns exit code."""
    python = _ensure_python()
    cmd = [python, "-m", ENGINE_MODULE] + args
    result = subprocess.run(cmd)
    return result.returncode


def _cmd_version():
    """Print version info — bluei + backend."""
    try:
        python = _ensure_python()
        backend = subprocess.run(
            [python, "-m", ENGINE_MODULE, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        backend_ver = backend.stdout.strip()
    except Exception:
        backend_ver = None

    print(f"bluei version {BLUEI_VERSION}")
    if backend_ver:
        print(f"backend: {backend_ver}")
    return 0


def _cmd_help(cmd: str | None = None):
    """Print help text."""
    if cmd and cmd in HELP_TEXT:
        print(HELP_TEXT[cmd])
    else:
        print(HELP_TEXT["bluei"])
    return 0


def _help_requested(args: list[str]) -> bool:
    """Check if -h/--help is anywhere in remaining args."""
    return any(a in ("-h", "--help") for a in args)


def _requires_name(cmd: str, rest: list[str]) -> bool:
    """Validate that a positional name was provided; return error if not."""
    if not rest or rest[0].startswith("-"):
        print(
            f"bluei: '{cmd}' needs a project name. Try 'bluei {cmd} <project>'.",
            file=sys.stderr,
        )
        return False
    return True


# ── Subcommand handlers ────────────────────────────────────────
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
        print()
        print(f"   Run 'bluei scan {repo_name}' when ready.")
        return 0

    # ── Fall through to interactive wizard ──
    return _run_engine(["init"])


def _cmd_onboard(rest: list[str]):
    from bluei.app.config import ConfigManager
    from bluei.app.health import HealthEngine
    from bluei.app.onboarding import OnboardEngine, OnboardOptions
    from bluei.app.plugins import PluginLoader
    from bluei.app.registry import RepoRegistry
    from bluei.app.state import StateManager

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
    )
    try:
        result = engine.onboard(Path(repo_path), options)
    except ValueError as exc:
        print(f"bluei: {exc}", file=sys.stderr)
        return 1

    print(f"Onboarded {result.repo.config.name}")
    print(f"Language: {result.language.name}")
    print(f"Findings: {result.findings_count}")
    return 0


def _cmd_upgrade_config(rest: list[str]) -> int:
    from bluei.app.onboarding import OnboardEngine
    from bluei.app.registry import RepoRegistry
    from bluei.app.config import ConfigManager
    from bluei.app.state import StateManager
    from bluei.app.plugins import PluginLoader
    from bluei.app.health import HealthEngine

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


def _cmd_install():
    print(HELP_TEXT["install"])
    return 0


def _cmd_update():
    return _run_engine(["update"])


def _cmd_status(rest: list[str]):
    from bluei.app.config import ConfigManager
    from bluei.app.registry import RepoRegistry

    registry = RepoRegistry(ConfigManager())
    repos = registry.list_all()
    print("Repositories:")
    if rest and not rest[0].startswith("-"):
        repo = registry.find_by_name(rest[0])
        if not repo:
            print(f"bluei: project not found: {rest[0]}", file=sys.stderr)
            return 1
        print(
            f"{repo.config.name}: {repo.status.value} vitality={repo.current_health_score:.1f}"
        )
        return 0
    if not repos:
        print("bluei: no projects registered")
        return 0
    for repo in repos:
        print(
            f"{repo.config.name}: {repo.status.value} vitality={repo.current_health_score:.1f}"
        )
    return 0


def _cmd_repos(rest: list[str]):
    from bluei.app.config import ConfigManager
    from bluei.app.registry import RepoRegistry

    registry = RepoRegistry(ConfigManager())
    repos = registry.list_all()
    if not repos:
        print("bluei: no projects registered")
        return 0

    enabled_only = "--all" not in rest
    if enabled_only:
        repos = [r for r in repos if r.config.enabled]

    print(f"{'Name':<30} {'Language':<12} {'Enabled':<8} {'Path'}")
    print("-" * 80)
    for repo in repos:
        enabled = "yes" if repo.config.enabled else "no"
        lang = repo.config.language or "unknown"
        path = str(repo.config.path)[:40]
        print(f"{repo.config.name:<30} {lang:<12} {enabled:<8} {path}")
    return 0


def _cmd_scan(name: str, passthrough: list[str]):
    return _cmd_run(name, ["--phase", "issue-cycle", "--no-dry-run"] + passthrough)


def _cmd_clean(name: str, passthrough: list[str]):
    return _cmd_run(name, ["--phase", "pr-cycle", "--no-dry-run"] + passthrough)


def _cmd_duster(name: str):
    return _cmd_run(name, ["--phase", "issue-cycle", "--dry-run"])


def _cmd_doctor(rest: list[str]):
    checks = [
        ("workspace", BLUEI_DIR.exists(), str(BLUEI_DIR)),
        ("python", bool(_ensure_python()), _ensure_python()),
        ("plugins", (BLUEI_DIR / "plugins").exists(), str(BLUEI_DIR / "plugins")),
        ("templates", (BLUEI_DIR / "templates").exists(), str(BLUEI_DIR / "templates")),
    ]

    from pathlib import Path
    from bluei.engine.notify import load_notification_config

    notif_config = load_notification_config(None, Path.cwd())
    notif_enabled = notif_config.get("enabled", False)
    notif_channels = notif_config.get("channels", [])
    has_channels = len(notif_channels) > 0

    if notif_enabled:
        checks.append(
            (
                "notifications",
                has_channels,
                f"enabled, {len(notif_channels)} channel(s)",
            )
        )
        from bluei.engine.notifiers import CHANNEL_REGISTRY

        for i, ch in enumerate(notif_channels):
            ch_type = ch.get("type", "?")
            registered = ch_type in CHANNEL_REGISTRY
            has_url = bool(ch.get("url") or ch.get("to"))
            ok = registered and has_url
            detail = f"channel[{i}] type={ch_type}" + (
                ""
                if ok
                else f" ({'unregistered' if not registered else 'missing config'})"
            )
            checks.append((f"notif-channel-{i}", ok, detail))
    else:
        checks.append(("notifications", True, "disabled (default)"))

    print("bluei doctor")
    failed = 0
    for name, ok, detail in checks:
        print(f"{'OK' if ok else 'ERR'} {name}: {detail}")
        failed += 0 if ok else 1
    return 0 if failed == 0 else 1


def _cmd_run(name: str, passthrough: list[str]):
    from bluei.app.config import ConfigManager
    from bluei.app.health import HealthEngine
    from bluei.app.registry import RepoRegistry
    from bluei.app.runner import RunEngine, RunOptions
    from bluei.app.state import StateManager

    config_mgr = ConfigManager(Path(BLUEI_DIR))
    registry = RepoRegistry(config_mgr)
    repo = registry.find_by_name(name)
    if not repo:
        print(f"bluei: project not found: {name}", file=sys.stderr)
        return 1

    phase = _parse_option(passthrough, "--phase") or "orchestrated"
    dry_run = _has_flag(passthrough, "--dry-run") or not _has_flag(
        passthrough, "--no-dry-run"
    )
    fix_engine = _parse_option(passthrough, "--fix-engine")
    engine = RunEngine(
        registry, StateManager(config_mgr.repos_dir), HealthEngine(), config_mgr
    )
    result = engine.run(
        repo, RunOptions(phase=phase, dry_run=dry_run, fix_engine=fix_engine)
    )
    if result.output:
        print(result.output, end="" if result.output.endswith("\n") else "\n")
    if result.error:
        print(result.error, file=sys.stderr)
    print(f"Findings: {result.run.findings_detected}")
    return 0 if result.success else 1


def _cmd_secrets(path: str):
    python = _ensure_python()
    return subprocess.run([python, str(BLUEI_SECRETS), path]).returncode


def _cmd_health(name: str, passthrough: list[str]):
    return _run_engine(["health", "--repo", name] + passthrough)


def _cmd_report(name: str, passthrough: list[str]):
    return _run_engine(["report", "--repo", name, "--format", "pdf"] + passthrough)


def _cmd_dashboard(rest: list[str]) -> int:
    import webbrowser

    from bluei.app.config import ConfigManager
    from bluei.app.dashboard import write_dashboard
    from bluei.app.dashboard import build_dashboard_data, render_dashboard_html

    output_format = _parse_option(rest, "--format") or "html"
    if output_format not in {"html", "json"}:
        print("bluei: dashboard --format must be html or json.", file=sys.stderr)
        return 1
    state_root = _parse_option(rest, "--state-root")
    config = ConfigManager()
    repos_dir = Path(state_root) if state_root else config.repos_dir
    output = _parse_option(rest, "--output") or _parse_option(rest, "-o")
    if output:
        output_path = Path(output)
    else:
        suffix = "json" if output_format == "json" else "html"
        output_path = config.workspace / f"bluei-dashboard.{suffix}"
    try:
        written = write_dashboard(
            repos_dir=repos_dir,
            output_path=output_path,
            output_format=output_format,
            repo_name=_parse_repo_arg(rest),
        )
    except ValueError as exc:
        print(f"bluei: {exc}", file=sys.stderr)
        return 1
    print(f"Dashboard written: {written}")
    if _has_flag(rest, "--open"):
        webbrowser.open(f"file://{written}")
    if _has_flag(rest, "--watch"):
        import time

        interval = 30
        for i, arg in enumerate(rest):
            if arg.startswith("--interval="):
                interval = int(arg.split("=")[1])
            elif arg == "--interval" and i + 1 < len(rest):
                interval = int(rest[i + 1])
        print(f"Watching with interval={interval}s. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(interval)
                write_dashboard(
                    repos_dir=repos_dir,
                    output_path=output_path,
                    output_format=output_format,
                    repo_name=_parse_repo_arg(rest),
                )
                print(f"Refreshed at {time.strftime('%H:%M:%S')}")
        except KeyboardInterrupt:
            print("Watch stopped.")
    return 0


def _cmd_languages(rest: list[str]) -> int:
    from bluei.app.plugins import PluginLoader

    loader = PluginLoader(BLUEI_DIR / "plugins")
    manifests = loader.discover()
    for manifest in sorted(manifests, key=lambda item: item.get("id", "")):
        rules = (manifest.get("discovery") or {}).get("rules") or []
        languages = ",".join(manifest.get("languages") or [])
        print(f"{manifest.get('id')} [{languages}] {len(rules)} rule(s)")
        for rule in rules:
            print(
                f"  {rule.get('id')} "
                f"category={rule.get('category', 'lint')} "
                f"confidence={float(rule.get('confidence', 0.0)):.2f} "
                f"autofix={bool(rule.get('autofix', False))}"
            )
    return 0


def _cmd_preflight(rest: list[str]):
    return _run_engine(["preflight"] + rest)


def _cmd_heal(rest: list[str]):
    return _run_engine(["heal"] + rest)


def _cmd_ci(name: str, passthrough: list[str]):
    return _run_engine(["ci", "--repo", name] + passthrough)


def _cmd_install_hook(name: str, passthrough: list[str]):
    return _run_engine(["install-hook", "--repo", name] + passthrough)


def _cmd_install_cron(name: str, passthrough: list[str]):
    return _run_engine(["install-cron", "--repo", name] + passthrough)


# ── Shared utilities ──────────────────────────────────────────
from bin.cmd_utils import (
    has_flag as _has_flag,
    parse_csv_option as _parse_csv_option,
    parse_option as _parse_option,
    parse_positive_int as _parse_positive_int,
    parse_repo_arg as _parse_repo_arg,
)


def _cmd_campaign(rest: list[str]) -> int:
    from bin.cmd_campaign import _cmd_campaign as _impl

    return _impl(rest)


def _cmd_emergent(rest: list[str]) -> int:
    from bin.cmd_emergent import _cmd_emergent as _impl

    return _impl(rest)


def _cmd_patterns(rest: list[str]) -> int:
    from bin.cmd_patterns import _cmd_patterns as _impl

    return _impl(rest)


# Re-exports for test monkeypatch compat
from bin.cmd_patterns import (  # noqa: E402, F401
    _patterns_deactivate,
    _patterns_list,
    _patterns_reactivate,
    _patterns_show,
)


def _cmd_lesson(rest: list[str]) -> int:
    from bin.cmd_lesson import _cmd_lesson as _impl

    return _impl(rest)


def _cmd_notify(rest: list[str]) -> int:
    from bin.cmd_notify import _cmd_notify as _impl

    return _impl(rest)


# Re-exports for test monkeypatch compat
from bin.cmd_notify import (  # noqa: E402, F401
    _notify_config,
)


def _cmd_create_plugin(rest: list[str]) -> int:
    from bin.cmd_create_plugin import _cmd_create_plugin as _impl

    return _impl(rest)


# ── Main ───────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]

    # --version / -v
    if not args or args[0] in ("--version", "-v"):
        return _cmd_version()

    # --help / -h (without subcommand)
    if args[0] in ("--help", "-h"):
        return _cmd_help()

    cmd = args[0]
    rest = args[1:]

    # help <subcommand>
    if cmd in ("help",):
        sub = rest[0] if rest else None
        return _cmd_help(sub)

    # intercept --help / -h for specific commands
    if _help_requested(rest):
        return _cmd_help(cmd)

    # ── Zero-arg commands ──
    if cmd == "init":
        return _cmd_init(rest)
    if cmd in ("install", "setup"):
        return _cmd_install()
    if cmd == "update":
        return _cmd_update()

    # ── Named-project or path commands (positional) ──
    if cmd in (
        "scan",
        "clean",
        "duster",
        "run",
        "health",
        "report",
        "ci",
        "install-hook",
        "install-cron",
    ):
        if rest and rest[0] == "--repo" and len(rest) > 1:
            name = rest[1]
            passthrough = rest[2:]
        else:
            if not _requires_name(cmd, rest):
                return 1
            name = rest[0]
            passthrough = rest[1:]
        handlers = {
            "scan": _cmd_scan,
            "clean": _cmd_clean,
            "duster": _cmd_duster,
            "run": _cmd_run,
            "health": _cmd_health,
            "report": _cmd_report,
            "ci": _cmd_ci,
            "install-hook": _cmd_install_hook,
            "install-cron": _cmd_install_cron,
        }
        fn = handlers[cmd]
        if cmd == "duster":
            return fn(name)
        return fn(name, passthrough)

    # ── Commands with optional project name ──
    if cmd == "status":
        return _cmd_status(rest)
    if cmd == "doctor":
        return _cmd_doctor(rest)
    if cmd == "dashboard":
        return _cmd_dashboard(rest)
    if cmd == "languages":
        return _cmd_languages(rest)
    if cmd == "repos":
        return _cmd_repos(rest)

    # ── Named-argument commands (--repo, --mode, etc.) ──
    if cmd == "onboard":
        return _cmd_onboard(rest)
    if cmd == "upgrade-config":
        return _cmd_upgrade_config(rest)
    if cmd == "preflight":
        return _cmd_preflight(rest)
    if cmd == "heal":
        return _cmd_heal(rest)
    if cmd == "patterns":
        return _cmd_patterns(rest)
    if cmd == "campaign":
        return _cmd_campaign(rest)
    if cmd == "emergent":
        return _cmd_emergent(rest)
    if cmd == "lesson":
        return _cmd_lesson(rest)
    if cmd == "notify":
        return _cmd_notify(rest)
    if cmd == "create-plugin":
        return _cmd_create_plugin(rest)

    # ── Path command ──
    if cmd == "secrets":
        if not rest:
            print(
                "bluei: 'secrets' needs a directory path. Try 'bluei secrets /path/to/project'.",
                file=sys.stderr,
            )
            return 1
        return _cmd_secrets(rest[0])

    # Unknown command
    print(
        f"bluei: '{cmd}' is not a command. Run 'bluei help' to see what's available.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
