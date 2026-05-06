#!/usr/bin/env python3
"""Rust language plugin for QA Agent."""

import hashlib
from pathlib import Path
from typing import Any, Dict, List

from qa_agent.models import Finding, now_iso
from qa_agent.plugins import DiscoveryPlugin


def stable_finding_id(repo: str, path: str, line: int, rule: str, snippet: str) -> str:
    material = f'{repo}|{path}|{line}|{rule}|{snippet.strip()}'
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


class Plugin(DiscoveryPlugin):
    @property
    def id(self) -> str:
        return 'plugin-rust'

    @property
    def name(self) -> str:
        return 'Rust Plugin'

    @property
    def languages(self) -> List[str]:
        return ['rust']

    @property
    def rules(self) -> List[str]:
        return ['debt-todo-marker']

    def detect(self, repo_path: Path) -> bool:
        return (repo_path / 'Cargo.toml').exists()

    def discover(self, repo_path: Path, config: Dict[str, Any]) -> List[Finding]:
        findings: List[Finding] = []
        for rs_file in repo_path.rglob('*.rs'):
            try:
                rel_path = str(rs_file.relative_to(repo_path))
                for i, line in enumerate(rs_file.read_text().splitlines(), 1):
                    if 'TODO' in line or 'FIXME' in line:
                        findings.append(Finding(
                            finding_id=stable_finding_id(str(repo_path), rel_path, i, 'debt-todo-marker', line),
                            repo=str(repo_path),
                            path=rel_path,
                            line=i,
                            rule='debt-todo-marker',
                            snippet=line[:200],
                            confidence=0.72,
                            quick_win=False,
                            safe_to_autofix=False,
                            severity='low',
                            category='technical-debt',
                            discovered_at=now_iso(),
                        ))
            except Exception:
                continue
        return findings
