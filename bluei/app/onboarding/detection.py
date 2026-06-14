"""Language, framework, and toolchain detection for repository onboarding.

Pure functions — no class, no constructor deps, no side effects.
"""

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import LanguageInfo

# Single source of truth lives in the neutral engine-layer module.
from bluei.engine.language_detect import (
    LANGUAGE_MARKERS as LANGUAGE_MARKERS,
    FRAMEWORK_MARKERS as FRAMEWORK_MARKERS,
    detect_all_languages as detect_all_languages,
)

_logger = logging.getLogger(__name__)


def detect_language_info(repo_path: Path) -> LanguageInfo:
    """Detect the primary language of a repository with full metadata.

    For mixed-language repos, the secondary languages are also recorded
    on the returned LanguageInfo so that baseline checks and plugins
    can be applied appropriately.
    """
    ranked = detect_all_languages(repo_path)
    if not ranked:
        return LanguageInfo(name="unknown")

    best_language = ranked[0][0]
    secondary = [lang for lang, _ in ranked[1:] if is_actionable_language(lang)]

    version = get_language_version(repo_path, best_language)

    return LanguageInfo(
        name=best_language,
        version=version,
        package_manager=detect_package_manager(repo_path, best_language),
        build_tool=detect_build_tool(repo_path, best_language),
        secondary_languages=secondary,
    )


def detect_git_remote(repo_path: Path) -> Dict[str, str]:
    """Detect git remote information for a repository.

    Runs ``git remote get-url origin``, parses owner/name, and identifies
    the hosting platform (GitHub, GitLab, or other).

    Returns a dict with keys: 'url', 'host', 'owner', 'name', 'platform', 'detected'.
    All values are empty strings when no remote is found or on error.
    """
    result: Dict[str, str] = {
        "url": "",
        "host": "",
        "owner": "",
        "name": "",
        "platform": "",
        "detected": "false",
    }
    repo_path = Path(repo_path).resolve()
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return result
        url = proc.stdout.strip()
        result["url"] = url
        result["detected"] = "true"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return result

    match = re.match(r"(?:https?://|git@)([^:/]+)[:/]([^/]+)/([^/].*?)(?:\.git)?$", url)
    if match:
        result["host"] = match.group(1)
        result["owner"] = match.group(2)
        result["name"] = match.group(3).replace(".git", "")

        host = result["host"].lower()
        if "github" in host:
            result["platform"] = "github"
        elif "gitlab" in host:
            result["platform"] = "gitlab"
        else:
            result["platform"] = "other"

    return result


def detect_package_manager(repo_path: Path, language: str) -> Optional[str]:
    """Infer the package manager used by the repository."""
    if language in ("typescript", "javascript"):
        if (repo_path / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (repo_path / "yarn.lock").exists():
            return "yarn"
        if (repo_path / "bun.lock").exists() or (repo_path / "bun.lockb").exists():
            return "bun"
        if (repo_path / "package-lock.json").exists() or (
            repo_path / "package.json"
        ).exists():
            return "npm"
    if language == "python":
        if (repo_path / "poetry.lock").exists():
            return "poetry"
        if (repo_path / "uv.lock").exists():
            return "uv"
        if (repo_path / "Pipfile").exists():
            return "pipenv"
        if (repo_path / "requirements.txt").exists() or (
            repo_path / "pyproject.toml"
        ).exists():
            return "pip"
    return None


def detect_build_tool(repo_path: Path, language: str) -> Optional[str]:
    """Infer the primary build/test tool."""
    if (
        language in ("typescript", "javascript")
        and (repo_path / "package.json").exists()
    ):
        return "package-scripts"
    if language == "python":
        if (repo_path / "pyproject.toml").exists():
            return "pyproject"
        if (repo_path / "setup.py").exists():
            return "setuptools"
    if language == "go" and (repo_path / "go.mod").exists():
        return "go"
    if language == "rust" and (repo_path / "Cargo.toml").exists():
        return "cargo"
    return None


def detect_monorepo(repo_path: Path, language_info: LanguageInfo) -> Dict[str, Any]:
    """Detect common monorepo/workspace patterns."""
    result: Dict[str, Any] = {
        "is_monorepo": False,
        "kind": None,
        "package_dirs": [],
    }
    if language_info.name not in {"typescript", "javascript"}:
        return result

    package_json = repo_path / "package.json"
    if not package_json.exists():
        return result
    try:
        pkg = json.loads(package_json.read_text())
    except Exception:
        return result

    workspaces = pkg.get("workspaces")
    if workspaces:
        result["is_monorepo"] = True
        result["kind"] = "workspaces"
        if isinstance(workspaces, list):
            result["package_dirs"] = workspaces
        elif isinstance(workspaces, dict) and isinstance(
            workspaces.get("packages"), list
        ):
            result["package_dirs"] = workspaces["packages"]
        return result

    if (repo_path / "pnpm-workspace.yaml").exists():
        result["is_monorepo"] = True
        result["kind"] = "pnpm-workspace"
        return result

    return result


def get_language_version(repo_path: Path, language: str) -> Optional[str]:
    """Get language version from version files."""
    version_files = {
        "python": [".python-version", "runtime.txt"],
        "typescript": [".nvmrc", ".node-version"],
        "javascript": [".nvmrc", ".node-version"],
        "go": ["go.mod"],
        "rust": ["Cargo.toml"],
    }

    for vf in version_files.get(language, []):
        version_file = repo_path / vf
        if version_file.exists():
            try:
                content = version_file.read_text()
                if language == "python":
                    return content.strip().split()[0] if content.strip() else None
                elif language in ("typescript", "javascript"):
                    return content.strip()
                elif language == "go":
                    for line in content.split("\n"):
                        if line.startswith("go "):
                            return line.split()[1]
            except Exception:
                _logger.debug("Failed to extract language version")

    return None


def is_actionable_language(language: str) -> bool:
    """Return True if a detected language has actionable plugin coverage."""
    return language in {"python", "typescript", "javascript", "go", "rust"}


def detect_language_simple(repo_path: Path) -> str:
    """Detect the primary language of a repository by scanning file extensions.

    Returns one of: 'python', 'typescript', 'javascript', or 'unknown'.
    This is a lightweight standalone function useful for quick auto-detection.
    """
    repo_path = Path(repo_path).resolve()
    scores: Dict[str, int] = {}

    try:
        for entry in repo_path.iterdir():
            if entry.is_file():
                ext = entry.suffix.lower()
                if ext == ".py":
                    scores["python"] = scores.get("python", 0) + 1
                elif ext in (".ts", ".tsx"):
                    scores["typescript"] = scores.get("typescript", 0) + 1
                elif ext in (".js", ".jsx"):
                    scores["javascript"] = scores.get("javascript", 0) + 1
    except PermissionError:
        _logger.debug("Permission denied scanning for languages")

    # Boost scores using the shared LANGUAGE_MARKERS (single source of truth)
    _SIMPLE_LANGS = {"python", "typescript", "javascript"}
    for lang, markers in LANGUAGE_MARKERS.items():
        if lang not in _SIMPLE_LANGS:
            continue
        for marker in markers:
            if (repo_path / marker).exists():
                scores[lang] = scores.get(lang, 0) + 5
                break

    # package.json without tsconfig → likely JavaScript, not TypeScript
    if (repo_path / "package.json").exists() and not (
        repo_path / "tsconfig.json"
    ).exists():
        scores["javascript"] = scores.get("javascript", 0) + 3

    if not scores:
        return "unknown"

    return max(scores, key=scores.get)


def detect_frameworks(repo_path: Path, language: str) -> List[str]:
    """Detect frameworks used in a repo based on config files and imports.

    Returns a list of framework identifiers, e.g. ['django', 'celery'].
    Scans up to 20 .py files for import detection (stops early if all found).
    """
    repo_path = Path(repo_path).resolve()
    frameworks: List[str] = []

    if language == "python":
        if (repo_path / "manage.py").exists() or any(repo_path.glob("**/settings.py")):
            frameworks.append("django")
        if any(repo_path.glob("**/conftest.py")) or (repo_path / "pytest.ini").exists():
            pass

        py_files = list(repo_path.rglob("*.py"))[:20]
        fastapi_found = False
        flask_found = False
        celery_found = False
        for py_file in py_files:
            if fastapi_found and flask_found and celery_found:
                break
            try:
                text = py_file.read_text(errors="ignore")[:5000]
                if not fastapi_found and (
                    "from fastapi" in text or "import fastapi" in text
                ):
                    frameworks.append("fastapi")
                    fastapi_found = True
                if not flask_found and ("from flask" in text or "import flask" in text):
                    frameworks.append("flask")
                    flask_found = True
                if not celery_found and (
                    "from celery" in text or "import celery" in text
                ):
                    frameworks.append("celery")
                    celery_found = True
            except OSError:
                continue

        req_file = repo_path / "requirements.txt"
        if req_file.exists() and "celery" not in frameworks:
            try:
                content = req_file.read_text().lower()
                if "celery" in content:
                    frameworks.append("celery")
            except OSError:
                _logger.debug("Failed to read requirements.txt for celery")

    elif language in ("typescript", "javascript"):
        pkg_path = repo_path / "package.json"
        if pkg_path.exists():
            try:
                pkg = json.loads(pkg_path.read_text())
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if "next" in deps:
                    frameworks.append("nextjs")
                if "express" in deps:
                    frameworks.append("express")
                if "react" in deps:
                    frameworks.append("react")
                if "@nestjs/core" in deps:
                    frameworks.append("nestjs")
                if "vitest" in deps:
                    frameworks.append("vitest")
            except (json.JSONDecodeError, OSError):
                _logger.debug("Failed to read package.json for frameworks")

    return frameworks
