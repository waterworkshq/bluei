#!/usr/bin/env python3
"""Plugin discovery and loading."""

import logging
import importlib.util
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml

from bluei.engine.models import Finding

_logger = logging.getLogger(__name__)


class DiscoveryPlugin(ABC):
    """Base class for discovery plugins."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Plugin identifier."""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name."""
        pass

    @property
    @abstractmethod
    def languages(self) -> List[str]:
        """Supported languages."""
        pass

    @property
    @abstractmethod
    def rules(self) -> List[str]:
        """Rules this plugin can discover."""
        pass

    @abstractmethod
    def discover(self, repo_path: Path, config: Dict[str, Any]) -> List[Finding]:
        """Run discovery and return findings."""
        pass

    @abstractmethod
    def detect(self, repo_path: Path) -> bool:
        """Check if this plugin applies to the repo."""
        pass


class ToolWrapperPlugin(DiscoveryPlugin):
    """Base class for plugins that invoke external CLI tools.

    Subclasses implement parse_output() to transform tool JSON into Findings.
    If the tool is not installed, discover() falls back to text scanning (returns []).
    """

    def __init__(self, manifest: Optional[Dict] = None):
        self._manifest = manifest or {}

    @property
    def tool_name(self) -> str:
        return (self._manifest.get("discovery") or {}).get("tool", "")

    @property
    def tool_args(self) -> List[str]:
        return (self._manifest.get("discovery") or {}).get("tool_args", [])

    def _run_tool(
        self, repo_path: Path, extra_args: Optional[List[str]] = None
    ) -> Optional[str]:
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
            if result.stdout:
                return result.stdout
            if result.returncode == 0:
                return result.stdout
            return None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def parse_output(self, output: str, repo_path: Path) -> List[Finding]:
        raise NotImplementedError

    def discover(self, repo_path: Path, config: Dict[str, Any]) -> List[Finding]:
        output = self._run_tool(repo_path)
        if output is not None:
            return self.parse_output(output, repo_path)
        return []

    def detect(self, repo_path: Path) -> bool:
        detection = self._manifest.get("detection") or {}
        files = detection.get("files", [])
        for pattern in files:
            if list(repo_path.glob(pattern)):
                return True
        return False


class PluginLoader:
    """Discovers and loads language-specific plugins."""

    def __init__(self, plugins_dir: Path):
        self.plugins_dir = Path(plugins_dir)
        self._plugins: Dict[str, DiscoveryPlugin] = {}
        self._manifests: Dict[str, Dict] = {}

    def discover(self) -> List[Dict]:
        """Discover all available plugins."""
        manifests = []

        if not self.plugins_dir.exists():
            return manifests

        for plugin_dir in self.plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue

            manifest_file = plugin_dir / "plugin.yaml"
            if manifest_file.exists():
                try:
                    with open(manifest_file) as f:
                        manifest = yaml.safe_load(f)
                    manifest["_path"] = str(plugin_dir)
                    manifest["health_mapping"] = self._load_health_mapping(plugin_dir)
                    manifests.append(manifest)
                    self._manifests[manifest["id"]] = manifest
                    self._register_health_mapping(manifest["health_mapping"])
                except Exception as e:
                    print(
                        f"Warning: Failed to load plugin manifest {manifest_file}: {e}"
                    )

        return manifests

    def load(self, plugin_id: str) -> Optional[DiscoveryPlugin]:
        """Load a specific plugin."""
        if plugin_id in self._plugins:
            return self._plugins[plugin_id]

        manifest = self._manifests.get(plugin_id)
        if not manifest:
            # Try to discover first
            self.discover()
            manifest = self._manifests.get(plugin_id)

        if not manifest:
            return None

        plugin_path = Path(manifest["_path"])
        plugin_file = plugin_path / "plugin.py"

        if not plugin_file.exists():
            return None

        # Load plugin module
        try:
            spec = importlib.util.spec_from_file_location(
                f"plugin_{plugin_id}", plugin_file
            )
            if not spec or not spec.loader:
                return None

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Get plugin class
            plugin_class = getattr(module, "Plugin", None)
            if not plugin_class:
                return None

            plugin = plugin_class()
            self._plugins[plugin_id] = plugin

            return plugin
        except Exception as e:
            print(f"Warning: Failed to load plugin {plugin_id}: {e}")
            return None

    def load_all(self) -> Dict[str, DiscoveryPlugin]:
        """Load all discovered plugins."""
        self.discover()
        for plugin_id in list(self._manifests.keys()):
            self.load(plugin_id)
        return self._plugins

    def get(self, plugin_id: str) -> Optional[DiscoveryPlugin]:
        """Get a loaded plugin."""
        return self._plugins.get(plugin_id)

    def get_for_language(self, language: str) -> Optional[DiscoveryPlugin]:
        """Get plugin for a language."""
        # First ensure plugins are loaded
        if not self._plugins:
            self.load_all()

        for plugin in self._plugins.values():
            if language.lower() in [l.lower() for l in plugin.languages]:
                return plugin
        return None

    def list_loaded(self) -> List[str]:
        """List loaded plugin IDs."""
        return list(self._plugins.keys())

    def get_manifest(self, plugin_id: str) -> Optional[Dict]:
        """Get plugin manifest."""
        return self._manifests.get(plugin_id)

    def list_available(self) -> List[str]:
        """List all available plugin IDs (discovered but not necessarily loaded)."""
        if not self._manifests:
            self.discover()
        return list(self._manifests.keys())

    def detector_catalog(self) -> List[Dict[str, Any]]:
        """Return detector metadata declared by language-pack manifests."""
        if not self._manifests:
            self.discover()
        entries: List[Dict[str, Any]] = []
        for manifest in self._manifests.values():
            languages = manifest.get("languages") or ["unknown"]
            language = languages[0] if languages else "unknown"
            for rule in (manifest.get("discovery") or {}).get("rules") or []:
                if not rule.get("id"):
                    continue
                entries.append(
                    {
                        "rule": rule["id"],
                        "category": rule.get("category", "lint"),
                        "confidence": float(rule.get("confidence", 0.7)),
                        "autofix": bool(rule.get("autofix", False)),
                        "language": rule.get("language", language),
                    }
                )
        return entries

    def _load_health_mapping(self, plugin_dir: Path) -> Dict[str, Any]:
        mapping_file = plugin_dir / "health_mapping.yaml"
        if not mapping_file.exists():
            return {"rule_to_component": {}, "category_inference": {}}
        try:
            with open(mapping_file) as f:
                payload = yaml.safe_load(f) or {}
        except Exception:
            return {"rule_to_component": {}, "category_inference": {}}
        return {
            "rule_to_component": dict(payload.get("rule_to_component") or {}),
            "category_inference": dict(payload.get("category_inference") or {}),
        }

    def _register_health_mapping(self, mapping: Dict[str, Any]) -> None:
        try:
            from .health import HealthEngine

            HealthEngine.register_language_pack_mappings(
                rule_to_component=mapping.get("rule_to_component") or {},
                category_inference=mapping.get("category_inference") or {},
            )
        except Exception:
            return
