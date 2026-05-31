#!/usr/bin/env python3
"""Python plugin — wraps ruff (JSON output) plus regex text scanning.

Finds lint issues (broad-except, trailing-whitespace, hardcoded-tmp-path),
type-safety concerns, and TODO/FIXME debt markers.
"""

import logging
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple
import re

import yaml

from bluei.engine.models import Finding
from bluei.app.plugins import ToolWrapperPlugin

_logger = logging.getLogger(__name__)


def stable_finding_id(repo: str, path: str, line: int, rule: str, snippet: str) -> str:
    """Deterministic SHA-256 ID from repo, path, line, rule, and snippet."""
    material = f"{repo}|{path}|{line}|{rule}|{snippet.strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Plugin(ToolWrapperPlugin):
    """Wraps ``ruff`` and supplements with regex-based text scanning.

    Maps ruff diagnostic codes to internal rule names via ``_RUFF_RULE_MAP``.
    Falls back to pure text scanning when ruff is unavailable.
    """

    _RUFF_RULE_MAP = {
        "E501": "trailing-whitespace",
        "E722": "broad-except",
        "B001": "broad-except",
        "S108": "hardcoded-tmp-path",
    }

    @property
    def id(self) -> str:
        return "plugin-python"

    @property
    def name(self) -> str:
        return "Python Plugin"

    @property
    def languages(self) -> List[str]:
        return ["python"]

    @property
    def rules(self) -> List[str]:
        return [
            "discount-math-sign",
            "catalog-query-not-normalized",
            "broad-except",
            "trailing-whitespace",
            "perf-pop-front-loop",
            "orders-tax-truncation",
            "notifications-email-no-trim",
            "notifications-type-guard-missing",
            "inventory-invalid-quantity",
            "hardcoded-tmp-path",
            "debt-todo-marker",
        ]

    def __init__(self):
        manifest_path = Path(__file__).parent / "plugin.yaml"
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
        super().__init__(manifest)

    def parse_output(self, output: str, repo_path: Path) -> List[Finding]:
        """Parse ruff JSON output into Findings.

        Args:
            output: Raw JSON string from ``ruff --output-format json``.
            repo_path: Repository root used for computing relative paths.

        Returns:
            List of Findings, one per ruff diagnostic with a non-empty code.
        """
        from bluei.engine.models import now_iso

        try:
            items = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(items, list):
            return []

        findings = []
        for item in items:
            if not isinstance(item, dict):
                continue
            code = item.get("code", "")
            if not code:
                continue
            rule = self._RUFF_RULE_MAP.get(code, f"ruff-{code}")
            loc = item.get("location", {})
            line = loc.get("row", 1)
            path = item.get("path", "")
            message = item.get("message", "")
            findings.append(
                Finding(
                    finding_id=stable_finding_id(
                        str(repo_path), path, line, rule, message
                    ),
                    repo=str(repo_path),
                    path=path,
                    line=line,
                    rule=rule,
                    snippet=message[:200],
                    confidence=0.88,
                    quick_win=code.startswith("W") or code.startswith("E"),
                    safe_to_autofix=code.startswith("W") or code.startswith("E"),
                    severity="medium",
                    category="lint",
                    discovered_at=now_iso(),
                )
            )
        return findings

    def _text_scan(self, repo_path: Path) -> List[Finding]:
        """Regex-based fallback: detects bare except, trailing whitespace, and TODO markers."""
        from bluei.engine.models import now_iso

        findings = []

        if (
            not (repo_path / "setup.py").exists()
            and not (repo_path / "pyproject.toml").exists()
        ):
            return findings

        for py_file in repo_path.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue

            try:
                content = py_file.read_text()
                lines = content.split("\n")

                rel_path = str(py_file.relative_to(repo_path))

                for i, line in enumerate(lines, 1):
                    if re.search(r"except\s*:", line):
                        findings.append(
                            Finding(
                                finding_id=stable_finding_id(
                                    str(repo_path),
                                    rel_path,
                                    i,
                                    "broad-except",
                                    line.strip(),
                                ),
                                repo=str(repo_path),
                                path=rel_path,
                                line=i,
                                rule="broad-except",
                                snippet=line.strip()[:200],
                                confidence=0.88,
                                quick_win=False,
                                safe_to_autofix=False,
                                severity="medium",
                                category="lint",
                                discovered_at=now_iso(),
                            )
                        )

                    if line.rstrip() != line and line.strip():
                        findings.append(
                            Finding(
                                finding_id=stable_finding_id(
                                    str(repo_path),
                                    rel_path,
                                    i,
                                    "trailing-whitespace",
                                    line.strip(),
                                ),
                                repo=str(repo_path),
                                path=rel_path,
                                line=i,
                                rule="trailing-whitespace",
                                snippet=line.strip()[:200],
                                confidence=0.75,
                                quick_win=True,
                                safe_to_autofix=True,
                                severity="low",
                                category="lint",
                                discovered_at=now_iso(),
                            )
                        )

                    if "TODO" in line or "FIXME" in line:
                        findings.append(
                            Finding(
                                finding_id=stable_finding_id(
                                    str(repo_path),
                                    rel_path,
                                    i,
                                    "debt-todo-marker",
                                    line.strip(),
                                ),
                                repo=str(repo_path),
                                path=rel_path,
                                line=i,
                                rule="debt-todo-marker",
                                snippet=line.strip()[:200],
                                confidence=0.72,
                                quick_win=False,
                                safe_to_autofix=False,
                                severity="low",
                                category="todo/debt",
                                discovered_at=now_iso(),
                            )
                        )

            except Exception:
                continue

        return findings

    def discover(self, repo_path: Path, config: Dict[str, Any]) -> List[Finding]:
        """Run ruff, then merge with text-scan findings (deduped by path/line/rule).

        Args:
            repo_path: Repository root to scan.
            config: Plugin configuration dict (unused by this plugin).

        Returns:
            Merged list of Findings from tool output and text scanning.
        """
        tool_findings = []
        output = self._run_tool(repo_path, extra_args=["."])
        if output is not None:
            tool_findings = self.parse_output(output, repo_path)

        text_findings = self._text_scan(repo_path)

        if not tool_findings:
            return text_findings

        tool_keys = {(f.path, f.line, f.rule) for f in tool_findings}
        for f in text_findings:
            if (f.path, f.line, f.rule) not in tool_keys:
                tool_findings.append(f)

        return tool_findings
