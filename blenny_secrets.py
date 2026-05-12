#!/usr/bin/env python3
"""blenny secrets — Standalone secret scanning using gitleaks.

Usage:
  blenny secrets <repo-path>
  blenny secrets --repo <repo-path> --format json

Scans a repository for hardcoded secrets (API keys, tokens, passwords, etc.)
using gitleaks. Reports findings in human-readable or JSON format.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def find_tool(binary: str) -> str:
    """Find a tool binary in common locations."""
    candidates = [
        binary,
        f'~/.local/bin/{binary}',
        f'/home/vikas/.local/bin/{binary}',
    ]
    for c in candidates:
        p = Path(c).expanduser()
        if p.exists():
            return str(p)
    try:
        res = subprocess.run(['which', binary], capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    sys.exit(f"Error: {binary} not found. Install it first:\n"
             f"  brew install gitleaks\n"
             f"  or:  go install github.com/gitleaks/gitleaks/v8@latest\n"
             f"  or:  https://github.com/gitleaks/gitleaks/releases")


def scan_secrets(repo_path: Path, format: str = 'text', verbose: bool = False) -> list:
    """Run gitleaks detection on a repository path."""
    repo_path = repo_path.resolve()
    if not repo_path.is_dir():
        sys.exit(f"Error: not a directory: {repo_path}")

    gitleaks_path = find_tool('gitleaks')

    tmp_report = Path(tempfile.mktemp(suffix='.json'))
    try:
        res = subprocess.run(
            [gitleaks_path, 'detect', '--no-git', '--source', str(repo_path),
             '--report-format', 'json', '--report-path', str(tmp_report)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=300, cwd=str(repo_path),
        )
        if verbose:
            print(res.stdout, file=sys.stderr)

        if tmp_report.exists() and tmp_report.stat().st_size > 0:
            raw = tmp_report.read_text().strip()
        else:
            return []
    finally:
        try:
            tmp_report.unlink(missing_ok=True)
        except Exception:
            pass

    if not raw or raw == 'null' or raw == '[]':
        return []

    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        return [data]
    except json.JSONDecodeError:
        return []


def format_text(findings: list) -> str:
    """Format findings as human-readable text."""
    if not findings:
        return "No secrets found. ✨"

    lines = []
    lines.append(f"Found {len(findings)} potential secret(s):")
    lines.append("")
    for i, f in enumerate(findings, 1):
        desc = f.get('Description', 'Unknown')
        file_path = f.get('File', '?')
        line = f.get('StartLine', f.get('line', '?'))
        rule_id = f.get('RuleID', f.get('rule', '?'))
        severity = f.get('Severity', 'medium')
        match = (f.get('Match', '') or '')[:60]

        lines.append(f"  {i}. [{severity.upper()}] {desc}")
        lines.append(f"     File: {file_path}:{line}")
        lines.append(f"     Rule: {rule_id}")
        if match:
            lines.append(f"     Match: {match}")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='Scan a repository for hardcoded secrets.',
    )
    parser.add_argument('repo', nargs='?', help='Path to the repository')
    parser.add_argument('--repo', '-r', dest='repo_flag', help='Path to the repository')
    parser.add_argument('--format', '-f', choices=['text', 'json'], default='text',
                        help='Output format (default: text)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show gitleaks output')

    args = parser.parse_args()

    repo_path = args.repo or args.repo_flag
    if not repo_path:
        parser.print_help()
        sys.exit(1)

    findings = scan_secrets(Path(repo_path), args.format, args.verbose)

    if args.format == 'json':
        print(json.dumps(findings, indent=2))
    else:
        print(format_text(findings))

    # Exit non-zero if secrets found
    if findings:
        sys.exit(1)


if __name__ == '__main__':
    main()
