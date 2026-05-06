#!/usr/bin/env python3
"""Repository registry management."""

import fcntl
import json
import os
from pathlib import Path
from typing import Dict, List, Optional
import yaml

from .models import Repo, RepoConfig, RepoStatus, generate_id, now_iso
from .config import ConfigManager


class RepoRegistry:
    """Manages the registry of onboarded repositories."""
    
    def __init__(self, config_manager: ConfigManager):
        self.config = config_manager
        self.registry_file = config_manager.workspace / 'registry.yaml'
        self._ensure_registry()
    
    def _ensure_registry(self):
        """Ensure registry file exists."""
        if not self.registry_file.exists():
            self._save_registry({'repos': [], 'version': '1.0'})
    
    def _load_registry(self) -> Dict:
        """Load registry data."""
        if not self.registry_file.exists():
            return {'repos': [], 'version': '1.0'}
        with open(self.registry_file) as f:
            return yaml.safe_load(f) or {'repos': [], 'version': '1.0'}
    
    def _save_registry(self, data: Dict):
        """Save registry data."""
        with open(self.registry_file, 'w') as f:
            yaml.dump(data, f, default_flow_style=False)
    
    def _load_repo_state_file(self, state_file: Path) -> Dict:
        if not state_file.exists():
            return {}
        try:
            with open(state_file) as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}

    def _save_repo_state_file(self, state_file: Path, state: Dict) -> None:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = state_file.with_suffix(state_file.suffix + '.tmp')
        lock_file = state_file.with_suffix('.lock')
        with open(lock_file, 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                with open(tmp_file, 'w') as f:
                    json.dump(state, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_file, state_file)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def create(self, config: RepoConfig) -> Repo:
        """Create a new repo entry."""
        repo = Repo(config=config, status=RepoStatus.IDLE)
        
        # Save config
        self.config.save_repo_config(config)
        
        # Update registry
        registry = self._load_registry()
        # Check if already exists
        existing = [r for r in registry['repos'] if r.get('name') == config.name]
        if not existing:
            registry['repos'].append({
                'id': config.id,
                'name': config.name,
                'path': config.path,
                'language': config.language,
                'enabled': config.enabled,
            })
            self._save_registry(registry)
        
        # Create state directories
        state_dir = self.config.repos_dir / config.name / 'state'
        state_dir.mkdir(parents=True, exist_ok=True)
        
        return repo
    
    def read(self, repo_name: str) -> Optional[Repo]:
        """Read a repo by name."""
        config = self.config.load_repo_config(repo_name)
        if not config:
            return None
        
        # Load state
        state_file = self.config.repos_dir / repo_name / 'state' / 'repo_state.json'
        if state_file.exists():
            state = self._load_repo_state_file(state_file)
            return Repo(
                config=config,
                status=RepoStatus(state.get('status', 'idle')),
                onboarded_at=state.get('onboarded_at'),
                last_run_at=state.get('last_run_at'),
                current_findings_count=state.get('current_findings_count', 0),
                current_health_score=state.get('current_health_score', 0.0),
                total_fixes=state.get('total_fixes', 0),
                total_prs=state.get('total_prs', 0),
                total_merges=state.get('total_merges', 0),
            )
        
        return Repo(config=config)
    
    def update(self, repo_name: str, updates: Dict) -> Optional[Repo]:
        """Update repo state."""
        repo = self.read(repo_name)
        if not repo:
            return None
        
        # Update config if provided
        if any(k in updates for k in ['enabled', 'limits', 'cooldowns', 'github', 'language', 'framework']):
            config = repo.config
            for key, value in updates.items():
                if hasattr(config, key):
                    setattr(config, key, value)
            self.config.save_repo_config(config)
        
        # Update state
        state_file = self.config.repos_dir / repo_name / 'state' / 'repo_state.json'
        state = self._load_repo_state_file(state_file)
        
        # Map status enum to string
        if 'status' in updates:
            status_val = updates['status']
            if hasattr(status_val, 'value'):
                state['status'] = status_val.value
            else:
                state['status'] = status_val
        else:
            state['status'] = state.get('status', 'idle')
        
        state.update({
            'onboarded_at': updates.get('onboarded_at', state.get('onboarded_at')),
            'last_run_at': updates.get('last_run_at', state.get('last_run_at')),
            'current_findings_count': updates.get('current_findings_count', state.get('current_findings_count', 0)),
            'current_health_score': updates.get('current_health_score', state.get('current_health_score', 0.0)),
            'total_fixes': updates.get('total_fixes', state.get('total_fixes', 0)),
            'total_prs': updates.get('total_prs', state.get('total_prs', 0)),
            'total_merges': updates.get('total_merges', state.get('total_merges', 0)),
            'updated_at': now_iso(),
        })
        
        self._save_repo_state_file(state_file, state)
        
        return self.read(repo_name)
    
    def delete(self, repo_name: str) -> bool:
        """Delete a repo from registry."""
        # Remove from registry
        registry = self._load_registry()
        original_count = len(registry['repos'])
        registry['repos'] = [
            r for r in registry['repos'] 
            if r.get('name') != repo_name
        ]
        self._save_registry(registry)
        
        # Remove config
        config_path = self.config.get_repo_config_path(repo_name)
        if config_path.exists():
            config_path.unlink()
        
        return len(registry['repos']) < original_count
    
    def list_all(self) -> List[Repo]:
        """List all repos."""
        repos = []
        for name in self.config.list_repo_configs().keys():
            repo = self.read(name)
            if repo:
                repos.append(repo)
        return repos
    
    def list_enabled(self) -> List[Repo]:
        """List enabled repos."""
        return [r for r in self.list_all() if r.config.enabled]
    
    def find_by_name(self, name: str) -> Optional[Repo]:
        """Find repo by name."""
        return self.read(name)
    
    def find_by_path(self, path: Path) -> Optional[Repo]:
        """Find repo by path."""
        path = Path(path).resolve()
        for repo in self.list_all():
            if Path(repo.config.path).resolve() == path:
                return repo
        return None
