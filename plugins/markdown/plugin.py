#!/usr/bin/env python3
"""Markdown plugin — wraps ``markdownlint`` plus manual doc-quality checks.

Finds style issues (trailing spaces, missing top-level heading) in ``*.md`` files.
"""

import hashlib
import json
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
    """Wraps ``markdownlint``; falls back to regex-based checks when unavailable."""

    @property
    def id(self) -> str:
        return "plugin-markdown"

    @property
    def name(self) -> str:
        return "Markdown Plugin"

    @property
    def languages(self) -> List[str]:
        return ["markdown"]

    @property
    def rules(self) -> List[str]:
        return ["md-MD009", "md-MD041"]

    def __init__(self):
        manifest_path = Path(__file__).parent / "plugin.yaml"
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
        super().__init__(manifest)

    def _run_tool(
        self, repo_path: Path, extra_args: Optional[List[str]] = None
    ) -> Optional[str]:
        """Override base to accept returncode 1 (markdownlint reports findings via exit code)."""
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
        """Parse markdownlint JSON output into Findings.

        Args:
            output: Raw JSON from ``markdownlint --output-format json``.
            repo_path: Repository root used for computing relative paths.

        Returns:
            List of Findings, one per markdownlint diagnostic.
        """
        try:
            items = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return []
        findings = []
        for item in items:
            if not isinstance(item, dict):
                continue
            rule_names = item.get("ruleNames", item.get("rule", []))
            if isinstance(rule_names, list) and rule_names:
                rule_name = rule_names[0]
            else:
                rule_name = str(item.get("rule", "MD000"))
            line = item.get("lineNumber", 1)
            description = item.get("ruleDescription", item.get("message", ""))
            file_path = item.get("fileName", "")
            findings.append(
                Finding(
                    finding_id=stable_finding_id(
                        str(repo_path), file_path, line, rule_name, description
                    ),
                    repo=str(repo_path),
                    path=file_path,
                    line=line,
                    rule=rule_name,
                    snippet=description[:200],
                    confidence=0.85,
                    quick_win=False,
                    safe_to_autofix=False,
                    severity="low",
                    category="docs",
                    discovered_at=now_iso(),
                )
            )
        return findings

    def discover(self, repo_path: Path, config: Dict[str, Any]) -> List[Finding]:
        """Run markdownlint on ``*.md`` files; fall back to regex checks.

        Args:
            repo_path: Repository root to scan.
            config: Plugin configuration dict (unused by this plugin).

        Returns:
            Findings from markdownlint, or regex-based style detections.
        """
        md_files = sorted({*repo_path.glob("*.md"), *repo_path.glob("docs/*.md")})
        if md_files:
            extra_args = [f.relative_to(repo_path).as_posix() for f in md_files]
            output = self._run_tool(repo_path, extra_args=extra_args)
            if output is not None:
                return self.parse_output(output, repo_path)
        findings: List[Finding] = []
        for md_file in md_files or sorted(
            {*repo_path.glob("*.md"), *repo_path.glob("docs/*.md")}
        ):
            rel_path = md_file.relative_to(repo_path).as_posix()
            try:
                lines = md_file.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            if lines and not lines[0].startswith("# "):
                findings.append(
                    _finding(repo_path, rel_path, 1, "md-MD041", lines[0], 0.80, "docs")
                )
            for line_number, line in enumerate(lines, 1):
                if line.rstrip(" ") != line:
                    findings.append(
                        _finding(
                            repo_path,
                            rel_path,
                            line_number,
                            "md-MD009",
                            line,
                            0.90,
                            "lint",
                        )
                    )
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
        severity="low",
        category=category,
        discovered_at=now_iso(),
    )
