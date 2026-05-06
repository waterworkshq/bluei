#!/usr/bin/env python3
"""Migrate ky repo state from old QA system to new QA Agent."""

import sys
import shutil
from pathlib import Path
from datetime import datetime, timezone
import json

sys.path.insert(0, str(Path(__file__).parent.parent))

from qa_agent.config import ConfigManager
from qa_agent.registry import RepoRegistry
from qa_agent.health import HealthEngine
from qa_agent.state import StateManager
from qa_agent.models import RepoConfig, RepoStatus, Finding


def migrate_ky():
    """Migrate ky repo from old QA system to new QA Agent."""
    
    workspace = Path.home() / '.openclaw' / 'workspace'
    qa_agent_dir = workspace / 'qa-agent'
    old_state_dir = workspace / 'pr-automation' / 'state'
    ky_repo = workspace / 'phase2' / 'ky'
    
    if not ky_repo.exists():
        print(f"❌ ky repo not found: {ky_repo}")
        return 1
    
    print("=" * 60)
    print("Migrating ky to QA Agent")
    print("=" * 60)
    
    # Setup components
    config = ConfigManager(qa_agent_dir)
    registry = RepoRegistry(config)
    health = HealthEngine()
    state = StateManager(config.repos_dir)
    
    # Delete existing ky if it was onboarded with wrong state
    if registry.find_by_name('ky'):
        print("\n🗑️  Removing existing ky config (will re-onboard with migrated state)...")
        registry.delete('ky')
    
    # Create config for ky with Docker container settings
    print("\n📋 Creating repository configuration...")
    ky_config = RepoConfig(
        id="repo-ky",
        name="ky",
        path=str(ky_repo),
        language="typescript",
        framework=None,
        enabled=True,
        plugin_id="plugin-typescript",
        github={
            'live_actions': True,
            'auto_merge': False,  # Never auto-merge for real repos
        },
        discovery={
            'use_docker': True,
            'container_name': 'ky-phase2-dev',
        },
        limits={
            'open_issues_cap': 20,
            'open_prs_cap': 5,
            'max_prs_per_run': 2,
            'max_issues_per_run': 10,
            'max_files_changed': 5,
            'max_loc_diff': 200,
            'max_fix_attempts': 3,
        },
    )
    
    # Create repo in registry
    repo = registry.create(ky_config)
    print(f"   ✅ Created repo: {repo.config.name}")
    
    # Migrate state files
    print("\n📦 Migrating state files...")
    new_state_dir = qa_agent_dir / 'repos' / 'ky' / 'state'
    new_state_dir.mkdir(parents=True, exist_ok=True)
    
    # File mappings (old -> new)
    file_mappings = [
        ('ky-findings.jsonl', 'findings.jsonl'),
        ('ky-issues.json', 'issues.json'),
        ('ky-state.json', 'state.json'),
    ]
    
    migrated_files = []
    for old_name, new_name in file_mappings:
        old_file = old_state_dir / old_name
        if old_file.exists():
            new_file = new_state_dir / new_name
            shutil.copy2(old_file, new_file)
            migrated_files.append(new_name)
            print(f"   ✅ Migrated: {old_name} -> {new_name}")
        else:
            print(f"   ⚠️  Not found: {old_name}")
    
    # Load existing findings and calculate health
    print("\n📊 Calculating health from existing findings...")
    findings = state.load_findings('ky')
    print(f"   Findings loaded: {len(findings)}")
    
    if findings:
        health_score = health.calculate(findings)
        print(f"   Health score: {health_score.score}/100 ({health_score.band})")
        
        # Create baseline from existing state
        baseline = health.create_baseline(
            repo_id=ky_config.id,
            findings=findings,
            health=health_score,
            findings_file=str(state.get_findings_file('ky'))
        )
        state.save_baseline('ky', baseline.to_dict())
        print(f"   ✅ Baseline created: {baseline.id}")
        
        # Load existing state for counters
        old_state_file = old_state_dir / 'ky-state.json'
        if old_state_file.exists():
            with open(old_state_file) as f:
                old_state = json.load(f)
            
            open_issues = old_state.get('open_issues', 0)
            open_prs = old_state.get('open_prs', 0)
            print(f"   Existing: {open_issues} open issues, {open_prs} open PRs")
        else:
            open_issues = 0
            open_prs = 0
        
        # Update registry status
        registry.update('ky', {
            'status': RepoStatus.READY.value,
            'onboarded_at': '2026-03-06T00:00:00+00:00',  # Approximate original date
            'current_findings_count': len(findings),
            'current_health_score': health_score.score,
        })
    else:
        print("   ⚠️  No findings found, setting default values")
        registry.update('ky', {
            'status': RepoStatus.READY.value,
            'onboarded_at': datetime.now(timezone.utc).isoformat(),
        })
    
    # Summary
    print("\n" + "=" * 60)
    print("Migration Complete!")
    print("=" * 60)
    print(f"\n✅ Repository: ky")
    print(f"   Path: {ky_repo}")
    print(f"   Config: {qa_agent_dir / 'repos' / 'ky' / 'config.yaml'}")
    print(f"   State: {new_state_dir}")
    print(f"\n📊 Stats:")
    print(f"   Findings: {len(findings)}")
    if findings:
        print(f"   Health: {health_score.score:.1f}/100 ({health_score.band})")
    print(f"   Files migrated: {len(migrated_files)}")
    
    return 0


if __name__ == '__main__':
    sys.exit(migrate_ky())
