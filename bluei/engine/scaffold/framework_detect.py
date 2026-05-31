"""scaffold.framework_detect — Detect test framework from project files."""

from __future__ import annotations

import logging
import json
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)


def detect_test_framework(worktree: Path, language: str = "python") -> str:
    if language in ("javascript", "typescript"):
        return _detect_js_framework(worktree)
    if language == "go":
        return "go_test"
    if language == "rust":
        return "cargo_test"
    return _detect_python_framework(worktree)


def _detect_python_framework(worktree: Path) -> str:
    if (worktree / "pytest.ini").exists():
        return "pytest"
    if (worktree / "pyproject.toml").exists():
        content = (worktree / "pyproject.toml").read_text(encoding="utf-8")
        if "[tool.pytest" in content or "pytest" in content:
            return "pytest"
    if (worktree / "setup.cfg").exists():
        content = (worktree / "setup.cfg").read_text(encoding="utf-8")
        if "[tool:pytest]" in content:
            return "pytest"
    if (worktree / "conftest.py").exists():
        return "pytest"
    if (worktree / "unittest.cfg").exists():
        return "unittest"
    return "pytest"


def _detect_js_framework(worktree: Path) -> str:
    pkg_json = worktree / "package.json"
    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            if "vitest" in deps:
                return "vitest"
            if "jest" in deps:
                return "jest"
            if "mocha" in deps:
                return "mocha"
        except (json.JSONDecodeError, OSError):
            _logger.debug("Failed to read package.json for test runner")

    if (worktree / "jest.config.js").exists() or (worktree / "jest.config.ts").exists():
        return "jest"
    if (worktree / "vitest.config.ts").exists() or (worktree / "vitest.config.js").exists():
        return "vitest"

    return "jest"


def detect_install_commands(worktree: Path) -> str:
    commands = []
    if (worktree / "pyproject.toml").exists():
        content = (worktree / "pyproject.toml").read_text(encoding="utf-8")
        if "pytest" in content:
            commands.append("pip install pytest")
    elif (worktree / "requirements-dev.txt").exists():
        commands.append("pip install -r requirements-dev.txt")
    elif (worktree / "setup.py").exists():
        commands.append("pip install -e .")
    if not commands:
        commands.append("pip install pytest")
    return "\n".join(commands)


def detect_test_commands(worktree: Path) -> str:
    commands = []
    if (worktree / "pytest.ini").exists() or (worktree / "conftest.py").exists():
        commands.append("pytest -q")
    elif (worktree / "pyproject.toml").exists():
        commands.append("pytest -q")
    else:
        commands.append("pytest -q")

    if (worktree / "Makefile").exists():
        content = (worktree / "Makefile").read_text(encoding="utf-8")
        if "test" in content:
            commands.append("make test")

    return "\n".join(commands)
