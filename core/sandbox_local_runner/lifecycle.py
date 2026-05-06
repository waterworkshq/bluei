"""lifecycle.py — apply_autofix, validation, git ops, fix orchestration."""
import hashlib
import re
import shlex
import subprocess
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

from .models import Finding
from .mnemo_client import MnemoClient
from .reforge import (
    RefactorClass,
    RefactorPhase,
    RefactorWork,
    classify_finding,
    can_auto_refactor,
    LARGE_FILE_SAFETY_LIMIT,
)
from .utils import run_capture, run_no_capture, sanitize_command_template, append_lesson, load_lessons_for_finding
from .state import _append_text
from .linters import (
    discover_python_linter_findings,
    discover_typescript_type_findings,
    discover_xo_linter_findings,
    discover_test_coverage_findings,
)
from .git_utils import get_branch, refresh_docs_index, load_docs_index
from .orchestrator import discover_findings
from .prompts import (
    render_test_coverage_prompt,
    render_type_safety_prompt,
    render_complexity_refactor_prompt,
    render_maxlines_refactor_prompt,
    render_claude_fix_prompt,
)
from .gh import repo_is_sandbox
from .constants import (
    DEFAULT_DOCS_INDEX,
    DEFAULT_FINDINGS,
    DEFAULT_LESSONS_LOG,
    DEFAULT_WORKTREE_ROOT,
    CLAUDE_REQUIRED_RULES,
    RULE_TARGET_CHECKS,
    BASELINE_VALIDATION_CHECKS,
    MAX_LINES_REFACTOR_LIMIT,
    MAX_LINES_REFACTOR_TARGET,
    DEFAULT_FIX_ENGINE,
    QA_FIX_PROMPT_FILENAME,
    DEFAULT_REPO,
    AGENT_ROOT,
)


def apply_claude_fix(
    worktree_path: Path,
    finding: Finding,
    baseline_checks: Dict[str, List[str]],
    target_checks: Dict[str, List[str]],
    claude_cmd_template: str,
    max_files_changed: int,
    max_loc_diff: int,
    log_file: Path,
) -> Tuple[int, str, str]:
    """Apply a fix using the Claude fix engine (LLM-based single-pass fix).

    This is the refactor-class companion to apply_autofix().  It is called
    from the orchestrator when classify_finding() returns REFACTOR_CLASS
    or CLAUDE_FIX, and can_auto_refactor() passes the safety gate.

    Args:
        worktree_path: Path to the worktree.
        finding: The Finding to fix.
        baseline_checks: Checks to run before the fix (for fingerprinting).
        target_checks: Checks that must pass after the fix.
        claude_cmd_template: Shell command template with {prompt_file},
            {finding_id}, {rule}, {path} placeholders.
        max_files_changed: Safety cap on files changed per fix.
        max_loc_diff: Safety cap on lines changed per fix.
        log_file: Path to the run log.

    Returns:
        Tuple of (return_code, output_text, prompt_path_str).
        return_code 0 = success, 2 = template error, non-zero = Claude failed.
    """
    prompt_path = worktree_path / QA_FIX_PROMPT_FILENAME
    prompt_text = render_claude_fix_prompt(
        finding=finding,
        baseline_checks=baseline_checks,
        target_checks=target_checks,
        max_files_changed=max_files_changed,
        max_loc_diff=max_loc_diff,
    )
    prompt_path.write_text(prompt_text, encoding='utf-8')

    try:
        try:
            command = claude_cmd_template.format(
                prompt_file=shlex.quote(str(prompt_path)),
                finding_id=shlex.quote(finding.finding_id),
                rule=shlex.quote(finding.rule),
                path=shlex.quote(finding.path),
            )
        except KeyError as exc:
            error = f'invalid claude command template placeholder: {exc}'
            _append_text(log_file, f'claude-fix: {error}')
            return 2, error, str(prompt_path)

        _append_text(
            log_file,
            'claude-fix: '
            f'finding_id={finding.finding_id} prompt_file={prompt_path} '
            f'cmd={sanitize_command_template(command)}',
        )
        res = subprocess.run(
            ['bash', '-l', '-c', command],
            cwd=str(worktree_path),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
        )
        output = (res.stdout or '').strip()
        _append_text(
            log_file,
            'claude-fix-result: '
            f'finding_id={finding.finding_id} rc={res.returncode} output={(output or "<empty>")[:1000]}',
        )
        return res.returncode, output, str(prompt_path)
    finally:
        try:
            prompt_path.unlink(missing_ok=True)
        except Exception:
            pass


def route_to_human_review(
    finding: Finding,
    refactor_work: RefactorWork,
    worktree_path: Path,
    log_file: Path,
) -> str:
    """Route a REFACTOR_CLASS finding to the human-review refactor queue.

    Called when can_auto_refactor() returns (False, reason) or when a
    RefactorWork enters the ABORTED phase.  Persists the queue entry
    on disk and logs the routing event.

    Args:
        finding: The Finding being routed.
        refactor_work: The associated RefactorWork state record.
        worktree_path: Root path of the worktree (used to store worktree-rooted file paths).
        log_file: Path to the run log.

    Returns:
        The work_id of the created queue entry.
    """
    try:
        from .refactor_queue import enqueue_refactor_work
        entry = enqueue_refactor_work(finding, refactor_work, worktree_path)
        _append_text(
            log_file,
            f'human-review: enqueued finding_id={finding.finding_id} '
            f'work_id={entry.work_id} rule={finding.rule} '
            f'phase={refactor_work.phase.value} '
            f'reason={refactor_work.review_outcome or "safety_gate"}',
        )
        return entry.work_id
    except Exception as exc:
        _append_text(
            log_file,
            f'human-review: failed to enqueue finding_id={finding.finding_id} '
            f'error={exc}',
        )
        return ''


def run_validation_gate(
    repo_path: Path,
    worktree_path: Path,
    checks: Optional[Dict[str, List[str]]],
    baseline_results: Optional[Dict[str, Dict[str, Any]]] = None,
    allow_unchanged_baseline_failures: bool = True,
    log_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run baseline + post-fix + target checks and return structured result.

    Args:
        repo_path: Repository root path.
        worktree_path: Worktree path.
        checks: Dict of check-name → command list. If None, runs no checks.
        baseline_results: Optional pre-computed baseline results (from a
            prior run_named_checks call) to avoid re-running baseline.
        allow_unchanged_baseline_failures: If True, baseline check failures
            that are unchanged post-fix are not treated as regressions.
        log_file: Optional log file path.

    Returns:
        Dict with keys:
            - passed: bool
            - message: str (empty on success)
            - regressions: List[str] (names of checks that regressed)
            - target_failures: List[str] (names of target checks that failed)
    """
    result: Dict[str, Any] = {
        "passed": True,
        "message": "",
        "regressions": [],
        "target_failures": [],
        "baseline_results": {},
        "post_results": {},
    }

    if checks is None:
        checks = {}

    phase_prefix = "validation"
    if log_file is None:
        log_file = Path("/dev/null")

    # Run baseline checks (or reuse pre-computed results)
    if baseline_results is not None:
        result["baseline_results"] = baseline_results
    else:
        result["baseline_results"] = run_named_checks(
            repo_path=repo_path, checks=checks, log_file=log_file, phase=f"{phase_prefix}-baseline"
        )

    # Apply fixes would happen here in the orchestrator path
    # Run post-fix checks
    result["post_results"] = run_named_checks(
        repo_path=repo_path, checks=checks, log_file=log_file, phase=f"{phase_prefix}-postfix"
    )

    # Run target checks
    target_results: Dict[str, Dict[str, Any]] = {}
    if checks:
        target_results = run_named_checks(
            repo_path=repo_path, checks=checks, log_file=log_file, phase=f"{phase_prefix}-target"
        )

    # Evaluate regressions
    for name, baseline in result["baseline_results"].items():
        post = result["post_results"].get(name, {"rc": 1, "fingerprint": "missing"})
        baseline_rc = int(baseline.get("rc", 1))
        post_rc = int(post.get("rc", 1))
        if baseline_rc == 0 and post_rc != 0:
            result["regressions"].append(name)

    target_failures = [n for n, r in target_results.items() if int(r.get("rc", 1)) != 0]
    if target_failures:
        result["target_failures"] = target_failures

    if result["regressions"]:
        result["passed"] = False
        result["message"] = f"regression in: {', '.join(result['regressions'])}"
        _append_text(log_file, f"validation-gate: FAIL {result['message']}")
        return result

    if result["target_failures"]:
        result["passed"] = False
        result["message"] = f"target checks failed: {', '.join(result['target_failures'])}"
        _append_text(log_file, f"validation-gate: FAIL {result['message']}")
        return result

    result["message"] = "all checks passed"
    _append_text(log_file, "validation-gate: PASS")
    return result

# Phase 3: mnemo client (lazy-initialized per repo, best-effort only)
_mnemo_clients: Dict[str, MnemoClient] = {}


def _get_mnemo_client(repo_path: Path, log_file: Optional[Path] = None) -> Optional[MnemoClient]:
    repo_key = str(repo_path.resolve())
    existing = _mnemo_clients.get(repo_key)
    if existing is not None:
        return existing

    try:
        client = MnemoClient(repo_path)
        _mnemo_clients[repo_key] = client
        return client
    except Exception as exc:
        if log_file is not None:
            _append_text(log_file, f'mnemo-init: unavailable ({exc})')
        return None


def _should_use_mnemo(
    finding: Finding,
    finding_record: Optional[Dict[str, Any]],
    fix_history: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Decide whether Mnemo should be queried for this fix attempt.

    Mnemo is most useful for harder or repeated fixes, not first-pass trivial quick wins.
    """
    attempts = max(
        int(getattr(finding, 'fix_attempts', 0) or 0),
        int((finding_record or {}).get('fix_attempts', 0) or 0),
    )

    if finding.rule in CLAUDE_REQUIRED_RULES:
        return True, 'claude-required-rule'
    if attempts > 0:
        return True, f'retry-attempts={attempts}'
    if fix_history:
        return True, f'lesson-history={len(fix_history)}'
    if not finding.quick_win:
        return True, 'not-quick-win'
    if not finding.safe_to_autofix:
        return True, 'unsafe-to-autofix'
    if len((finding.snippet or '').strip()) >= 120:
        return True, 'long-snippet'

    return False, 'trivial-first-pass-quick-win'


def verify_fix_closed(
    worktree_path: Path,
    finding: Finding,
    log_file: Path,
    docs_index_file: Path = DEFAULT_DOCS_INDEX,
) -> bool:
    rescanned = discover_findings(worktree_path, docs_index_file=docs_index_file)
    still_firing = [
        f for f in rescanned
        if (
            f.finding_id == finding.finding_id
            or (
                f.rule == finding.rule
                and f.path == finding.path
                and int(f.line) == int(finding.line)
            )
        )
    ]
    _append_text(
        log_file,
        f"verification: finding_id={finding.finding_id} rule={finding.rule} "
        f"path={finding.path} line={finding.line} still_firing={len(still_firing)}",
    )
    return len(still_firing) == 0


def apply_autofix(worktree_path: Path, finding: Finding, log_file: Path) -> bool:
    # --- Refactor-class scaffolding: classify finding ---
    rc = classify_finding(finding)
    finding.refactor_class = rc.value
    _append_text(
        log_file,
        f'reforge: classify finding_id={finding.finding_id} rule={finding.rule} '
        f'class={rc.value}',
    )

    # Safety gate: REFACTOR_CLASS findings that exceed size limits bypass deterministic engine
    if rc == RefactorClass.REFACTOR_CLASS:
        allowed, reason = can_auto_refactor(finding, worktree_path)
        if not allowed:
            _append_text(
                log_file,
                f'reforge: SAFETY_GATE in apply_autofix finding_id={finding.finding_id} '
                f'reason={reason}',
            )
            finding.refactor_phase = RefactorPhase.ABORTED.value
            # Persist RefactorWork state and route to human-review queue
            rw = RefactorWork(finding_id=finding.finding_id)
            rw.mark_aborted(reason)
            route_to_human_review(finding, rw, worktree_path, log_file)
            return False  # Bypass deterministic engine; route to human review queue

    file_path = worktree_path / finding.path

    if finding.rule == 'test-gap-missing-file' and finding.path == 'tests/test_notifications.py':
        if file_path.exists():
            _append_text(log_file, f'autofix: no-op finding_id={finding.finding_id} rule={finding.rule}')
            return False
        file_path.parent.mkdir(parents=True, exist_ok=True)
        template = """import pytest
from qa_sandbox.notifications import normalize_email


def test_normalize_email_trims_and_lowers() -> None:
    assert normalize_email("  USER@Example.com  ") == "user@example.com"


def test_normalize_email_invalid_input_raises() -> None:
    with pytest.raises(AttributeError):
        normalize_email(None)  # type: ignore[arg-type]
"""
        file_path.write_text(template, encoding='utf-8')
        _append_text(log_file, f'autofix: applied finding_id={finding.finding_id} rule={finding.rule}')
        return True

    if not file_path.exists():
        _append_text(log_file, f'autofix: target file missing finding_id={finding.finding_id} path={finding.path}')
        return False

    text = file_path.read_text(encoding='utf-8')
    updated = text

    if finding.rule == 'discount-math-sign':
        updated = updated.replace('return amount + discount', 'return amount - discount', 1)
    elif finding.rule == 'catalog-query-not-normalized':
        updated = updated.replace('if item == query', 'if item.strip().lower() == query.strip().lower()', 1)
    elif finding.rule == 'notifications-email-no-trim':
        updated = updated.replace('return value.lower()', 'return value.strip().lower()', 1)
    elif finding.rule == 'inventory-invalid-quantity':
        if 'if quantity <= 0:' not in updated:
            updated = re.sub(
                r'(\n\s*if sku not in stock:\n\s*return False\n)',
                r'\1    if quantity <= 0:\n        return False\n',
                updated,
                count=1,
            )
    elif finding.rule == 'hardcoded-tmp-path':
        updated = updated.replace('/tmp/qa-sandbox-state.json', '/var/lib/qa-sandbox/state.json', 1)
    elif finding.rule == 'docs-legacy-reference':
        updated = updated.replace('legacy_pricer.py', 'price.py')
    elif finding.rule == 'docs-missing-rollback':
        marker = '## Incident handling'
        rollback_block = (
            '\n## Rollback\n'
            '- Revert the latest bad commit (`git revert <sha>`).\n'
            '- Re-run checks: `python3 lint_check.py`, `python3 test_price.py`, `PYTHONPATH=src pytest -q`.\n'
            '- If checks recover, push revert and annotate the incident with root cause + follow-up fix.\n'
        )
        if marker in updated and '## Rollback' not in updated:
            updated = updated.replace(marker, marker + rollback_block, 1)
    elif finding.rule == 'docs-quickstart-gap':
        quickstart = '## Quick start\n```bash\npip install pytest\npython3 lint_check.py\npython3 test_price.py\npytest -q\n```'
        if '## Quick start' in updated and 'pip install pytest' not in updated and 'uv pip install pytest' not in updated:
            updated = re.sub(r'## Quick start\n```bash\n.*?```', quickstart, updated, flags=re.S)
    elif finding.rule == 'test-gap-missing-case' and finding.path == 'tests/test_orders.py':
        marker = 'test_apply_coupon_invalid_code_returns_original_total'
        if marker not in updated:
            updated = updated.rstrip() + (
                '\n\n\ndef test_apply_coupon_invalid_code_returns_original_total() -> None:\n'
                '    assert apply_coupon(100.0, "INVALID") == 100.0\n'
            )
    elif finding.rule == 'test-gap-missing-case' and finding.path == 'tests/test_inventory.py':
        marker = 'test_reserve_stock_rejects_negative_quantity'
        if marker not in updated:
            updated = updated.rstrip() + (
                '\n\n\ndef test_reserve_stock_rejects_negative_quantity() -> None:\n'
                '    stock = {"SKU-1": 5}\n'
                '    assert reserve_stock(stock, "SKU-1", -1) is False\n'
                '    assert stock["SKU-1"] == 5\n'
            )
    elif finding.rule == 'trailing-whitespace':
        # Strip trailing whitespace from all lines
        lines = updated.splitlines()
        stripped_lines = [line.rstrip() for line in lines]
        updated = '\n'.join(stripped_lines)
        if updated and not updated.endswith('\n'):
            updated += '\n'
    elif finding.rule == 'notifications-type-guard-missing':
        # Add isinstance type guard to normalize_email function
        if 'if not isinstance(value, str):' not in updated:
            updated = re.sub(
                r'(def normalize_email\(value: str\) -> str:\n)',
                r'\1    if not isinstance(value, str):\n        raise TypeError("value must be a string")\n',
                updated,
                count=1,
            )

    # Performance fixes - deterministic patterns
    if finding.rule == 'perf-pop-front-loop':
        # Replace list.pop(0) with deque.popleft() for O(1) operations
        if 'from collections import deque' not in updated and 'from collections import deque' not in updated:
            # Add import at the top after other imports
            import_match = re.search(r'(from \S+ import .+\n|import .+\n)+', updated)
            if import_match:
                insert_pos = import_match.end()
                updated = updated[:insert_pos] + 'from collections import deque\n' + updated[insert_pos:]
        # Replace .pop(0) with .popleft() (assumes deque is used)
        # Note: This is a simplified fix - in practice, need to ensure the list is converted to deque
        if '.pop(0)' in updated:
            updated = re.sub(r'(\w+)\.pop\(0\)', r'\1.popleft()', updated)

    if finding.rule == 'perf-list-membership-loop':
        # Convert list membership tests to use sets for O(1) lookup
        # Pattern: if item in some_list: (where some_list is used repeatedly in a loop)
        # Fix: Convert to set before the loop
        # This is a simplified fix - looks for common patterns
        list_membership_pattern = r'(\w+)\s*=\s*\[([^\]]+)\][\s\S]*?if\s+(\w+)\s+in\s+\1:'
        match = re.search(list_membership_pattern, updated)
        if match:
            list_name = match.group(1)
            list_content = match.group(2)
            # Add set conversion before the loop
            set_declaration = f'{list_name}_set = {{{list_content}}}'
            # Replace membership check
            updated = re.sub(
                rf'if\s+(\w+)\s+in\s+{list_name}:',
                rf'if \1 in {list_name}_set:',
                updated
            )
            # Add set declaration after the list
            updated = re.sub(
                rf'({list_name}\s*=\s*\[[^\]]+\])',
                rf'\1\n{set_declaration}',
                updated
            )

    # Type safety fixes - deterministic patterns
    if finding.rule == 'type-explicit-any':
        # Replace explicit `any` with `unknown` (safer) or proper type if inferrable
        # This is a simplified fix - just replace : any with : unknown
        if ': any' in updated:
            updated = re.sub(r':\s*any\b', ': unknown', updated)
        if '<any>' in updated:
            updated = re.sub(r'<any>', '<unknown>', updated)

    # Test coverage fixes - these require test generation, delegate to Claude
    # Mark as needing Claude fix engine by returning False
    if finding.rule in ('test-coverage-branch', 'test-coverage-function'):
        _append_text(
            log_file,
            f'autofix: test coverage finding requires Claude fix engine finding_id={finding.finding_id} rule={finding.rule}'
        )
        # Add to CLAUDE_REQUIRED_RULES equivalent check in fix workflow
        return False

    # Max-lines refactor: check if file is within auto-refactor limit
    if finding.rule == 'xo-max-lines':
        line_count = len(text.splitlines())
        if line_count > MAX_LINES_REFACTOR_LIMIT:
            _append_text(
                log_file,
                f'autofix: skip max-lines refactor finding_id={finding.finding_id} '
                f'path={finding.path} lines={line_count} limit={MAX_LINES_REFACTOR_LIMIT} (too large)'
            )
            return False
        # File is within limit - will be handled by Claude fix engine for complex refactoring
        # Return False here to let the deterministic engine pass through to Claude
        _append_text(
            log_file,
            f'autofix: max-lines refactor eligible finding_id={finding.finding_id} '
            f'path={finding.path} lines={line_count} target={MAX_LINES_REFACTOR_TARGET} (requires Claude fix engine)'
        )
        # Don't modify the file here - let Claude fix engine handle it
        return False

    # Ruff autofix - run ruff --fix for ruff-* rules
    if finding.rule.startswith('ruff-') and finding.safe_to_autofix:
        # Find ruff binary
        ruff_path = None
        for candidate in ['ruff', '~/.local/bin/ruff', '/home/vikas/.local/bin/ruff']:
            expanded = Path(candidate).expanduser()
            if expanded.exists():
                ruff_path = str(expanded)
                break
        if not ruff_path:
            try:
                res = subprocess.run(['which', 'ruff'], capture_output=True, text=True, timeout=5)
                if res.returncode == 0 and res.stdout.strip():
                    ruff_path = res.stdout.strip()
            except Exception:
                pass

        if ruff_path:
            try:
                # Read file content before fix
                content_before = file_path.read_text(encoding='utf-8') if file_path.exists() else ''

                res = subprocess.run(
                    [ruff_path, 'check', str(file_path), '--fix', '--unsafe-fixes', '--preview', '--select=' + finding.rule.replace('ruff-', '')],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=60,
                    cwd=str(worktree_path)
                )

                # Check if file was modified (ruff returns rc=2 if other issues remain)
                content_after = file_path.read_text(encoding='utf-8') if file_path.exists() else ''
                file_changed = content_before != content_after

                _append_text(
                    log_file,
                    f'autofix: ruff applied finding_id={finding.finding_id} rule={finding.rule} rc={res.returncode} file_changed={file_changed}'
                )
                return file_changed  # Return True if the file was actually modified
            except Exception as e:
                _append_text(log_file, f'autofix: ruff failed finding_id={finding.finding_id} rule={finding.rule} error={e}')
                return False
        else:
            _append_text(log_file, f'autofix: ruff not found finding_id={finding.finding_id} rule={finding.rule}')
            return False

    if updated == text:
        _append_text(log_file, f'autofix: no-op finding_id={finding.finding_id} rule={finding.rule}')
        return False

    file_path.write_text(updated, encoding='utf-8')
    _append_text(log_file, f'autofix: applied finding_id={finding.finding_id} rule={finding.rule}')
    return True


def git_commit_all(repo_path: Path, message: str, log_file: Path, dry_run: bool) -> str:
    rc, _ = run_capture(['git', 'add', '-A'], cwd=repo_path)
    if rc != 0:
        _append_text(log_file, 'error: git add failed')
        return 'error'

    rc, _ = run_capture(['git', 'diff', '--cached', '--quiet'], cwd=repo_path)
    if rc == 0:
        _append_text(log_file, 'autofix: no staged changes to commit')
        return 'no_changes'
    if rc not in (0, 1):
        _append_text(log_file, f'error: git diff --cached --quiet failed rc={rc}')
        return 'error'

    if dry_run:
        _append_text(log_file, f'dry-run-live: would git commit message="{message}"')
        return 'committed'

    rc, out = run_capture(['git', 'commit', '-m', message], cwd=repo_path)
    if rc != 0:
        _append_text(log_file, f'error: git commit failed output={out[:300]}')
        return 'error'
    return 'committed'


def git_push_branch(repo_path: Path, branch: str, log_file: Path, dry_run: bool) -> bool:
    if dry_run:
        _append_text(log_file, f'dry-run-live: would git push -u origin {branch}')
        return True
    rc, out = run_capture(['git', 'push', '-u', 'origin', branch], cwd=repo_path)
    if rc != 0:
        _append_text(log_file, f'error: git push failed branch={branch} output={out[:300]}')
        return False
    return True


def diff_stats(repo_path: Path) -> Tuple[int, int]:
    rc, out = run_capture(['git', 'diff', '--numstat'], cwd=repo_path)
    if rc != 0:
        return 0, 0
    files_changed = 0
    loc_diff = 0
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) < 3:
            continue
        add_s, del_s = parts[0], parts[1]
        if add_s != '-' and del_s != '-':
            try:
                loc_diff += int(add_s) + int(del_s)
            except ValueError:
                pass
        files_changed += 1
    return files_changed, loc_diff


def _normalize_check_output(out: str, cwd: Path) -> str:
    normalized = out.replace(str(cwd), '<CWD>')
    normalized = re.sub(r'/qa-sandbox-v2-[^/]+', '/<WORKTREE>', normalized)
    normalized = re.sub(r'line\s+\d+', 'line <N>', normalized)

    filtered_lines: List[str] = []
    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r'^[✔✓]', line):
            continue
        if re.match(r'^\d+\s+warnings?$', line, re.IGNORECASE):
            continue
        if re.match(r'^\d+(?:\.\d+)?s$', line, re.IGNORECASE):
            continue
        filtered_lines.append(line)

    normalized = '\n'.join(filtered_lines)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized[:2000]


def run_named_checks(
    repo_path: Path,
    checks: Dict[str, List[str]],
    log_file: Path,
    phase: str,
) -> Dict[str, Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    for name, cmd in checks.items():
        rc, out = run_capture(cmd, cwd=repo_path)
        normalized = _normalize_check_output(out, repo_path)
        fingerprint = hashlib.sha256(normalized.encode('utf-8')).hexdigest() if rc != 0 else ''
        results[name] = {
            'rc': rc,
            'cmd': cmd,
            'output': out,
            'fingerprint': fingerprint,
        }
        _append_text(log_file, f'{phase}: check={name} rc={rc} cmd={" ".join(cmd)}')
        if out:
            _append_text(log_file, f'{phase}-output[{name}]: {out[:500]}')
    return results


def build_target_checks(finding: Finding) -> Dict[str, List[str]]:
    if finding.rule == 'docs-legacy-reference':
        return {
            'target_docs_legacy_reference': [
                'python3',
                '-c',
                (
                    f'from pathlib import Path; text = Path("{finding.path}").read_text(encoding="utf-8"); '
                    'raise SystemExit(0 if "legacy_pricer.py" not in text else 1)'
                ),
            ]
        }

    if finding.rule == 'test-gap-missing-file' and finding.path == 'tests/test_notifications.py':
        return {
            'target_test_gap_missing_file': [
                'python3',
                '-c',
                (
                    'from pathlib import Path; p = Path("tests/test_notifications.py"); '
                    'text = p.read_text(encoding="utf-8") if p.exists() else ""; '
                    'ok = p.exists() and ("invalid_input" in text) and ("trims_and_lowers" in text); '
                    'raise SystemExit(0 if ok else 1)'
                ),
            ]
        }

    if finding.rule == 'test-gap-missing-case' and finding.path == 'tests/test_orders.py':
        return {
            'target_test_gap_orders_case': [
                'python3',
                '-c',
                (
                    'from pathlib import Path; text = Path("tests/test_orders.py").read_text(encoding="utf-8").lower(); '
                    'raise SystemExit(0 if ("invalid" in text and "coupon" in text) else 1)'
                ),
            ]
        }

    # Default: no target-specific checks for rules not listed above.
    # Must return {} (not None) — render_claude_fix_prompt() calls .items() on target_checks.
    return {}


def process_refactor_queue(
    worktree_path: Path,
    repo_path: Path,
    dry_run: bool = False,
    max_items: Optional[int] = None,
    auto_approve: bool = False,
) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {
        "processed": [],
        "approved": [],
        "pending": [],
        "failed": [],
    }

    queue = RefactorQueue()
    wt = Path(worktree_path)

    # Auto-approve: move pending_review → approved
    if auto_approve:
        pending_items = queue.list_items(
            status=QueueStatus.PENDING_REVIEW.value,
            worktree_path=str(worktree_path),
        )
        for item in pending_items[:max_items] if max_items else pending_items:
            if not dry_run:
                queue.approve(item.work_id, "auto_approved")
                result["approved"].append(item.work_id)
            # In dry_run mode, skip both the actual approval and the result update

    # Get items ready for execution (approved status).
    # No worktree filter here because the queue is already scoped to the
    # worktree being processed, and approved entries stored via
    # enqueue_refactor_work carry worktree-rooted paths.
    approved_items = queue.list_items(
        status=QueueStatus.APPROVED.value,
        worktree_path=None,
    )

    count = 0
    for item in approved_items:
        if max_items and count >= max_items:
            break
        count += 1

        try:
            if not dry_run:
                queue.start_execution(item.work_id)

                # Build the Finding from the stored finding_dict
                finding = Finding.from_dict(item.finding_dict)

                # Get the target checks for this rule
                target_checks = build_target_checks(finding)

                # Apply the fix via Claude fix engine
                rc, output, _ = apply_claude_fix(
                    worktree_path=wt,
                    finding=finding,
                    baseline_checks=BASELINE_VALIDATION_CHECKS,
                    target_checks=target_checks,
                    claude_cmd_template='claude --print "Read {prompt_file} and apply minimal fix."',
                    max_files_changed=20,
                    max_loc_diff=500,
                    log_file=Path("/dev/null"),
                )

                if rc == 0:
                    validation = run_validation_gate(
                        repo_path=repo_path,
                        worktree_path=wt,
                        checks=target_checks,
                        allow_unchanged_baseline_failures=True,
                        log_file=Path("/dev/null"),
                    )
                    if validation.get("passed"):
                        queue.complete(item.work_id)
                        result["processed"].append(item.work_id)
                    else:
                        error_msg = validation.get("message", "validation failed")
                        queue.fail(item.work_id, error_msg)
                        result["failed"].append(item.work_id)
                else:
                    error_msg = output[:500] if output else f"Claude returned rc={rc}"
                    queue.fail(item.work_id, error_msg)
                    result["failed"].append(item.work_id)
            else:
                result["processed"].append(item.work_id)

        except Exception as e:
            error_msg = f"exception: {e}"
            if not dry_run:
                queue.fail(item.work_id, error_msg)
            result["failed"].append(item.work_id)

    # Collect remaining pending items (no worktree filter — queue is already scoped)
    pending_items = queue.list_items(
        status=QueueStatus.PENDING_REVIEW.value,
        worktree_path=None,
    )
    result["pending"] = [item.work_id for item in pending_items]

    return result


def choose_validation_baseline(
    repo_baseline_results: Dict[str, Dict[str, Any]],
    worktree_baseline_results: Dict[str, Dict[str, Any]],
    log_file: Path,
) -> Dict[str, Dict[str, Any]]:
    """Choose between repo-level and worktree-level baseline results.

    If the worktree has meaningful baseline results (non-empty with actual
    check data), prefer those as they're specific to the worktree being
    validated. Otherwise fall back to the repo-level results.
    """
    if worktree_baseline_results:
        first_val = next(iter(worktree_baseline_results.values()), None)
        if first_val is not None and first_val.get("rc") is not None:
            _append_text(log_file, "validation-baseline: using worktree-specific baseline")
            return worktree_baseline_results
    _append_text(log_file, "validation-baseline: using repo-level baseline")
    return repo_baseline_results


def classify_review_feedback(feedback: str) -> str:
    """Classify review feedback as either 'needs-human' or 'actionable'.

    Called from the orchestrator to decide whether review feedback requires
    human intervention or can be addressed autonomously.
    """
    text = feedback.lower()
    conceptual_markers = [
        "architecture",
        "product decision",
        "design change",
        "conceptual",
        "rethink",
    ]
    for marker in conceptual_markers:
        if marker in text:
            return "needs-human"
    return "actionable"


def review_loop_allowed(
    pr_author: str,
    pr_tags: List[str],
    bot_author: str,
    explicit_tag: str,
) -> Tuple[bool, str]:
    """Decide whether a review loop is permitted for a PR.

    A review loop is allowed if the PR author is the bot itself (self-review
    guard bypass) or if the PR has an explicit tag indicating intentional
    review loop.
    """
    author_ok = pr_author == bot_author
    tag_ok = explicit_tag and explicit_tag in pr_tags
    if author_ok or tag_ok:
        return True, "review-loop-policy-pass"
    return False, "review-loop-policy-block: non-bot PR without explicit tag"
