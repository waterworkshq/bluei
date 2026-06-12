"""Inference logic for onboarding — baseline checks, discovery, fix strategy, safety.

Pure functions — no class, no constructor deps.
"""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import (
    LanguageInfo,
    RepoConfig,
    SafetyMode,
    SafetyProfile,
)
from . import detection as _detection

_logger = logging.getLogger(__name__)


def infer_baseline_checks(repo_path: Path, language: LanguageInfo) -> List[List[str]]:
    """Infer sensible baseline validation commands for a repo.

    For mixed-language repos, checks are inferred for ALL detected languages
    (primary + secondary), so that e.g. a Python+TypeScript repo gets both
    pytest and npm test commands.
    """
    commands: List[List[str]] = []

    for lang in [language.name] + language.secondary_languages:
        commands.extend(_infer_baseline_checks_for_language(repo_path, lang))

    deduped: List[List[str]] = []
    seen: set = set()
    for cmd in commands:
        key = tuple(cmd)
        if key not in seen:
            deduped.append(cmd)
            seen.add(key)
    return deduped


def _infer_baseline_checks_for_language(
    repo_path: Path, language_name: str, package_manager: Optional[str] = None
) -> List[List[str]]:
    """Infer baseline checks for a single language (used for primary + secondary)."""
    import json

    commands: List[List[str]] = []
    pkg_mgr = package_manager or _detection.detect_package_manager(
        repo_path, language_name
    )

    if language_name in ("typescript", "javascript"):
        if not (repo_path / "package.json").exists():
            return commands
        try:
            pkg = json.loads((repo_path / "package.json").read_text())
            scripts = pkg.get("scripts", {})
            runner = pkg_mgr or "npm"
            if "test" in scripts:
                commands.append(
                    ["npm", "test"] if runner == "npm" else [runner, "test"]
                )
            if "lint" in scripts:
                commands.append(
                    ["npm", "run", "lint"] if runner == "npm" else [runner, "lint"]
                )
            if "build" in scripts:
                commands.append(
                    ["npm", "run", "build"] if runner == "npm" else [runner, "build"]
                )
            if "typecheck" in scripts:
                commands.append(
                    ["npm", "run", "typecheck"]
                    if runner == "npm"
                    else [runner, "typecheck"]
                )
        except Exception:
            _logger.debug("Failed to detect typecheck command")
        return commands

    if language_name == "python":
        has_tests = (
            (repo_path / "tests").exists()
            or (repo_path / "pytest.ini").exists()
            or (repo_path / "pyproject.toml").exists()
        )
        has_ruff = (
            (repo_path / ".ruff.toml").exists()
            or (repo_path / "ruff.toml").exists()
            or (repo_path / "pyproject.toml").exists()
        )
        has_mypy = (
            (repo_path / "mypy.ini").exists()
            or (repo_path / ".mypy.ini").exists()
            or (repo_path / "pyproject.toml").exists()
        )

        if pkg_mgr == "poetry":
            if has_tests:
                commands.append(["poetry", "run", "pytest", "-q"])
            if has_ruff:
                commands.append(["poetry", "run", "ruff", "check", "."])
            if has_mypy:
                commands.append(["poetry", "run", "mypy", "."])
        elif pkg_mgr == "uv":
            if has_tests:
                commands.append(["uv", "run", "pytest", "-q"])
            if has_ruff:
                commands.append(["uv", "run", "ruff", "check", "."])
            if has_mypy:
                commands.append(["uv", "run", "mypy", "."])
        else:
            if has_tests:
                commands.append(["pytest", "-q"])
            if has_ruff:
                commands.append(["ruff", "check", "."])
            if has_mypy:
                commands.append(["mypy", "."])
        return commands

    if language_name == "go" and (repo_path / "go.mod").exists():
        commands.append(["go", "test", "./..."])
        return commands

    if language_name == "rust" and (repo_path / "Cargo.toml").exists():
        commands.append(["cargo", "test"])
        return commands

    return commands


def infer_discovery_config(repo_path: Path, language: LanguageInfo) -> Dict[str, Any]:
    """Infer discovery mode and execution hints."""
    docker_files = [
        repo_path / "Dockerfile",
        repo_path / "docker-compose.yml",
        repo_path / "docker-compose.yaml",
    ]
    use_docker = any(p.exists() for p in docker_files)
    monorepo = _detection.detect_monorepo(repo_path, language)
    config: Dict[str, Any] = {
        "internal": True,
        "external_script": None,
        "skip_internal": False,
        "use_docker": use_docker,
        "monorepo": monorepo.get("is_monorepo", False),
    }
    if monorepo.get("is_monorepo"):
        config["monorepo_kind"] = monorepo.get("kind")
        config["package_dirs"] = monorepo.get("package_dirs", [])
    if use_docker:
        config["docker_hint"] = "review container/service name before first live run"
    if language.name in ("typescript", "javascript"):
        config["ecosystem"] = "node"
    elif language.name:
        config["ecosystem"] = language.name
    return config


def infer_fix_strategy(repo_path: Path, language: LanguageInfo) -> Dict[str, Any]:
    """Infer local fix backend strategy."""
    available: List[str] = []
    if shutil.which("claude"):
        available.append("claude")
    if shutil.which("opencode"):
        available.append("opencode")
    available.append("deterministic")

    fix_engine = "auto" if len(available) > 1 else available[0]
    claude_template = ""
    opencode_template = ""
    if "claude" in available:
        claude_template = (
            "claude --dangerously-skip-permissions --print "
            '"Read {prompt_file} and apply the minimal safe fix for finding {finding_id}. '
            'Run relevant tests/build checks, keep the diff small, and exit non-zero on failure."'
        )
    if "opencode" in available:
        opencode_template = (
            'opencode run "Read {prompt_file} and apply the minimal safe fix for finding {finding_id}. '
            'Run relevant tests/build checks, keep the diff small, and exit non-zero on failure."'
        )
    return {
        "fix_engine": fix_engine,
        "fallback_engines": available,
        "claude_template": claude_template,
        "opencode_template": opencode_template,
    }


def infer_safety_policy(
    repo_path: Path, mode: str, profile: str, allow_dirty_worktree: bool = False
) -> Dict[str, Any]:
    """Infer and normalize safety policy for a repo."""
    resolved_mode = (
        mode if mode in {m.value for m in SafetyMode} else SafetyMode.OBSERVE.value
    )
    resolved_profile = (
        profile
        if profile in {p.value for p in SafetyProfile}
        else SafetyProfile.CONSERVATIVE.value
    )

    protected = ["main", "master"]
    head_proc = None
    try:
        head_proc = subprocess.run(
            [
                "bash",
                "-lc",
                'git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed "s#^origin/##"',
            ],
            cwd=str(repo_path),
            text=True,
            capture_output=True,
        )
    except Exception:
        head_proc = None
    if head_proc and head_proc.returncode == 0 and head_proc.stdout.strip():
        default_branch = head_proc.stdout.strip()
        if default_branch not in protected:
            protected.insert(0, default_branch)

    notes: List[str] = []
    if resolved_mode == SafetyMode.OBSERVE.value:
        notes.append("Observe mode blocks non-dry-run execution.")
    elif resolved_mode == SafetyMode.ISSUE_ONLY.value:
        notes.append(
            "Issue-only mode allows issue discovery/creation but blocks PR/merge execution."
        )
    elif resolved_mode == SafetyMode.PR.value:
        notes.append(
            "PR mode allows issue and PR execution but blocks merge execution."
        )
    elif resolved_mode == SafetyMode.MERGE.value:
        notes.append(
            "Merge mode allows all phases; auto-merge remains separately controlled."
        )

    if resolved_profile == SafetyProfile.CONSERVATIVE.value:
        notes.append("Conservative profile keeps small diffs and low concurrency caps.")
    elif resolved_profile == SafetyProfile.AGGRESSIVE.value:
        notes.append(
            "Aggressive profile raises caps; review before enabling on important repos."
        )

    return {
        "mode": resolved_mode,
        "profile": resolved_profile,
        "require_clean_worktree": not allow_dirty_worktree,
        "protected_branches": protected,
        "allow_live_on_dirty_tree": allow_dirty_worktree,
        "notes": notes,
    }


def apply_safety_profile(config: RepoConfig) -> RepoConfig:
    """Adjust limits and GitHub settings according to the selected safety profile/mode."""
    profile = config.safety.get("profile", SafetyProfile.CONSERVATIVE.value)
    mode = config.safety.get("mode", SafetyMode.OBSERVE.value)

    if profile == SafetyProfile.CONSERVATIVE.value:
        config.limits.update(
            {
                "open_issues_cap": 10,
                "open_prs_cap": 2,
                "max_prs_per_run": 1,
                "max_issues_per_run": 5,
                "max_files_changed": 3,
                "max_loc_diff": 120,
                "max_fix_attempts": 2,
            }
        )
    elif profile == SafetyProfile.BALANCED.value:
        config.limits.update(
            {
                "open_issues_cap": 20,
                "open_prs_cap": 5,
                "max_prs_per_run": 2,
                "max_issues_per_run": 10,
                "max_files_changed": 5,
                "max_loc_diff": 200,
                "max_fix_attempts": 3,
            }
        )
    elif profile == SafetyProfile.AGGRESSIVE.value:
        config.limits.update(
            {
                "open_issues_cap": 30,
                "open_prs_cap": 8,
                "max_prs_per_run": 3,
                "max_issues_per_run": 15,
                "max_files_changed": 8,
                "max_loc_diff": 350,
                "max_fix_attempts": 4,
            }
        )

    config.github["live_actions"] = mode in {
        SafetyMode.ISSUE_ONLY.value,
        SafetyMode.PR.value,
        SafetyMode.MERGE.value,
    }
    config.github["auto_merge"] = False
    return config
