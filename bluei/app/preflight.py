"""Preflight — assess a project's readiness before onboarding or running.

Checks:
1. Language detection
2. Plugin tool availability (shutil.which for each declared tool)
3. Baseline validation commands (inferred + dry-run executed)

Returns a structured PreflightResult that the CLI command, the onboarding
gate, and tests can all consume.
"""

from __future__ import annotations

import shutil
import subprocess
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.app.onboarding.detection import detect_language_info
from bluei.app.onboarding.inference import infer_baseline_checks
from bluei.engine.plugin_loader import discover_plugins

_logger = logging.getLogger(__name__)

PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent / "plugins"


@dataclass
class ToolCheck:
    """Result of checking whether a single tool is installed."""

    tool: str
    found: bool
    path: Optional[str] = None


@dataclass
class ValidationCheck:
    """Result of running a single baseline validation command."""

    command: List[str]
    passed: bool
    returncode: int = -1
    stderr_snippet: str = ""


@dataclass
class PreflightResult:
    """Full preflight assessment for a repository."""

    repo_path: str
    language: str = "unknown"
    secondary_languages: List[str] = field(default_factory=list)
    plugin_id: Optional[str] = None
    tool_checks: List[ToolCheck] = field(default_factory=list)
    validation_checks: List[ValidationCheck] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """True if all required tools are found AND all validation passes."""
        tools_ok = all(tc.found for tc in self.tool_checks)
        validation_ok = all(vc.passed for vc in self.validation_checks)
        return tools_ok and validation_ok and not self.errors

    @property
    def missing_tools(self) -> List[str]:
        return [tc.tool for tc in self.tool_checks if not tc.found]

    @property
    def failed_validations(self) -> List[ValidationCheck]:
        return [vc for vc in self.validation_checks if not vc.passed]


def _find_plugin_for_language(language: str) -> Optional[Dict[str, Any]]:
    """Find the plugin manifest that declares the given language."""
    manifests = discover_plugins(PLUGINS_DIR)
    for manifest in manifests.values():
        langs = manifest.get("languages", [])
        if language in langs:
            return manifest
    return None


def run_preflight(
    repo_path: Path,
    *,
    run_validation: bool = True,
    plugins_dir: Optional[Path] = None,
) -> PreflightResult:
    """Run preflight checks on a repository.

    Args:
        repo_path: Path to the repository to assess.
        run_validation: If True, execute inferred baseline commands.
                       If False, skip validation dry-run (tool checks only).
        plugins_dir: Override plugins directory (for testing).

    Returns:
        PreflightResult with all check outcomes.
    """
    repo_path = Path(repo_path).resolve()
    result = PreflightResult(repo_path=str(repo_path))

    if not repo_path.is_dir():
        result.errors.append(f"Path is not a directory: {repo_path}")
        return result

    # ── 1. Language detection ──
    try:
        lang_info = detect_language_info(repo_path)
        result.language = lang_info.name
        result.secondary_languages = list(lang_info.secondary_languages)
    except Exception as exc:
        result.errors.append(f"Language detection failed: {exc}")
        return result

    # ── 2. Plugin tool verification (D3) ──
    plugins_to_check = [result.language] + result.secondary_languages
    checked_plugins = set()

    pd = plugins_dir or PLUGINS_DIR
    manifests = discover_plugins(pd)

    for lang in plugins_to_check:
        for manifest in manifests.values():
            manifest_langs = manifest.get("languages", [])
            if lang not in manifest_langs:
                continue
            plugin_id = manifest.get("id", "")
            if plugin_id in checked_plugins:
                continue
            checked_plugins.add(plugin_id)

            if not result.plugin_id:
                result.plugin_id = plugin_id

            tool = manifest.get("discovery", {}).get("tool", "")
            if tool:
                tool_path = shutil.which(tool)
                result.tool_checks.append(
                    ToolCheck(
                        tool=tool,
                        found=tool_path is not None,
                        path=tool_path,
                    )
                )

    if not result.tool_checks:
        result.errors.append(f"No plugin tool found for language: {result.language}")

    # ── 3. Validation dry-run (D4) ──
    if run_validation:
        try:
            baseline_checks = infer_baseline_checks(repo_path, lang_info)
        except Exception as exc:
            _logger.debug("Baseline inference failed: %s", exc)
            baseline_checks = []

        for cmd in baseline_checks:
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                result.validation_checks.append(
                    ValidationCheck(
                        command=cmd,
                        passed=proc.returncode == 0,
                        returncode=proc.returncode,
                        stderr_snippet=(proc.stderr or "")[:200],
                    )
                )
            except FileNotFoundError:
                result.validation_checks.append(
                    ValidationCheck(
                        command=cmd,
                        passed=False,
                        stderr_snippet=f"Command not found: {cmd[0]}",
                    )
                )
            except subprocess.TimeoutExpired:
                result.validation_checks.append(
                    ValidationCheck(
                        command=cmd,
                        passed=False,
                        stderr_snippet="Timed out after 60s",
                    )
                )
            except Exception as exc:
                result.validation_checks.append(
                    ValidationCheck(
                        command=cmd,
                        passed=False,
                        stderr_snippet=str(exc)[:200],
                    )
                )

    return result
