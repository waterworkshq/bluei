#!/usr/bin/env python3
"""One-time purge: remove zombie ruff-c408 findings in Django migration files.

These findings live in per-repo state/issues.json and have accumulated
58+ failed fix attempts because migration files use dict() for runtime
model field resolution and can NEVER be rewritten.

The discovery-side suppression (linters.py, SHA 306b3ee) stops NEW ones
from being created, but existing entries still cycle through escalation
checks and trigger reappearing_finding + dedup_saturation alerts.

This script purges them directly from the per-repo issue stores.
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
REPOS_DIR = BASE_DIR / "repos"

def find_issue_stores() -> list[Path]:
    """Find all repos/*/state/issues.json files."""
    stores = []
    for repo_dir in sorted(REPOS_DIR.iterdir()):
        issue_file = repo_dir / "state" / "issues.json"
        if issue_file.exists():
            stores.append(issue_file)
    return stores


def is_migration_c408_zombie(issue: dict) -> bool:
    """Check if an issue is a ruff-c408 finding in a migration file."""
    return (issue.get("rule") == "ruff-c408" and 
            "/migrations/" in issue.get("path", ""))


def purge(dry_run: bool = True) -> int:
    """Remove migration C408 zombies from all per-repo issue stores.
    
    Returns total purged count.
    """
    stores = find_issue_stores()
    print(f"Found {len(stores)} issue stores\n")
    
    total_purged = 0
    all_results = []
    
    for store in stores:
        data = json.loads(store.read_text())
        issues = data.get("issues", [])
        
        zombies = [i for i in issues if is_migration_c408_zombie(i)]
        survivors = [i for i in issues if not is_migration_c408_zombie(i)]
        
        if zombies:
            total_purged += len(zombies)
            all_results.append({
                "store": str(store.relative_to(BASE_DIR)),
                "purged": len(zombies),
                "before": len(issues),
                "after": len(survivors),
            })
            
            if dry_run:
                print(f"  WOULD PURGE {len(zombies)} from {store.relative_to(BASE_DIR)} "
                      f"({len(issues)} → {len(survivors)})")
                for z in zombies[:3]:
                    fa = sum(1 for h in z.get("history", []) 
                            if h.get("event") == "fix_failed_verification")
                    print(f"    {z.get('finding_id','?')[:20]}... | {z.get('path')} "
                          f"(failed={fa})")
            else:
                data["issues"] = survivors
                store.write_text(json.dumps(data, indent=2) + "\n")
                print(f"  PURGED {len(zombies)} from {store.relative_to(BASE_DIR)} "
                      f"({len(issues)} → {len(survivors)})")
    
    print(f"\n{'[DRY RUN] ' if dry_run else ''}"
          f"Total: {total_purged} migration-C408 zombies "
          f"{'would be ' if dry_run else ''}removed")
    
    if not dry_run and total_purged > 0:
        log_entry = {
            "event": "purge_migration_c408",
            "total_purged": total_purged,
            "stores_affected": len(all_results),
            "results": all_results,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        log_path = BASE_DIR / "state" / "cleanup_log.jsonl"
        with open(log_path, "a") as f:
            f.write(json.dumps(log_entry, default=str) + "\n")
        print(f"\nLogged to {log_path.relative_to(BASE_DIR)}")
    
    return total_purged


if __name__ == "__main__":
    dry_run = "--execute" not in sys.argv
    count = purge(dry_run=dry_run)
    
    if dry_run:
        print("\nRun with --execute to apply.")
    sys.exit(0 if count >= 0 else 1)
