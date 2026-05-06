#!/usr/bin/env python3
"""Python language plugin for QA Agent."""

import hashlib
from pathlib import Path
from typing import Any, Dict, List
import re

from qa_agent.models import Finding
from qa_agent.plugins import DiscoveryPlugin


def stable_finding_id(repo: str, path: str, line: int, rule: str, snippet: str) -> str:
    """Generate a stable finding ID."""
    material = f'{repo}|{path}|{line}|{rule}|{snippet.strip()}'
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


class Plugin(DiscoveryPlugin):
    """Python discovery plugin.
    
    This plugin delegates to the core sandbox_local_runner.py for actual
    discovery. It provides Python-specific configuration and detection.
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
    
    def detect(self, repo_path: Path) -> bool:
        """Check if this is a Python repo."""
        markers = ['setup.py', 'pyproject.toml', 'requirements.txt', 'Pipfile', 'setup.cfg']
        return any((repo_path / m).exists() for m in markers)
    
    def discover(self, repo_path: Path, config: Dict[str, Any]) -> List[Finding]:
        """Run Python-specific discovery.
        
        This method provides sample findings for testing. In production,
        the actual discovery is delegated to sandbox_local_runner.py.
        """
        from qa_agent.models import now_iso
        
        findings = []
        
        # Only generate sample findings for testing if this is a test repo
        # In production, the runner calls sandbox_local_runner.py directly
        if not (repo_path / 'setup.py').exists() and not (repo_path / 'pyproject.toml').exists():
            return findings
        
        # Sample Python-specific discovery patterns
        # These are basic patterns - full discovery is in sandbox_local_runner.py
        
        # Check for broad except clauses
        for py_file in repo_path.rglob('*.py'):
            if '__pycache__' in str(py_file):
                continue
            
            try:
                content = py_file.read_text()
                lines = content.split('\n')
                
                rel_path = str(py_file.relative_to(repo_path))
                
                for i, line in enumerate(lines, 1):
                    # Broad except detection
                    if re.search(r'except\s*:', line):
                        findings.append(Finding(
                            finding_id=stable_finding_id(str(repo_path), rel_path, i, 'broad-except', line.strip()),
                            repo=str(repo_path),
                            path=rel_path,
                            line=i,
                            rule='broad-except',
                            snippet=line.strip()[:200],
                            confidence=0.88,
                            quick_win=False,
                            safe_to_autofix=False,
                            severity='medium',
                            category='lint',
                            discovered_at=now_iso()
                        ))
                    
                    # Trailing whitespace
                    if line.rstrip() != line and line.strip():
                        findings.append(Finding(
                            finding_id=stable_finding_id(str(repo_path), rel_path, i, 'trailing-whitespace', line.strip()),
                            repo=str(repo_path),
                            path=rel_path,
                            line=i,
                            rule='trailing-whitespace',
                            snippet=line.strip()[:200],
                            confidence=0.75,
                            quick_win=True,
                            safe_to_autofix=True,
                            severity='low',
                            category='lint',
                            discovered_at=now_iso()
                        ))
                    
                    # TODO/FIXME markers
                    if 'TODO' in line or 'FIXME' in line:
                        findings.append(Finding(
                            finding_id=stable_finding_id(str(repo_path), rel_path, i, 'debt-todo-marker', line.strip()),
                            repo=str(repo_path),
                            path=rel_path,
                            line=i,
                            rule='debt-todo-marker',
                            snippet=line.strip()[:200],
                            confidence=0.72,
                            quick_win=False,
                            safe_to_autofix=False,
                            severity='low',
                            category='todo/debt',
                            discovered_at=now_iso()
                        ))
                        
            except Exception as e:
                # Skip files that can't be read
                continue
        
        return findings
