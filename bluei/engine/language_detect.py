"""Shared language detection constants and core scoring logic.

Neutral module in the engine layer — importable by app, review, and engine
without violating layer boundaries.  This is the single source of truth for
``LANGUAGE_MARKERS``, ``LANGUAGE_GLOBS``, ``FRAMEWORK_MARKERS``, and the
canonical ``detect_all_languages()`` function.
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

_logger = logging.getLogger(__name__)

LANGUAGE_MARKERS: Dict[str, List[str]] = {
    "python": [
        "setup.py",
        "pyproject.toml",
        "requirements.txt",
        "Pipfile",
        "setup.cfg",
    ],
    "typescript": ["tsconfig.json"],
    "javascript": ["package.json", ".eslintrc", ".prettierrc"],
    "go": ["go.mod", "go.sum"],
    "rust": ["Cargo.toml", "Cargo.lock"],
    "java": ["pom.xml", "build.gradle", "build.gradle.kts"],
    "ruby": ["Gemfile", "Rakefile"],
    "php": ["composer.json"],
    "dockerfile": ["Dockerfile", "Containerfile"],
}

LANGUAGE_GLOBS: Dict[str, List[str]] = {
    "shell": ["*.sh", "scripts/*.sh", "bin/*.sh"],
    "markdown": ["*.md", "docs/*.md"],
}

FRAMEWORK_MARKERS: Dict[str, Dict[str, List[str]]] = {
    "python": {
        "django": ["manage.py", "settings.py", "urls.py"],
        "flask": ["app.py", "flask"],
        "fastapi": ["fastapi", "main.py"],
        "pytest": ["pytest.ini", "conftest.py"],
    },
    "typescript": {
        "react": ["react", "jsx", "tsx"],
        "vue": ["vue"],
        "angular": ["angular", "@angular"],
        "nextjs": ["next"],
        "nestjs": ["@nestjs"],
    },
    "javascript": {
        "react": ["react", "jsx"],
        "vue": ["vue"],
        "express": ["express"],
        "nextjs": ["next"],
    },
    "go": {
        "gin": ["gin-gonic"],
        "echo": ["labstack/echo"],
    },
    "rust": {
        "actix": ["actix"],
        "rocket": ["rocket"],
    },
}


def detect_all_languages(
    repo_path: Path,
    language_markers: Optional[Dict[str, List[str]]] = None,
    framework_markers: Optional[Dict[str, Dict[str, List[str]]]] = None,
) -> List[tuple[str, int]]:
    """Detect all languages present in a repository with scores.

    Returns languages sorted by score descending. Useful for mixed-language
    repos (e.g. Zulip-style: Python backend + TypeScript frontend).
    A language must have a minimum score to be included (avoids false positives
    from lock files or generic configs).
    """
    lang_markers = language_markers or LANGUAGE_MARKERS
    fw_markers = framework_markers or FRAMEWORK_MARKERS
    repo_path = Path(repo_path).resolve()
    scores: Dict[str, int] = {}

    for language, markers in lang_markers.items():
        score = 0
        for marker in markers:
            if (repo_path / marker).exists():
                score += 1
        scores[language] = score

    package_json = repo_path / "package.json"
    if package_json.exists():
        try:
            with open(package_json) as f:
                pkg = json.load(f)

            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}

            if "typescript" in deps or any("@types/" in d for d in deps):
                scores["typescript"] = scores.get("typescript", 0) + 5

            for fw, markers in fw_markers.get("typescript", {}).items():
                for marker in markers:
                    if marker in deps:
                        scores["typescript"] = scores.get("typescript", 0) + 2
        except Exception:
            _logger.debug("Failed to scan typescript dependencies")

    requirements = repo_path / "requirements.txt"
    if requirements.exists():
        try:
            content = requirements.read_text().lower()
            for fw, markers in fw_markers.get("python", {}).items():
                for marker in markers:
                    if marker in content:
                        scores["python"] = scores.get("python", 0) + 2
        except Exception:
            _logger.debug("Failed to scan python requirements")

    if all(v == 0 for v in scores.values()):
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
        except Exception:
            _logger.debug("Failed to scan file extensions")

    MIN_SCORE = 1
    return sorted(
        [(lang, s) for lang, s in scores.items() if s >= MIN_SCORE],
        key=lambda x: x[1],
        reverse=True,
    )
