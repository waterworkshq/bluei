#!/usr/bin/env python3
"""Migration script to move existing sandbox repo to new QA Agent structure."""

import sys
import shutil
from pathlib import Path
from datetime import datetime, timezone

# Add parent directory to path for qa_agent imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from qa_agent.config import ConfigManager
from qa_agent.registry import RepoRegistry
from qa_agent.health import HealthEngine
from qa_agent.state import StateManager
from qa_agent.models import RepoConfig, RepoStatus


def migrate_sandbox():
    """Migrate the existing sandbox repo to new agent structure."""
    
    workspace = Path.home() / '.openclaw' / 'workspace'
    qa_agent_dir = workspace / 'qa-agent'
    old_pr_automation = workspace / 'pr-automation'
    sandbox_repo = workspace / 'qa-sandbox-repo'
    
    if not sandbox_repo.exists():
        print(f"❌ Sandbox repo not found: {sandbox_repo}")
        return 1
    
    print("=" * 60)
    print("Migrating qa-sandbox to QA Agent")
    print("=" * 60)
    
    # Setup components
    config = ConfigManager(qa_agent_dir)
    registry = RepoRegistry(config)
    health = HealthEngine()
    state = StateManager(config.repos_dir)
    
    # Create config for existing sandbox
    print("\n📋 Creating repository configuration...")
    sandbox_config = RepoConfig(
        id="repo-qa-sandbox",
        name="qa-sandbox",
        path=str(sandbox_repo),
        language="python",
        framework=None,
        enabled=True,
        plugin_id="plugin-python",
        github={
            'live_actions': True,
            'auto_merge': True,  # Sandbox only
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
    repo = registry.create(sandbox_config)
    print(f"   ✅ Created repo: {repo.config.name}")
    
    # Migrate state files
    print("\n📦 Migrating state files...")
    old_state_dir = old_pr_automation / 'state'
    new_state_dir = qa_agent_dir / 'repos' / 'qa-sandbox' / 'state'
    new_state_dir.mkdir(parents=True, exist_ok=True)
    
    # File mappings (old -> new)
    file_mappings = [
        ('qa_findings.jsonl', 'findings.jsonl'),
        ('qa_issues.json', 'issues.json'),
        ('sandbox-state.json', 'state.json'),
        ('docs_index.json', 'docs_index.json'),
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
    
    # Create baseline from current state
    print("\n📸 Creating baseline...")
    findings = state.load_findings('qa-sandbox')
    if findings:
        health_score = health.calculate(findings)
        baseline = health.create_baseline(
            repo_id=sandbox_config.id,
            findings=findings,
            health=health_score,
            findings_file=str(state.get_findings_file('qa-sandbox'))
        )
        state.save_baseline('qa-sandbox', baseline.to_dict())
        print(f"   ✅ Baseline created: {baseline.id}")
        print(f"   📊 Initial health score: {health_score.score}/100")
    
    # Update registry status
    print("\n🔄 Updating repository status...")
    registry.update('qa-sandbox', {
        'status': RepoStatus.READY.value,
        'onboarded_at': '2026-02-17T00:00:00+00:00',  # Original creation date
        'current_findings_count': len(findings),
        'current_health_score': health_score.score if findings else 0.0,
    })
    
    # Summary
    print("\n" + "=" * 60)
    print("Migration Complete!")
    print("=" * 60)
    print(f"\n✅ Repository: qa-sandbox")
    print(f"   Path: {sandbox_repo}")
    print(f"   Config: {qa_agent_dir / 'repos' / 'qa-sandbox' / 'config.yaml'}")
    print(f"   State: {new_state_dir}")
    print(f"\n📊 Stats:")
    print(f"   Findings: {len(findings)}")
    print(f"   Health: {health_score.score:.1f}/100" if findings else "   Health: N/A")
    print(f"   Files migrated: {len(migrated_files)}")
    
    print(f"\n🚀 Next steps:")
    print(f"   Check status: ./qa-agent status")
    print(f"   View details: ./qa-agent repos show qa-sandbox")
    print(f"   Run QA: ./qa-agent run --repo qa-sandbox --no-dry-run")
    
    return 0


if __name__ == '__main__':
    sys.exit(migrate_sandbox())
