#!/usr/bin/env python3
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone

def stable_finding_id(repo, path, line, rule, snippet):
    material = f'{repo}|{path}|{line}|{rule}|{snippet.strip()}'
    return hashlib.sha256(material.encode('utf-8')).hexdigest()

# Parse GitHub issue body to extract finding details
def parse_issue_body(body, repo):
    # Extract path and line from Current State section (may be in code block)
    current_state_match = re.search(r'## Current State\s*\n(?:```\n)?(.+?)(?:```\n|\n##|\Z)', body, re.DOTALL)
    if not current_state_match:
        return None
    
    state_text = current_state_match.group(1).strip()
    
    # Try to parse: 'source/core/Ky.ts:381:2  snippet  rule'
    parts = state_text.split(None, 1)
    if not parts:
        return None
    
    path_line_part = parts[0]
    rest = parts[1] if len(parts) > 1 else ''
    
    # Parse path:line:line_num or path:line
    path_match = re.match(r'^(.+?):(\d+):\d*$', path_line_part)
    if not path_match:
        path_match = re.match(r'^(.+?):(\d+)$', path_line_part)
    
    if not path_match:
        return None
    
    path = path_match.group(1)
    line = int(path_match.group(2))
    
    # Extract rule from the end (supports hyphenated rules like max-lines)
    rule_match = re.search(r'\s+([\w-]+)$', rest)
    rule = rule_match.group(1) if rule_match else 'unknown'
    
    # Snippet is everything between path:line and rule
    snippet = rest.rsplit(None, 1)[0] if rule_match else rest
    
    return {
        'path': path,
        'line': line,
        'rule': rule,
        'snippet': snippet.strip(),
    }

def main():
    repo = '/home/vikas/.openclaw/workspace/phase2/ky'
    repo_slug = 'vikasagarwal101/ky'
    issues_file = '/home/vikas/.openclaw/workspace/qa-agent/repos/ky/state/issues.json'
    
    # Fetch all open issues from GitHub
    result = subprocess.run(
        ['gh', 'issue', 'list', '--repo', repo_slug, '--state', 'open', '--json', 'number,title,body,createdAt'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"Error fetching issues: {result.stderr}")
        return
    
    issues = json.loads(result.stdout)
    print(f"Found {len(issues)} open GitHub issues")
    
    rebuilt_issues = []
    for issue in issues:
        parsed = parse_issue_body(issue['body'], repo)
        if parsed:
            fid = stable_finding_id(repo, parsed['path'], parsed['line'], parsed['rule'], parsed['snippet'])
            rebuilt_issues.append({
                'id': f'QA-{issue["number"]:04d}',
                'finding_id': fid,
                'github': {
                    'issue_number': issue['number'],
                    'issue_url': f'https://github.com/{repo_slug}/issues/{issue["number"]}'
                },
                'path': parsed['path'],
                'line': parsed['line'],
                'rule': parsed['rule'],
                'snippet': parsed['snippet'],
                'status': 'open',
                'confidence': 0.8,
                'safe_to_autofix': True,
                'quick_win': False,
                'repo': repo,
                'created_at': issue['createdAt']
            })
            print(f'  Issue #{issue["number"]}: {parsed["rule"]} - {parsed["path"]}:{parsed["line"]}')
        else:
            print(f'  Issue #{issue["number"]}: Could not parse')
    
    # Write rebuilt issues.json
    output = {
        'issues': rebuilt_issues,
        'last_updated': datetime.now(timezone.utc).isoformat(),
        'repo': repo_slug
    }
    
    with open(issues_file, 'w') as f:
        json.dump(output, f, indent=2)
    
    print(f'\nRebuilt {len(rebuilt_issues)} issues in {issues_file}')

if __name__ == '__main__':
    main()