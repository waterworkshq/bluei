#!/usr/bin/env python3
"""Dockerfile plugin — wraps ``hadolint`` plus manual Dockerfile anti-pattern checks.

Finds Docker best-practice violations (untagged images, root user, unpinned packages).
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
    """Wraps ``hadolint``; falls back to regex-based Dockerfile checks when unavailable."""

    @property
    def id(self) -> str:
        return "plugin-dockerfile"

    @property
    def name(self) -> str:
        return "Dockerfile Plugin"

    @property
    def languages(self) -> List[str]:
        return ["dockerfile"]

    @property
    def rules(self) -> List[str]:
        return ["docker-DL3006", "docker-DL3002", "docker-DL4006"]

    def __init__(self):
        manifest_path = Path(__file__).parent / "plugin.yaml"
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
        super().__init__(manifest)

    def _run_tool(
        self, repo_path: Path, extra_args: Optional[List[str]] = None
    ) -> Optional[str]:
        """Override base to accept returncode 1 (hadolint reports findings via exit code)."""
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
        """Parse hadolint JSON output into Findings.

        Args:
            output: Raw JSON from ``hadolint --format json``.
            repo_path: Repository root (paths are always ``Dockerfile``).

        Returns:
            List of Findings, one per hadolint diagnostic.
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
            message = item.get("message", "")
            level = item.get("level", "warning")
            rule = f"docker-{code}" if code else "docker-DL0000"
            findings.append(
                Finding(
                    finding_id=stable_finding_id(
                        str(repo_path), "Dockerfile", line, rule, message
                    ),
                    repo=str(repo_path),
                    path="Dockerfile",
                    line=line,
                    rule=rule,
                    snippet=message[:200],
                    confidence=0.85 if level == "error" else 0.78,
                    quick_win=False,
                    safe_to_autofix=False,
                    severity="medium",
                    category="security",
                    discovered_at=now_iso(),
                )
            )
        return findings

    def discover(self, repo_path: Path, config: Dict[str, Any]) -> List[Finding]:
        """Run hadolint on Dockerfile/Containerfile; fall back to regex checks.

        Args:
            repo_path: Repository root to scan.
            config: Plugin configuration dict (unused by this plugin).

        Returns:
            Findings from hadolint, or regex-based anti-pattern detections.
        """
        docker_files = [
            f
            for f in [repo_path / "Dockerfile", repo_path / "Containerfile"]
            if f.exists()
        ]
        if docker_files:
            extra_args = [f.relative_to(repo_path).as_posix() for f in docker_files]
            output = self._run_tool(repo_path, extra_args=extra_args)
            if output is not None:
                return self.parse_output(output, repo_path)
        findings: List[Finding] = []
        for docker_file in [repo_path / "Dockerfile", repo_path / "Containerfile"]:
            if not docker_file.exists():
                continue
            rel_path = docker_file.relative_to(repo_path).as_posix()
            for line_number, line in enumerate(
                docker_file.read_text(encoding="utf-8").splitlines(), 1
            ):
                stripped = line.strip()
                if stripped.startswith("FROM ") and ":" not in stripped.split()[1]:
                    findings.append(
                        _finding(
                            repo_path,
                            rel_path,
                            line_number,
                            "docker-DL3006",
                            line,
                            0.85,
                            "security",
                        )
                    )
                if stripped == "USER root":
                    findings.append(
                        _finding(
                            repo_path,
                            rel_path,
                            line_number,
                            "docker-DL3002",
                            line,
                            0.82,
                            "security",
                        )
                    )
                if "apt-get install" in stripped and "=" not in stripped:
                    findings.append(
                        _finding(
                            repo_path,
                            rel_path,
                            line_number,
                            "docker-DL3008",
                            line,
                            0.80,
                            "security",
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
        severity="medium",
        category=category,
        discovered_at=now_iso(),
    )
