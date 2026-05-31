#!/usr/bin/env python3
"""Shell plugin — wraps ``shellcheck`` plus manual bash anti-pattern checks.

Finds common shell scripting bugs (unquoted variables, unsafe cd, deprecated
``$[`` arithmetic).
"""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from bluei.engine.models import Finding, now_iso
from bluei.app.plugins import ToolWrapperPlugin


def stable_finding_id(repo: str, path: str, line: int, rule: str, snippet: str) -> str:
    """Deterministic SHA-256 ID from repo, path, line, rule, and snippet."""
    material = f"{repo}|{path}|{line}|{rule}|{snippet.strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Plugin(ToolWrapperPlugin):
    """Wraps ``shellcheck``; falls back to regex-based checks when unavailable."""

    @property
    def id(self) -> str:
        return "plugin-shell"

    @property
    def name(self) -> str:
        return "Shell Plugin"

    @property
    def languages(self) -> List[str]:
        return ["shell"]

    @property
    def rules(self) -> List[str]:
        return ["shell-SC2086", "shell-SC2164", "shell-SC2004"]

    def __init__(self):
        manifest_path = Path(__file__).parent / "plugin.yaml"
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
        super().__init__(manifest)

    def _run_tool(
        self, repo_path: Path, extra_args: Optional[List[str]] = None
    ) -> Optional[str]:
        """Override base to accept returncode 1 (shellcheck reports findings via exit code)."""
        tool = self.tool_name
        if not tool or not shutil.which(tool):
            return None
        cmd = [tool] + self.tool_args + (extra_args or [])
        try:
            result = subprocess.run(
                cmd,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode <= 1 and result.stdout:
                return result.stdout
            return None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def parse_output(self, output: str, repo_path: Path) -> List[Finding]:
        """Parse shellcheck JSON output into Findings.

        Args:
            output: Raw JSON from ``shellcheck --format json``.
            repo_path: Repository root used for computing relative paths.

        Returns:
            List of Findings, one per shellcheck diagnostic.
        """
        try:
            items = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return []
        findings = []
        for item in items:
            if not isinstance(item, dict):
                continue
            code = item.get("code", "")
            line = item.get("line", 1)
            file_path = item.get("file", "")
            message = item.get("message", "")
            level = item.get("level", "warning")
            rule = f"shell-SC{code}"
            findings.append(
                Finding(
                    finding_id=stable_finding_id(
                        str(repo_path), file_path, line, rule, message
                    ),
                    repo=str(repo_path),
                    path=file_path,
                    line=line,
                    rule=rule,
                    snippet=message[:200],
                    confidence=0.88 if level == "error" else 0.80,
                    quick_win=False,
                    safe_to_autofix=False,
                    severity="medium",
                    category="bug" if level == "error" else "lint",
                    discovered_at=now_iso(),
                )
            )
        return findings

    def discover(self, repo_path: Path, config: Dict[str, Any]) -> List[Finding]:
        """Run shellcheck on ``*.sh`` files; fall back to regex checks.

        Args:
            repo_path: Repository root to scan.
            config: Plugin configuration dict (unused by this plugin).

        Returns:
            Findings from shellcheck, or regex-based anti-pattern detections.
        """
        shell_files = sorted(
            {
                *repo_path.glob("*.sh"),
                *repo_path.glob("scripts/*.sh"),
                *repo_path.glob("bin/*.sh"),
            }
        )
        if shell_files:
            extra_args = [f.relative_to(repo_path).as_posix() for f in shell_files]
            output = self._run_tool(repo_path, extra_args=extra_args)
            if output is not None:
                return self.parse_output(output, repo_path)
        findings: List[Finding] = []
        for shell_file in shell_files or sorted(
            {
                *repo_path.glob("*.sh"),
                *repo_path.glob("scripts/*.sh"),
                *repo_path.glob("bin/*.sh"),
            }
        ):
            try:
                rel_path = shell_file.relative_to(repo_path).as_posix()
                for line_number, line in enumerate(
                    shell_file.read_text(encoding="utf-8").splitlines(), 1
                ):
                    if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*", line) and '"' not in line:
                        findings.append(
                            _finding(
                                repo_path,
                                rel_path,
                                line_number,
                                "shell-SC2086",
                                line,
                                0.88,
                                "bug",
                            )
                        )
                    if (
                        line.strip().startswith("cd ")
                        and "||" not in line
                        and "&&" not in line
                    ):
                        findings.append(
                            _finding(
                                repo_path,
                                rel_path,
                                line_number,
                                "shell-SC2164",
                                line,
                                0.85,
                                "bug",
                            )
                        )
                    if "$[" in line:
                        findings.append(
                            _finding(
                                repo_path,
                                rel_path,
                                line_number,
                                "shell-SC2004",
                                line,
                                0.90,
                                "lint",
                            )
                        )
            except OSError:
                continue
        return findings


def _finding(
    repo_path: Path,
    path: str,
    line: int,
    rule: str,
    snippet: str,
    confidence: float,
    category: str,
) -> Finding:
    return Finding(
        finding_id=stable_finding_id(str(repo_path), path, line, rule, snippet),
        repo=str(repo_path),
        path=path,
        line=line,
        rule=rule,
        snippet=snippet[:200],
        confidence=confidence,
        quick_win=False,
        safe_to_autofix=False,
        severity="medium",
        category=category,
        discovered_at=now_iso(),
    )
