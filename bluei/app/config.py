#!/usr/bin/env python3
"""Configuration management for bluei."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional
import yaml

_logger = logging.getLogger(__name__)


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


WORKSPACE = Path(
    os.environ.get("QA_AGENT_WORKSPACE", Path(__file__).resolve().parents[2])
).expanduser()

_global_config_cache: Dict[str, Any] = {}


def load_global_config(workspace: Optional[Path] = None) -> Dict[str, Any]:
    """Load the global config.yaml from workspace root.

    Returns a dict with sections like ``mnemo``, ``github``, etc.
    Results are cached per workspace path.
    """
    ws = (workspace or WORKSPACE).resolve()
    cache_key = str(ws)
    if cache_key in _global_config_cache:
        return _global_config_cache[cache_key]

    config_path = ws / "config.yaml"
    config: Dict[str, Any] = {}
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                config = loaded
        except (yaml.YAMLError, OSError):
            _logger.debug("Failed to read global config.yaml")
            pass

    _global_config_cache[cache_key] = config
    return config


def reload_global_config(workspace: Optional[Path] = None) -> Dict[str, Any]:
    """Force-reload the global config (e.g. after writes)."""
    ws = (workspace or WORKSPACE).resolve()
    _global_config_cache.pop(str(ws), None)
    return load_global_config(ws)


class ConfigManager:
    """Manages agent and repo configurations."""

    def __init__(self, workspace: Optional[Path] = None):
        self.workspace = (workspace or WORKSPACE).resolve()
        self.repos_dir = self.workspace / "repos"
        self.plugins_dir = self.workspace / "plugins"
        self.templates_dir = self.workspace / "templates"
        self.repo_templates_dir = self.templates_dir / "repos"

        # Ensure directories exist
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.repo_templates_dir.mkdir(parents=True, exist_ok=True)

    def get_repo_config_path(self, repo_name: str) -> Path:
        """Get path to repo config file."""
        return self.repos_dir / repo_name / "config.yaml"

    def load_repo_config(self, repo_name: str) -> Optional[RepoConfig]:
        """Load a repo's configuration with validation."""
        from bluei.app.models import RepoConfig

        config_path = self.get_repo_config_path(repo_name)
        if not config_path.exists():
            return None
        try:
            config = RepoConfig.from_yaml(config_path)
        except (ValueError, yaml.YAMLError, FileNotFoundError) as e:
            self._log_config_error(repo_name, f"parse-error: {e}")
            return None

        errors = config.validate()
        if errors:
            self._log_config_error(repo_name, "; ".join(errors))
            return None

        return config

    def _log_config_error(self, repo_name: str, detail: str) -> None:
        """Write a config validation failure to the escalation log."""
        from datetime import datetime, timezone
        import json

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "config_validation_failure",
            "repo": repo_name,
            "detail": detail,
        }
        escalation_file = self.workspace / "state" / "escalation_log.jsonl"
        try:
            escalation_file.parent.mkdir(parents=True, exist_ok=True)
            with open(escalation_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            _logger.debug("Failed to append escalation record")  # Best-effort logging

    def save_repo_config(self, config: RepoConfig) -> Path:
        """Save a repo's configuration."""
        config_path = self.get_repo_config_path(config.name)
        config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(config_path, "w") as f:
            yaml.dump(config.to_dict(), f, default_flow_style=False)

        return config_path

    def list_repo_configs(self) -> Dict[str, Path]:
        """List all repo config files."""
        configs = {}
        for repo_dir in self.repos_dir.iterdir():
            if repo_dir.is_dir():
                config_path = repo_dir / "config.yaml"
                if config_path.exists():
                    configs[repo_dir.name] = config_path
        return configs

    def get_template(self, name: str) -> str:
        """Get a legacy text template file content."""
        template_path = self.templates_dir / f"{name}.template"
        if not template_path.exists():
            raise FileNotFoundError(f"Template not found: {template_path}")
        return template_path.read_text()

    def get_repo_template_path(self, name: str) -> Path:
        return self.repo_templates_dir / f"{name}.yaml"

    def load_repo_template(self, name: str) -> Dict[str, Any]:
        template_path = self.get_repo_template_path(name)
        if not template_path.exists():
            raise FileNotFoundError(f"Repo template not found: {template_path}")
        with open(template_path) as f:
            return yaml.safe_load(f) or {}

    def list_repo_templates(self) -> Dict[str, Path]:
        templates: Dict[str, Path] = {}
        if not self.repo_templates_dir.exists():
            return templates
        for path in self.repo_templates_dir.glob("*.yaml"):
            templates[path.stem] = path
        return templates

    def get_rule_packs_dir(self) -> Path:
        return self.templates_dir / "rules"

    def load_rule_pack(self, name: str) -> Dict[str, Any]:
        pack_path = self.get_rule_packs_dir() / f"{name}.yaml"
        if not pack_path.exists():
            raise FileNotFoundError(f"Rule pack not found: {pack_path}")
        with open(pack_path) as f:
            return yaml.safe_load(f) or {}

    def list_rule_packs(self) -> Dict[str, Path]:
        packs: Dict[str, Path] = {}
        packs_dir = self.get_rule_packs_dir()
        if not packs_dir.exists():
            return packs
        for path in packs_dir.glob("*.yaml"):
            packs[path.stem] = path
        return packs

    def render_config_from_template(
        self,
        name: str,
        path: str,
        language: str,
        template_name: Optional[str] = None,
        **kwargs,
    ) -> RepoConfig:
        """Create a config from a structured repo template."""
        from bluei.app.models import ONBOARDING_VERSION, RepoConfig

        merged: Dict[str, Any] = {}
        if template_name:
            merged = _deep_merge(merged, self.load_repo_template(template_name))
        merged = _deep_merge(merged, kwargs)
        merged.setdefault(
            "meta",
            {
                "onboarding_version": ONBOARDING_VERSION,
                "template": template_name,
                "inferred_by": "template",
            },
        )
        return RepoConfig(
            id=f"repo-{name}",
            name=name,
            path=path,
            language=language,
            **merged,
        )
