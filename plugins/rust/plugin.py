#!/usr/bin/env python3
"""Rust plugin — wraps ``cargo clippy`` (JSON messages) plus TODO scanning.

Finds clippy lint violations (unwrap-used, clone-on-copy, etc.) and
TODO/FIXME debt markers.
"""

import logging
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

import yaml

from bluei.engine.models import Finding, now_iso
from bluei.app.plugins import ToolWrapperPlugin

_logger = logging.getLogger(__name__)


def stable_finding_id(repo: str, path: str, line: int, rule: str, snippet: str) -> str:
    """Deterministic SHA-256 ID from repo, path, line, rule, and snippet."""
    material = f"{repo}|{path}|{line}|{rule}|{snippet.strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Plugin(ToolWrapperPlugin):
    """Wraps ``cargo clippy``; falls back to TODO scanning when unavailable."""

    @property
    def id(self) -> str:
        return "plugin-rust"

    @property
    def name(self) -> str:
        return "Rust Plugin"

    @property
    def languages(self) -> List[str]:
        return ["rust"]

    @property
    def rules(self) -> List[str]:
        return ["clippy-unwrap-used", "clippy-clone-on-copy", "debt-todo-marker"]

    def __init__(self):
        manifest_path = Path(__file__).parent / "plugin.yaml"
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
        super().__init__(manifest)

    def parse_output(self, output: str, repo_path: Path) -> List[Finding]:
        """Parse ``cargo clippy --message-format=json`` output into Findings.

        Extracts only ``compiler-message`` entries with a clippy diagnostic code.

        Args:
            output: Newline-delimited JSON from cargo clippy.
            repo_path: Repository root used for computing relative paths.

        Returns:
            List of Findings, one per clippy diagnostic.
        """
        findings: List[Finding] = []
        for line in output.strip().splitlines():
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if item.get("reason") != "compiler-message":
                continue
            msg = item.get("message", {})
            code_obj = msg.get("code", {})
            code = code_obj.get("code", "") if isinstance(code_obj, dict) else ""
            if not code:
                continue
            spans = msg.get("spans", [])
            if not spans:
                continue
            span = spans[0]
            file_path = span.get("file_name", "")
            line_num = span.get("line_start", 1)
            rendered = msg.get("rendered", "")
            rule = f"clippy-{code.replace('clippy::', '')}"
            findings.append(
                Finding(
                    finding_id=stable_finding_id(
                        str(repo_path), file_path, line_num, rule, rendered
                    ),
                    repo=str(repo_path),
                    path=file_path,
                    line=line_num,
                    rule=rule,
                    snippet=rendered[:200],
                    confidence=0.85,
                    quick_win=False,
                    safe_to_autofix=False,
                    severity="medium",
                    category="lint",
                    discovered_at=now_iso(),
                )
            )
        return findings

    def _scan_todos(self, repo_path: Path) -> List[Finding]:
        """Scan ``*.rs`` files for TODO/FIXME markers when clippy is unavailable."""
        findings: List[Finding] = []
        for rs_file in repo_path.rglob("*.rs"):
            try:
                rel_path = str(rs_file.relative_to(repo_path))
                for i, line in enumerate(rs_file.read_text().splitlines(), 1):
                    if "TODO" in line or "FIXME" in line:
                        findings.append(
                            Finding(
                                finding_id=stable_finding_id(
                                    str(repo_path),
                                    rel_path,
                                    i,
                                    "debt-todo-marker",
                                    line,
                                ),
                                repo=str(repo_path),
                                path=rel_path,
                                line=i,
                                rule="debt-todo-marker",
                                snippet=line[:200],
                                confidence=0.72,
                                quick_win=False,
                                safe_to_autofix=False,
                                severity="low",
                                category="technical-debt",
                                discovered_at=now_iso(),
                            )
                        )
            except Exception:
                continue
        return findings

    def discover(self, repo_path: Path, config: Dict[str, Any]) -> List[Finding]:
        """Run cargo clippy; fall back to TODO scanning on failure.

        Args:
            repo_path: Repository root to scan.
            config: Plugin configuration dict (unused by this plugin).

        Returns:
            Findings from tool output, or TODO markers if the tool is missing.
        """
        output = self._run_tool(repo_path)
        if output is not None:
            return self.parse_output(output, repo_path)
        return self._scan_todos(repo_path)
