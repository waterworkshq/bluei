#!/usr/bin/env python3
"""Test plugin for unit testing."""

from pathlib import Path
from typing import Any, Dict, List

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from qa_agent.models import Finding
from qa_agent.plugins import DiscoveryPlugin


class Plugin(DiscoveryPlugin):
    """Test discovery plugin."""
    
    @property
    def id(self) -> str:
        return "plugin-test"
    
    @property
    def name(self) -> str:
        return "Test Plugin"
    
    @property
    def languages(self) -> List[str]:
        return ["test"]
    
    @property
    def rules(self) -> List[str]:
        return ["test-rule"]
    
    def detect(self, repo_path: Path) -> bool:
        """Check if this is a test repo."""
        return (repo_path / "test.txt").exists()
    
    def discover(self, repo_path: Path, config: Dict[str, Any]) -> List[Finding]:
        """Run test discovery."""
        # Return a test finding
        return [
            Finding(
                finding_id="test-finding-001",
                repo=str(repo_path),
                path="test.txt",
                line=1,
                rule="test-rule",
                snippet="test finding",
                confidence=0.85,
                quick_win=True,
                safe_to_autofix=True
            )
        ]
