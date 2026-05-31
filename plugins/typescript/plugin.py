#!/usr/bin/env python3
"""TypeScript/JavaScript plugin — wraps eslint/xo/tsc plus regex text scanning.

Finds lint issues (max-lines, complexity, warning-comments), type-safety
concerns (explicit-any, missing-return), and debug leftovers (console.log).
"""

import logging
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List
import re

_logger = logging.getLogger(__name__)

import yaml

from bluei.engine.models import Finding
from bluei.app.plugins import ToolWrapperPlugin


def stable_finding_id(repo: str, path: str, line: int, rule: str, snippet: str) -> str:
    """Deterministic SHA-256 ID from repo, path, line, rule, and snippet."""
    material = f"{repo}|{path}|{line}|{rule}|{snippet.strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Plugin(ToolWrapperPlugin):
    """Wraps eslint/xo/tsc and supplements with regex-based text scanning.

    Maps eslint rule IDs to internal names via ``_XO_RULE_MAP``. Falls back
    to pure text scanning when no linter is available.
    """

    _XO_RULE_MAP = {
        "max-lines": "xo-max-lines",
        "complexity": "xo-complexity",
        "no-warning-comments": "xo-no-warning-comments",
        "@typescript-eslint/no-explicit-any": "type-explicit-any",
        "@typescript-eslint/explicit-module-boundary-types": "type-missing-return",
    }

    @property
    def id(self) -> str:
        return "plugin-typescript"

    @property
    def name(self) -> str:
        return "TypeScript/JavaScript Plugin"

    @property
    def languages(self) -> List[str]:
        return ["typescript", "javascript"]

    @property
    def rules(self) -> List[str]:
        return [
            "xo-max-lines",
            "xo-complexity",
            "xo-no-warning-comments",
            "type-explicit-any",
            "type-missing-return",
            "type-missing-param",
            "type-untyped-import",
            "test-coverage-branch",
            "test-coverage-function",
            "test-coverage-line",
        ]

    def __init__(self):
        manifest_path = Path(__file__).parent / "plugin.yaml"
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
        super().__init__(manifest)

    def _is_typescript(self, repo_path: Path) -> bool:
        """Return True if the repo uses TypeScript (tsconfig.json or typescript dep)."""
        if (repo_path / "tsconfig.json").exists():
            return True

        package_json = repo_path / "package.json"
        if package_json.exists():
            try:
                with open(package_json) as f:
                    pkg = json.load(f)
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if "typescript" in deps:
                    return True
            except Exception:
                _logger.debug("Failed to read package.json for TS detection")

        return False

    def parse_output(self, output: str, repo_path: Path) -> List[Finding]:
        """Parse eslint/xo JSON output (array of file-result objects) into Findings.

        Args:
            output: Raw JSON from ``eslint --format json`` or equivalent.
            repo_path: Repository root used for computing relative paths.

        Returns:
            List of Findings, one per diagnostic message with a non-empty ruleId.
        """
        from bluei.engine.models import now_iso

        try:
            items = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(items, list):
            return []

        findings = []
        for file_result in items:
            if not isinstance(file_result, dict):
                continue
            file_path = file_result.get("filePath", "")
            try:
                rel_path = (
                    str(Path(file_path).relative_to(repo_path)) if file_path else ""
                )
            except ValueError:
                rel_path = file_path
            for msg in file_result.get("messages", []):
                rule_id = msg.get("ruleId", "")
                if not rule_id:
                    continue
                rule = self._XO_RULE_MAP.get(rule_id, rule_id)
                line = msg.get("line", 1)
                message = msg.get("message", "")
                findings.append(
                    Finding(
                        finding_id=stable_finding_id(
                            str(repo_path), rel_path, line, rule, message
                        ),
                        repo=str(repo_path),
                        path=rel_path,
                        line=line,
                        rule=rule,
                        snippet=message[:200],
                        confidence=0.85,
                        quick_win=True,
                        safe_to_autofix=True,
                        severity="medium",
                        category="lint",
                        discovered_at=now_iso(),
                    )
                )
        return findings

    def _text_scan(self, repo_path: Path) -> List[Finding]:
        """Regex-based fallback: detects ``any`` types, TODOs, console.log, and oversized files."""
        from bluei.engine.models import now_iso

        findings = []

        if not self.detect(repo_path):
            return findings

        is_typescript = self._is_typescript(repo_path)

        extensions = (
            [".ts", ".tsx", ".js", ".jsx"] if is_typescript else [".js", ".jsx"]
        )

        for ext in extensions:
            for file_path in repo_path.rglob(f"*{ext}"):
                if "node_modules" in str(file_path):
                    continue
                if str(file_path).startswith("."):
                    continue

                try:
                    content = file_path.read_text()
                    lines = content.split("\n")

                    rel_path = str(file_path.relative_to(repo_path))

                    for i, line in enumerate(lines, 1):
                        if is_typescript and ": any" in line:
                            findings.append(
                                Finding(
                                    finding_id=stable_finding_id(
                                        str(repo_path),
                                        rel_path,
                                        i,
                                        "type-explicit-any",
                                        line.strip(),
                                    ),
                                    repo=str(repo_path),
                                    path=rel_path,
                                    line=i,
                                    rule="type-explicit-any",
                                    snippet=line.strip()[:200],
                                    confidence=0.85,
                                    quick_win=True,
                                    safe_to_autofix=True,
                                    severity="medium",
                                    category="type-safety",
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
                                        "xo-no-warning-comments",
                                        line.strip(),
                                    ),
                                    repo=str(repo_path),
                                    path=rel_path,
                                    line=i,
                                    rule="xo-no-warning-comments",
                                    snippet=line.strip()[:200],
                                    confidence=0.80,
                                    quick_win=True,
                                    safe_to_autofix=True,
                                    severity="low",
                                    category="lint",
                                    discovered_at=now_iso(),
                                )
                            )

                        if "console.log" in line and not (
                            "test" in rel_path.lower() or "spec" in rel_path.lower()
                        ):
                            findings.append(
                                Finding(
                                    finding_id=stable_finding_id(
                                        str(repo_path),
                                        rel_path,
                                        i,
                                        "debug-console-log",
                                        line.strip(),
                                    ),
                                    repo=str(repo_path),
                                    path=rel_path,
                                    line=i,
                                    rule="xo-no-warning-comments",
                                    snippet=line.strip()[:200],
                                    confidence=0.70,
                                    quick_win=True,
                                    safe_to_autofix=True,
                                    severity="low",
                                    category="lint",
                                    discovered_at=now_iso(),
                                )
                            )

                except Exception:
                    continue

        for ext in extensions:
            for file_path in repo_path.rglob(f"*{ext}"):
                if "node_modules" in str(file_path):
                    continue

                try:
                    content = file_path.read_text()
                    lines = content.split("\n")

                    if len(lines) > 500:
                        rel_path = str(file_path.relative_to(repo_path))
                        findings.append(
                            Finding(
                                finding_id=stable_finding_id(
                                    str(repo_path),
                                    rel_path,
                                    1,
                                    "xo-max-lines",
                                    f"{len(lines)} lines",
                                ),
                                repo=str(repo_path),
                                path=rel_path,
                                line=1,
                                rule="xo-max-lines",
                                snippet=f"File has {len(lines)} lines (threshold: 500)",
                                confidence=0.85,
                                quick_win=False,
                                safe_to_autofix=True,
                                severity="medium",
                                category="refactor",
                                discovered_at=now_iso(),
                            )
                        )
                except Exception:
                    continue

        return findings

    def discover(self, repo_path: Path, config: Dict[str, Any]) -> List[Finding]:
        """Run eslint/xo, then merge with text-scan findings (deduped by path/line/rule).

        Args:
            repo_path: Repository root to scan.
            config: Plugin configuration dict (unused by this plugin).

        Returns:
            Merged list of Findings from tool output and text scanning.
        """
        tool_findings = []
        output = self._run_tool(repo_path)
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

    def detect(self, repo_path: Path) -> bool:
        """Return True if repo has tsconfig.json, package.json with deps/devDeps/scripts."""
        if (repo_path / "tsconfig.json").exists():
            return True

        package_json = repo_path / "package.json"
        if package_json.exists():
            try:
                with open(package_json) as f:
                    pkg = json.load(f)
                if (
                    pkg.get("dependencies")
                    or pkg.get("devDependencies")
                    or pkg.get("scripts")
                ):
                    return True
            except Exception:
                _logger.debug("Failed to read package.json for TS validation")

        return False
