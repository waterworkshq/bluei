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

from bluei.engine.constants import DETECTOR_CATALOG
from bluei.engine.models import Finding, canonical_ruff_rule
from bluei.app.plugins import ToolWrapperPlugin

_logger = logging.getLogger(__name__)

# Confidence for ruff diagnostics whose code is not in DETECTOR_CATALOG.
# Catalog codes use the catalog confidence so the native ruff producer and
# this plugin report identical metadata for the same rule.
_PLUGIN_ONLY_RUFF_CONFIDENCE = 0.88


def stable_finding_id(repo: str, path: str, line: int, rule: str, snippet: str) -> str:
    """Deterministic SHA-256 ID from repo, path, line, rule, and snippet."""
    material = f"{repo}|{path}|{line}|{rule}|{snippet.strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Plugin(ToolWrapperPlugin):
    """Wraps ``ruff`` and supplements with regex-based text scanning.

    Ruff diagnostics keep their canonical ``ruff-<code>`` lowercase identity
    (same rule, confidence, and autofix safety as the native ruff producer);
    the regex text scan emits the internal rule names (broad-except,
    trailing-whitespace, debt-todo-marker) for lines the tool did not cover.
    Falls back to pure text scanning when ruff is unavailable.
    """

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

    def parse_output(
        self, output: str, repo_path: Path, raw_rule_ids: bool = False
    ) -> List[Finding]:
        """Parse ruff JSON output into Findings.

        Args:
            output: Raw JSON string from ``ruff --output-format json``.
            repo_path: Repository root used for computing relative paths.
            raw_rule_ids: Kept for backward compatibility. Both lanes now
                emit the same canonical lowercase ``ruff-<code>`` rules with
                identity, confidence, and autofix safety matching the native
                ruff producer, so the python discovery scope can deduplicate
                the two producers by instance.

        Returns:
            List of Findings, one per ruff diagnostic with a well-formed code.
        """
        from bluei.engine.models import now_iso

        try:
            items = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(items, list):
            return []

        catalog_meta = {
            str(entry["rule"]): entry
            for entry in DETECTOR_CATALOG
            if str(entry["rule"]).startswith("ruff-")
        }

        findings = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # Helper contract: full-rule in/out — canonical_ruff_rule
            # ("ruff-B904") -> "ruff-b904"; anything not a well-formed
            # ruff rule id returns None and is skipped.
            rule = canonical_ruff_rule(f"ruff-{str(item.get('code', ''))}")
            if rule is None:
                continue
            loc = item.get("location", {})
            if not isinstance(loc, dict):
                # Native parser parity: skip this diagnostic, not the run.
                continue
            try:
                line = int(loc.get("row", 0))
            except (TypeError, ValueError):
                # Malformed row: skip per-item exactly like the native
                # producer's except clause; default row is 0, not 1.
                continue
            raw_path = item.get("filename") or item.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                continue
            try:
                path = str((repo_path / raw_path).resolve().relative_to(repo_path.resolve()))
            except (ValueError, OSError, RuntimeError):
                continue
            if path == ".":
                continue
            # Truncate before hashing: the native producer hashes the same
            # truncated material, so an identical diagnostic gets an
            # identical finding id regardless of producer.
            message = str(item.get("message", ""))[:200]
            # Autofix safety comes from this instance's fix applicability,
            # not the rule code prefix; mirrors the native producer,
            # including the c408 override.
            fix_obj = item.get("fix")
            applicability = (
                fix_obj.get("applicability") if isinstance(fix_obj, dict) else None
            )
            safe_to_autofix = applicability in ("safe", "generated")
            if rule == "ruff-c408":
                safe_to_autofix = False
            meta = catalog_meta.get(rule)
            confidence = (
                float(meta.get("confidence", _PLUGIN_ONLY_RUFF_CONFIDENCE))
                if meta is not None
                else _PLUGIN_ONLY_RUFF_CONFIDENCE
            )
            findings.append(
                Finding(
                    finding_id=stable_finding_id(
                        str(repo_path), path, line, rule, message
                    ),
                    repo=str(repo_path),
                    path=path,
                    line=line,
                    rule=rule,
                    snippet=message,
                    confidence=confidence,
                    quick_win=safe_to_autofix,
                    safe_to_autofix=safe_to_autofix,
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
            config: Plugin configuration dict. ``{"ruff_only": True}``
                restricts discovery to the actual ruff tool run (canonical
                ``ruff-<code>`` rules only): no regex text-scan findings and
                no text-scan fallback when the tool is unavailable.

        Returns:
            Merged list of Findings from tool output and text scanning, or
            only tool findings when ``ruff_only`` is set.
        """
        ruff_only = bool((config or {}).get("ruff_only"))
        tool_findings = []
        output = self._run_tool(repo_path, extra_args=["."])
        if output is not None:
            tool_findings = self.parse_output(
                output, repo_path, raw_rule_ids=ruff_only
            )

        if ruff_only:
            # Provenance-complete mode for the python discovery scope: a
            # clean real-tool run must emit nothing, so no text-scan
            # fallback and no internal rule names.
            return tool_findings

        text_findings = self._text_scan(repo_path)

        if not tool_findings:
            return text_findings

        tool_keys = {(f.path, f.line, f.rule) for f in tool_findings}
        for f in text_findings:
            if (f.path, f.line, f.rule) not in tool_keys:
                tool_findings.append(f)

        return tool_findings
