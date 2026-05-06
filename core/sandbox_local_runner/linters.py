"""sandbox_local_runner.linters - Ruff rule cache, xo linter runner, and discovery functions."""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .constants import DETECTOR_CATALOG
from .models import Finding, stable_finding_id
from .state import _append_text
from .utils import run_capture


# Cache for ruff rule descriptions
_Ruff_Rule_Descriptions = {}  # Cache for ruff rule descriptions


# DEAD CODE - verify before using
# This function is defined and cached but never called anywhere in the codebase.
def _get_ruff_rule_description(rule: str) -> str:
    """Fetch the ruff rule description. Cached per rule."""
    if rule in _Ruff_Rule_Descriptions:
        return _Ruff_Rule_Descriptions[rule]

    # Try full rule name (RUF007, F401) first, then strip prefix
    for name in (rule, rule.split('.')[-1] if '.' in rule else rule):
        r = run_capture(['ruff', 'rule', name], cwd=None)
        if r and len(r) > 10:
            _Ruff_Rule_Descriptions[rule] = r.strip()
            return r.strip()

    # Fallback: strip prefix and try again
    if '.' in rule:
        stripped = rule.split('.', 1)[-1]
        r = run_capture(['ruff', 'rule', stripped], cwd=None)
        if r and len(r) > 10:
            _Ruff_Rule_Descriptions[rule] = r.strip()
            return r.strip()

    _Ruff_Rule_Descriptions[rule] = f"[{rule}]"
    return _Ruff_Rule_Descriptions[rule]


def run_xo_linter_in_container(container_name: str = 'ky-phase2-dev') -> Tuple[int, List[Dict[str, Any]]]:
    """Run xo linter in Docker container and return parsed results.

    Returns:
        Tuple of (return_code, list of xo warning objects)
        Each warning object has: filePath, messages (list with ruleId, message, line, column, etc.)
    """
    try:
        res = subprocess.run(
            ['docker', 'exec', container_name, 'npx', 'xo'],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120
        )
        output = (res.stdout or '').strip()

        # If no output or empty, return empty list
        if not output:
            return res.returncode, []

        # Try to parse as JSON first (for newer xo versions)
        try:
            data = json.loads(output)
            if isinstance(data, list):
                return res.returncode, data
        except json.JSONDecodeError:
            pass

        # Parse text output format:
        # file:line:column
        #   ⚠  line:column  message              rule-id
        # OR (for subsequent files with leading whitespace):
        #   file:line:column
        #     ⚠  line:column  message              rule-id
        results: Dict[str, Dict[str, Any]] = {}
        current_file = None

        for line in output.split('\n'):
            line = line.rstrip()
            if not line:
                continue

            # Check if this is a warning line (contains ⚠ symbol)
            if '⚠' in line:
                # Parse warning line: "  ⚠  line:column  message              rule-id"
                stripped = line.strip()
                if stripped.startswith('⚠') and current_file:
                    rest = stripped[1:].strip()
                    parts = rest.split(None, 1)
                    if len(parts) >= 2:
                        line_col = parts[0]
                        message_and_rule = parts[1]

                        # Parse line:column
                        lc_parts = line_col.split(':')
                        line_num = int(lc_parts[0]) if lc_parts[0].isdigit() else 0
                        col_num = int(lc_parts[1]) if len(lc_parts) > 1 and lc_parts[1].isdigit() else 0

                        # Extract rule ID from the end (last word)
                        msg_parts = message_and_rule.rsplit(None, 1)
                        message = msg_parts[0] if len(msg_parts) > 1 else message_and_rule
                        rule_id = msg_parts[1] if len(msg_parts) > 1 else ''

                        results[current_file]['messages'].append({
                            'ruleId': rule_id,
                            'message': message,
                            'line': line_num,
                            'column': col_num,
                            'severity': 1  # warning
                        })
            else:
                # This might be a file path line (with or without leading whitespace)
                # Format: "file:line:column" or "  file:line:column"
                stripped = line.strip()
                # Check if it looks like a file path (contains : and doesn't start with digit)
                if ':' in stripped and not stripped[0].isdigit():
                    # This is likely a file path line
                    parts = stripped.split(':')
                    if len(parts) >= 2:
                        potential_file = parts[0]
                        # Verify it's not a message (file paths don't contain spaces typically)
                        if ' ' not in potential_file:
                            current_file = potential_file
                            if current_file not in results:
                                results[current_file] = {
                                    'filePath': current_file,
                                    'messages': []
                                }

        return res.returncode, list(results.values())

    except subprocess.TimeoutExpired:
        return 1, []
    except Exception:
        return 1, []


def discover_xo_linter_findings(repo_path: Path, log_file: Path) -> List[Finding]:
    """Discover findings from xo linter for TypeScript/JavaScript repos.

    This runs xo linter in a Docker container (for ky repo) or directly on the host
    and converts warnings to findings.
    """
    findings: List[Finding] = []

    # Only run xo for repos that have xo configured (check for xo in package.json or .xorc)
    package_json = repo_path / 'package.json'
    if not package_json.exists():
        return findings

    # Check if xo is configured in this repo
    try:
        pkg = json.loads(package_json.read_text(encoding='utf-8'))
        has_xo = 'xo' in pkg.get('devDependencies', {}) or 'xo' in pkg.get('dependencies', {})
        if not has_xo:
            return findings
    except Exception:
        return findings

    # Determine how to run xo based on repo
    is_ky_repo = 'ky' in str(repo_path).lower()
    if is_ky_repo:
        # Use Docker container for ky repo
        container_name = 'ky-phase2-dev'
        _append_text(log_file, f'xo-discovery: running xo linter in container {container_name}')
        rc, xo_results = run_xo_linter_in_container(container_name)
    else:
        # Try to run xo directly on the host via npx
        _append_text(log_file, 'xo-discovery: running xo linter via npx')
        try:
            res = subprocess.run(
                ['npx', 'xo', '--format=json'],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
                cwd=str(repo_path)
            )
            output = (res.stdout or '').strip()
            if output:
                try:
                    xo_results = json.loads(output)
                    rc = res.returncode
                except json.JSONDecodeError:
                    rc, xo_results = 1, []
            else:
                rc, xo_results = res.returncode, []
        except Exception as e:
            _append_text(log_file, f'xo-discovery: npx xo failed: {e}')
            return findings

    if rc != 0 and not xo_results:
        _append_text(log_file, f'xo-discovery: xo linter failed with rc={rc}')
        return findings

    rule_meta = {entry['rule']: entry for entry in DETECTOR_CATALOG}

    # Process xo results
    total_warnings = 0
    for result in xo_results:
        file_path = str(result.get('filePath', ''))
        messages = result.get('messages', [])

        for msg in messages:
            rule_id = str(msg.get('ruleId', ''))
            message_text = str(msg.get('message', ''))
            line = int(msg.get('line', 0))
            column = int(msg.get('column', 0))
            severity = int(msg.get('severity', 1))  # 1=warning, 2=error

            # Skip if no rule ID
            if not rule_id:
                continue

            # Map xo rule IDs to our finding rule names
            rule_mapping = {
                'max-lines': 'xo-max-lines',
                'no-warning-comments': 'xo-no-warning-comments',
                'complexity': 'xo-complexity',
            }

            finding_rule = rule_mapping.get(rule_id, f'xo-{rule_id}')

            # Check if this rule is in our catalog
            if finding_rule not in rule_meta:
                # Add a generic entry for unmapped xo rules
                rule_meta[finding_rule] = {
                    'rule': finding_rule,
                    'category': 'lint',
                    'confidence': 0.75,
                    'autofix': False
                }

            meta = rule_meta.get(finding_rule, {})
            confidence = float(meta.get('confidence', 0.75))
            safe_to_autofix = bool(meta.get('autofix', False))

            # Make file path relative to repo
            if file_path.startswith('/app/'):
                relative_path = file_path[5:]  # Remove /app/ prefix
            elif file_path.startswith(str(repo_path)):
                relative_path = str(Path(file_path).relative_to(repo_path))
            else:
                relative_path = file_path

            snippet = f"{message_text} (line {line}, col {column})"

            finding = Finding(
                finding_id=stable_finding_id(str(repo_path), relative_path, line, finding_rule, snippet),
                repo=str(repo_path),
                path=relative_path,
                line=line,
                rule=finding_rule,
                snippet=snippet,
                confidence=confidence,
                quick_win=severity == 1 and safe_to_autofix,
                safe_to_autofix=safe_to_autofix,
            )
            findings.append(finding)
            total_warnings += 1

    _append_text(log_file, f'xo-discovery: found {total_warnings} xo warnings, converted to {len(findings)} findings')

    return findings


def discover_python_linter_findings(repo_path: Path, log_file: Path) -> List[Finding]:
    """Discover issues from Python linter (ruff) for Python repos.

    This runs ruff linter and converts warnings to findings.
    Only works for repos with ruff installed.
    """
    findings: List[Finding] = []

    # Check if ruff is available
    ruff_path = None
    for candidate in ['ruff', '~/.local/bin/ruff', '/home/vikas/.local/bin/ruff']:
        expanded = Path(candidate).expanduser()
        if expanded.exists():
            ruff_path = str(expanded)
            break

    if not ruff_path:
        # Try to find ruff in PATH
        try:
            res = subprocess.run(['which', 'ruff'], capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout.strip():
                ruff_path = res.stdout.strip()
        except Exception:
            pass

    if not ruff_path:
        _append_text(log_file, 'python-discovery: ruff not found, skipping')
        return findings

    # Check if this is a Python repo (has .py files)
    py_files = list(repo_path.rglob('*.py'))
    if not py_files:
        return findings

    _append_text(log_file, f'python-discovery: running ruff linter on {len(py_files)} Python files')

    try:
        # Run ruff with selected rules:
        # B=bugbear, E/W=pycodestyle, F=pyflakes, S=bandit/security, C4=comprehensions
        # Use JSON output to get per-instance fix applicability (not just rule-level catalog)
        res = subprocess.run(
            [ruff_path, 'check', str(repo_path), '--select=B,E,W,F,S,C4', '--output-format=json'],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
            cwd=str(repo_path)
        )
        raw = (res.stdout or '').strip()
    except subprocess.TimeoutExpired:
        _append_text(log_file, 'python-discovery: ruff timed out')
        return findings
    except Exception as e:
        _append_text(log_file, f'python-discovery: ruff failed: {e}')
        return findings

    if not raw or raw == 'null' or raw == '[]':
        _append_text(log_file, 'python-discovery: no ruff findings')
        return findings

    rule_meta = {entry['rule']: entry for entry in DETECTOR_CATALOG}
    seen_finding_ids: set[str] = set()

    try:
        ruff_results = json.loads(raw)
    except json.JSONDecodeError:
        _append_text(log_file, f'python-discovery: ruff JSON parse failed, falling back to concise')
        return findings

    for result in ruff_results:
        try:
            code = result.get('code', '')
            rule_name = f'ruff-{code.lower()}'

            if rule_name not in rule_meta:
                continue

            meta = rule_meta[rule_name]
            confidence = float(meta.get('confidence', 0.75))

            # Determine autofix safety from ruff's per-instance fix applicability.
            # This correctly overrides the blanket catalog value:
            # - "safe"      -> ruff will apply safely
            # - "unsafe"    -> ruff marks it unsafe (e.g. migration dict() rewrites)
            # - "generated" -> fix produces generated code, treat as safe
            # - null         -> ruff has no fix for this specific instance
            fix_obj = result.get('fix')
            if fix_obj is not None:
                applicability = fix_obj.get('applicability')
                safe_to_autofix = applicability in ('safe', 'generated')
            else:
                safe_to_autofix = False

            # Hardcoded override: ruff-c408 (dict() → {} rewrite) often reports as
            # safe/autofixable but ruff --fix fails in practice because the rewriting
            # requires surrounding context. Force it to NOT autofix so it routes to
            # the LLM fix engine instead.
            if rule_name == 'ruff-c408':
                safe_to_autofix = False

            location = result.get('location', {})
            line_num = int(location.get('row', 0))

            file_rel = str(result.get('filename', ''))
            try:
                file_path = str(Path(file_rel).relative_to(repo_path))
            except ValueError:
                file_path = file_rel

            message = result.get('message', '')[:200]

            finding_id = stable_finding_id(
                str(repo_path), file_path, line_num, rule_name, message
            )

            if finding_id in seen_finding_ids:
                continue
            seen_finding_ids.add(finding_id)

            findings.append(Finding(
                finding_id=finding_id,
                repo=str(repo_path),
                path=file_path,
                line=line_num,
                rule=rule_name,
                snippet=message,
                confidence=confidence,
                quick_win=safe_to_autofix,
                safe_to_autofix=safe_to_autofix,
            ))
        except (ValueError, IndexError, TypeError, KeyError):
            continue

    _append_text(log_file, f'python-discovery: found {len(findings)} ruff findings')
    return findings


def discover_typescript_type_findings(repo_path: Path, log_file: Path) -> List[Finding]:
    """Discover type safety issues in TypeScript files.

    Uses TypeScript compiler in strict mode to detect:
    - Explicit `any` usage
    - Missing return type annotations
    - Missing parameter type annotations
    - Untyped imports / modules with missing declarations
    """
    findings: List[Finding] = []
    rule_meta = {entry['rule']: entry for entry in DETECTOR_CATALOG}

    # Check if this is a TypeScript repo
    ts_config = repo_path / 'tsconfig.json'
    if not ts_config.exists():
        _append_text(log_file, 'type-discovery: no tsconfig.json found, skipping')
        return findings

    # Run TypeScript compiler to check for type issues
    # We look for common type safety patterns in the output
    try:
        # First, try to run tsc with --noEmit to check for type errors
        rc, output = run_capture(
            ['npx', 'tsc', '--noEmit', '--strict', '--pretty', 'false'],
            cwd=repo_path
        )

        # Parse TypeScript compiler output for type issues
        # TypeScript outputs errors in format: file(line,col): error TSxxxx: message
        type_error_pattern = re.compile(
            r'^(.+?)\((\d+),(\d+)\):\s*error\s+(TS\d+):\s*(.+)$',
            re.MULTILINE
        )

        for match in type_error_pattern.finditer(output):
            file_path, line, col, error_code, message = match.groups()

            # Map TypeScript error codes to our rules
            rule = None
            snippet = message[:200]

            if (
                error_code == 'TS7016'
                or ('could not find a declaration file for module' in message.lower())
                or ('implicitly has an any type' in message.lower() and 'module' in message.lower())
            ):
                rule = 'type-untyped-import'
            elif 'any' in message.lower() or error_code in ['TS7005', 'TS7006']:
                rule = 'type-explicit-any'
            elif 'return type' in message.lower() or error_code == 'TS7010':
                rule = 'type-missing-return'
            elif 'parameter' in message.lower() and 'implicitly' in message.lower():
                rule = 'type-missing-param'

            if rule and rule in rule_meta:
                meta = rule_meta[rule]
                relative_path = file_path.replace(str(repo_path) + '/', '')

                finding = Finding(
                    finding_id=stable_finding_id(str(repo_path), relative_path, int(line), rule, snippet),
                    repo=str(repo_path),
                    path=relative_path,
                    line=int(line),
                    rule=rule,
                    snippet=snippet,
                    confidence=float(meta.get('confidence', 0.7)),
                    quick_win=meta.get('autofix', False),
                    safe_to_autofix=meta.get('autofix', False),
                )
                findings.append(finding)

        _append_text(log_file, f'type-discovery: found {len(findings)} type safety findings')

    except Exception as e:
        _append_text(log_file, f'type-discovery: error running tsc: {e}')

    return findings


def discover_test_coverage_findings(repo_path: Path, log_file: Path) -> List[Finding]:
    """Discover test coverage gaps using coverage reports.

    Uses coverage tools to detect:
    - Uncovered branches
    - Uncovered functions
    - Uncovered lines (for smaller gaps only)
    """
    findings: List[Finding] = []
    rule_meta = {entry['rule']: entry for entry in DETECTOR_CATALOG}

    # Check for coverage report or run coverage
    coverage_file = repo_path / 'coverage' / 'coverage-final.json'
    lcov_file = repo_path / 'coverage' / 'lcov.info'

    # If no coverage file exists, try to generate one
    if not coverage_file.exists() and not lcov_file.exists():
        _append_text(log_file, 'coverage-discovery: no coverage file found, attempting to generate')
        try:
            # Try nyc/c8 for Node.js projects
            if (repo_path / 'package.json').exists():
                rc, _ = run_capture(
                    ['npx', 'nyc', '--reporter=json', 'npm', 'test'],
                    cwd=repo_path,
                    timeout=120
                )
        except Exception as e:
            _append_text(log_file, f'coverage-discovery: failed to generate coverage: {e}')

    # Parse coverage report
    if coverage_file.exists():
        try:
            import json
            with open(coverage_file) as f:
                coverage_data = json.load(f)

            for file_path, file_coverage in coverage_data.items():
                # Skip node_modules and test files
                if 'node_modules' in file_path or file_path.endswith('.test.ts'):
                    continue

                relative_path = file_path.replace(str(repo_path) + '/', '')

                # Check for uncovered branches
                branches = file_coverage.get('branches', {})
                if branches:
                    for branch_id, hits in branches.items():
                        if hits == 0:
                            line = int(branch_id.split(',')[0]) if ',' in str(branch_id) else 1
                            finding = Finding(
                                finding_id=stable_finding_id(str(repo_path), relative_path, line, 'test-coverage-branch', f'Branch not covered'),
                                repo=str(repo_path),
                                path=relative_path,
                                line=line,
                                rule='test-coverage-branch',
                                snippet='Branch not covered by tests',
                                confidence=0.82,
                                quick_win=True,
                                safe_to_autofix=True,
                            )
                            findings.append(finding)
                            break  # One finding per file to avoid spam

                # Check for uncovered functions
                functions = file_coverage.get('functions', {})
                if functions:
                    for func_name, func_data in functions.items():
                        if isinstance(func_data, dict):
                            hits = func_data.get('hits', 1)
                            line = func_data.get('line', 1)
                        else:
                            hits = func_data
                            line = 1

                        if hits == 0:
                            finding = Finding(
                                finding_id=stable_finding_id(str(repo_path), relative_path, line, 'test-coverage-function', f'Function {func_name} not covered'),
                                repo=str(repo_path),
                                path=relative_path,
                                line=line,
                                rule='test-coverage-function',
                                snippet=f'Function {func_name} not covered by tests',
                                confidence=0.80,
                                quick_win=True,
                                safe_to_autofix=True,
                            )
                            findings.append(finding)
                            break  # One finding per file

                # Check for uncovered lines, but keep it coarse to avoid spam
                statement_map = file_coverage.get('statementMap', {})
                statement_hits = file_coverage.get('s', {})
                if statement_map and statement_hits:
                    for statement_id, hits in statement_hits.items():
                        if hits != 0:
                            continue
                        location = statement_map.get(str(statement_id)) or statement_map.get(statement_id)
                        if not isinstance(location, dict):
                            continue
                        start = location.get('start') if isinstance(location.get('start'), dict) else {}
                        line = int(start.get('line') or 1)
                        finding = Finding(
                            finding_id=stable_finding_id(str(repo_path), relative_path, line, 'test-coverage-line', 'Line not covered'),
                            repo=str(repo_path),
                            path=relative_path,
                            line=line,
                            rule='test-coverage-line',
                            snippet='Line not covered by tests',
                            confidence=0.78,
                            quick_win=False,
                            safe_to_autofix=False,
                        )
                        findings.append(finding)
                        break  # One line-coverage finding per file

            _append_text(log_file, f'coverage-discovery: found {len(findings)} coverage findings')

        except Exception as e:
            _append_text(log_file, f'coverage-discovery: error parsing coverage: {e}')
    else:
        _append_text(log_file, 'coverage-discovery: no coverage data available')

    return findings
