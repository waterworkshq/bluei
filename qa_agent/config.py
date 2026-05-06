#!/usr/bin/env python3
"""Configuration management for QA Agent."""

import os
from pathlib import Path
from typing import Any, Dict, Optional
import yaml

from .models import RepoConfig


WORKSPACE = Path(
    os.environ.get('QA_AGENT_WORKSPACE', Path(__file__).resolve().parents[1])
).expanduser()


class ConfigManager:
    """Manages agent and repo configurations."""
    
    def __init__(self, workspace: Optional[Path] = None):
        self.workspace = (workspace or WORKSPACE).resolve()
        self.repos_dir = self.workspace / 'repos'
        self.plugins_dir = self.workspace / 'plugins'
        self.templates_dir = self.workspace / 'templates'
        self.repo_templates_dir = self.templates_dir / 'repos'
        
        # Ensure directories exist
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.repo_templates_dir.mkdir(parents=True, exist_ok=True)
    
    def get_repo_config_path(self, repo_name: str) -> Path:
        """Get path to repo config file."""
        return self.repos_dir / repo_name / 'config.yaml'
    
    def load_repo_config(self, repo_name: str) -> Optional[RepoConfig]:
        """Load a repo's configuration with validation."""
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
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'type': 'config_validation_failure',
            'repo': repo_name,
            'detail': detail,
        }
        escalation_file = self.workspace / 'state' / 'escalation_log.jsonl'
        try:
            escalation_file.parent.mkdir(parents=True, exist_ok=True)
            with open(escalation_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record) + '\n')
        except OSError:
            pass  # Best-effort logging
    
    def save_repo_config(self, config: RepoConfig) -> Path:
        """Save a repo's configuration."""
        config_path = self.get_repo_config_path(config.name)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(config_path, 'w') as f:
            yaml.dump(config.to_dict(), f, default_flow_style=False)
        
        return config_path
    
    def list_repo_configs(self) -> Dict[str, Path]:
        """List all repo config files."""
        configs = {}
        for repo_dir in self.repos_dir.iterdir():
            if repo_dir.is_dir():
                config_path = repo_dir / 'config.yaml'
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
        for path in self.repo_templates_dir.glob('*.yaml'):
            templates[path.stem] = path
        return templates
    
    def render_config_from_template(self, 
                                     name: str, 
                                     path: str, 
                                     language: str,
                                     template_name: Optional[str] = None,
                                     **kwargs) -> RepoConfig:
        """Create a config from a structured repo template."""
        merged: Dict[str, Any] = {}
        if template_name:
            merged.update(self.load_repo_template(template_name))
        merged.update(kwargs)
        merged.setdefault('meta', {
            'onboarding_version': 2,
            'template': template_name,
            'inferred_by': 'template',
        })
        return RepoConfig(
            id=f"repo-{name}",
            name=name,
            path=path,
            language=language,
            **merged,
        )
