#!/usr/bin/env python3
"""TypeScript/JavaScript language plugin for QA Agent."""

import hashlib
import json
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
    """TypeScript/JavaScript discovery plugin.
    
    This plugin provides TypeScript/JavaScript-specific discovery.
    For XO linter and TypeScript compiler integration, it typically
    runs inside a Docker container or uses npx.
    """
    
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
    
    def detect(self, repo_path: Path) -> bool:
        """Check if this is a TypeScript/JavaScript repo."""
        # TypeScript specific
        if (repo_path / 'tsconfig.json').exists():
            return True
        
        # JavaScript (with package.json)
        package_json = repo_path / 'package.json'
        if package_json.exists():
            # Check if it's a Node.js project (not just a frontend asset)
            try:
                with open(package_json) as f:
                    pkg = json.load(f)
                # Has dependencies or scripts suggests it's a Node.js project
                if pkg.get('dependencies') or pkg.get('devDependencies') or pkg.get('scripts'):
                    return True
            except:
                pass
        
        return False
    
    def _is_typescript(self, repo_path: Path) -> bool:
        """Check if repo uses TypeScript."""
        if (repo_path / 'tsconfig.json').exists():
            return True
        
        package_json = repo_path / 'package.json'
        if package_json.exists():
            try:
                with open(package_json) as f:
                    pkg = json.load(f)
                deps = {**pkg.get('dependencies', {}), **pkg.get('devDependencies', {})}
                if 'typescript' in deps:
                    return True
            except:
                pass
        
        return False
    
    def discover(self, repo_path: Path, config: Dict[str, Any]) -> List[Finding]:
        """Run TypeScript/JavaScript-specific discovery.
        
        This method provides sample findings for testing. In production,
        the actual discovery would run XO linter and TypeScript compiler.
        """
        from qa_agent.models import now_iso
        
        findings = []
        
        # Only process if this looks like a JS/TS project
        if not self.detect(repo_path):
            return findings
        
        is_typescript = self._is_typescript(repo_path)
        
        # Check for common patterns in TS/JS files
        extensions = ['.ts', '.tsx', '.js', '.jsx'] if is_typescript else ['.js', '.jsx']
        
        for ext in extensions:
            for file_path in repo_path.rglob(f'*{ext}'):
                if 'node_modules' in str(file_path):
                    continue
                if str(file_path).startswith('.'):
                    continue
                
                try:
                    content = file_path.read_text()
                    lines = content.split('\n')
                    
                    rel_path = str(file_path.relative_to(repo_path))
                    
                    for i, line in enumerate(lines, 1):
                        # Explicit any detection (TypeScript)
                        if is_typescript and ': any' in line:
                            findings.append(Finding(
                                finding_id=stable_finding_id(str(repo_path), rel_path, i, 'type-explicit-any', line.strip()),
                                repo=str(repo_path),
                                path=rel_path,
                                line=i,
                                rule='type-explicit-any',
                                snippet=line.strip()[:200],
                                confidence=0.85,
                                quick_win=True,
                                safe_to_autofix=True,
                                severity='medium',
                                category='type-safety',
                                discovered_at=now_iso()
                            ))
                        
                        # TODO/FIXME comments
                        if 'TODO' in line or 'FIXME' in line:
                            findings.append(Finding(
                                finding_id=stable_finding_id(str(repo_path), rel_path, i, 'xo-no-warning-comments', line.strip()),
                                repo=str(repo_path),
                                path=rel_path,
                                line=i,
                                rule='xo-no-warning-comments',
                                snippet=line.strip()[:200],
                                confidence=0.80,
                                quick_win=True,
                                safe_to_autofix=True,
                                severity='low',
                                category='lint',
                                discovered_at=now_iso()
                            ))
                        
                        # Console.log (potential debugging leftover)
                        if 'console.log' in line and not ('test' in rel_path.lower() or 'spec' in rel_path.lower()):
                            findings.append(Finding(
                                finding_id=stable_finding_id(str(repo_path), rel_path, i, 'debug-console-log', line.strip()),
                                repo=str(repo_path),
                                path=rel_path,
                                line=i,
                                rule='xo-no-warning-comments',
                                snippet=line.strip()[:200],
                                confidence=0.70,
                                quick_win=True,
                                safe_to_autofix=True,
                                severity='low',
                                category='lint',
                                discovered_at=now_iso()
                            ))
                        
                except Exception as e:
                    continue
        
        # Check for file length (max-lines heuristic)
        for ext in extensions:
            for file_path in repo_path.rglob(f'*{ext}'):
                if 'node_modules' in str(file_path):
                    continue
                
                try:
                    content = file_path.read_text()
                    lines = content.split('\n')
                    
                    if len(lines) > 500:  # Heuristic threshold
                        rel_path = str(file_path.relative_to(repo_path))
                        findings.append(Finding(
                            finding_id=stable_finding_id(str(repo_path), rel_path, 1, 'xo-max-lines', f'{len(lines)} lines'),
                            repo=str(repo_path),
                            path=rel_path,
                            line=1,
                            rule='xo-max-lines',
                            snippet=f'File has {len(lines)} lines (threshold: 500)',
                            confidence=0.85,
                            quick_win=False,
                            safe_to_autofix=True,
                            severity='medium',
                            category='refactor',
                            discovered_at=now_iso()
                        ))
                except:
                    continue
        
        return findings
