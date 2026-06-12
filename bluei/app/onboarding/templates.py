"""Template and rule pack selection for onboarding.

Pure functions — no class, no constructor deps.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from ..models import LanguageInfo
from . import detection as _detection

_logger = logging.getLogger(__name__)


def select_template(
    repo_path: Path, language: LanguageInfo, framework: Optional[str]
) -> Optional[str]:
    """Select a repo template when confidence is high."""
    if language.name in {"typescript", "javascript"}:
        package_json = repo_path / "package.json"
        scripts: dict = {}
        deps: dict = {}
        monorepo = _detection.detect_monorepo(repo_path, language)
        if package_json.exists():
            try:
                pkg = json.loads(package_json.read_text())
                scripts = pkg.get("scripts", {})
                deps = {
                    **pkg.get("dependencies", {}),
                    **pkg.get("devDependencies", {}),
                }
            except Exception:
                _logger.debug("Failed to read package.json scripts/deps")

        if monorepo.get("is_monorepo"):
            if {"build", "test"} & set(scripts.keys()):
                return "node-workspace-root"
            return "node-workspace-root"
        if framework == "nextjs" or "next" in deps:
            return "next-app"
        if framework == "react" or "react" in deps:
            return "react-app"
        if framework in {"express", "nestjs"} or "express" in deps or "@nestjs" in deps:
            return "node-api"
        if {"build", "test"} & set(scripts.keys()):
            return "node-library"
        return "node-library" if package_json.exists() else None

    if language.name == "python":
        if framework == "django":
            return "django-app"
        if framework == "fastapi":
            return "fastapi-app"
        if framework in {"flask"}:
            return "python-api"
        if (repo_path / "manage.py").exists():
            return "django-app"
        if (repo_path / "app.py").exists() or (repo_path / "main.py").exists():
            return "python-api"
        if (repo_path / "setup.py").exists() or (repo_path / "pyproject.toml").exists():
            return "python-library"
        return "python-library"

    if language.name == "go":
        return "go-service"

    if language.name == "rust":
        return "rust-crate"

    return None


def select_rule_pack(template_name: Optional[str]) -> Optional[str]:
    _MAPPING = {
        "node-library": "node-library-safe",
        "node-api": "node-backend-safe",
        "next-app": "node-frontend-safe",
        "react-app": "node-frontend-safe",
        "node-workspace-root": "node-backend-safe",
        "python-library": "python-library-safe",
        "python-api": "python-api-safe",
        "django-app": "python-api-safe",
        "fastapi-app": "python-api-safe",
        "go-service": "go-safe",
        "rust-crate": "rust-safe",
    }
    return _MAPPING.get(template_name or "")
