"""batch_pr.py — Batch PR grouping and execution engine.

Phase 1: Pure grouping logic (rules, isolation, chunking, conflict detection).
Phase 2: Batch fix execution (shared worktrees, sequential fixes, batch PRs).
Phase 3: Split/recovery logic and conflict detection.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .models import BatchGroup, BatchRule, BatchStatus, Finding, FixResult, now_iso

logger = logging.getLogger(__name__)


def load_batch_rules(rules_path: Path) -> List[BatchRule]:
    """Load batch rules from a YAML file.

    Returns a list of BatchRule objects.
    Raises FileNotFoundError if the file does not exist.
    """
    text = rules_path.read_text(encoding='utf-8')
    data = yaml.safe_load(text)
    if not data or 'rules' not in data:
        return []

    rules: List[BatchRule] = []
    for entry in data['rules']:
        rules.append(BatchRule(
            rule_pattern=str(entry.get('rule_pattern', '')),
            enabled=bool(entry.get('enabled', True)),
            group_by=str(entry.get('group_by', 'rule')),
            max_batch_size=int(entry.get('max_batch_size', 20)),
            max_files_per_batch=int(entry.get('max_files_per_batch', 15)),
            max_loc_per_batch=int(entry.get('max_loc_per_batch', 500)),
            isolation=dict(entry.get('isolation', {})),
            priority=int(entry.get('priority', 99)),
            severity=str(entry.get('severity', 'normal')),
        ))
    return rules


def rule_matches(finding_rule: str, rule_pattern: str) -> bool:
    """Match a finding's rule against a batch rule pattern.

    Supports:
    - Exact match: "ruff-c408" == "ruff-c408"
    - Glob/prefix match: "ruff-b904" matches "ruff-*"
    """
    if '*' in rule_pattern:
        return fnmatch.fnmatch(finding_rule, rule_pattern)
    return finding_rule == rule_pattern


def is_isolated(finding: Finding, isolation_config: dict) -> bool:
    """Check if a finding should be excluded from batching.

    Reasons for isolation:
    - File matches an isolation file_pattern (e.g., migrations, middleware)
    """
    if not isolation_config:
        return False

    file_patterns = isolation_config.get('file_patterns', [])
    for pattern in file_patterns:
        if fnmatch.fnmatch(finding.path, pattern):
            return True
    return False


def check_batch_conflicts(findings: List[Finding]) -> List[Tuple[Finding, Finding]]:
    """Detect potential conflicts within a batch.

    Two findings conflict if:
    - They're in the same file AND
    - Their line numbers are within 5 lines of each other

    Returns a list of conflicting (finding_a, finding_b) pairs.
    """
    by_file: Dict[str, List[Finding]] = {}
    for f in findings:
        by_file.setdefault(f.path, []).append(f)

    conflicts: List[Tuple[Finding, Finding]] = []
    for path, file_findings in by_file.items():
        sorted_findings = sorted(file_findings, key=lambda f: f.line)
        for i in range(len(sorted_findings) - 1):
            if sorted_findings[i + 1].line - sorted_findings[i].line < 5:
                conflicts.append((sorted_findings[i], sorted_findings[i + 1]))

    return conflicts


def chunk_findings(findings: List[Finding], rule_config: BatchRule) -> List[List[Finding]]:
    """Split findings into chunks respecting size limits.

    Respects:
    - max_batch_size: max findings per chunk
    - max_files_per_batch: max unique files per chunk
    - max_loc_per_batch: estimated max lines changed (soft cap)

    Strategy: greedy fill — add findings to current chunk until a limit
    is hit, then start a new chunk.
    """
    if not findings:
        return []

    # Dynamic batch sizing: auto-determine max_batch_size based on file density.
    # Rationale: if 20 findings touch 5 files, they can be one batch (small PR).
    # If 20 findings touch 20 files, split into smaller batches.
    unique_files = len(set(f.path for f in findings))
    files_per_finding = unique_files / len(findings) if findings else 1.0

    # Target: keep each batch to a manageable number of unique files
    target_files = rule_config.max_files_per_batch
    auto_batch_size = max(3, min(30, int(target_files / files_per_finding)))

    # Use the SMALLER of auto_batch_size and configured max_batch_size
    effective_max_batch = min(auto_batch_size, rule_config.max_batch_size)

    chunks: List[List[Finding]] = []
    current: List[Finding] = []
    current_files: set = set()
    current_est_loc = 0

    for finding in findings:
        file_in_chunk = finding.path not in current_files

        if (len(current) >= effective_max_batch or
                (file_in_chunk and len(current_files) >= rule_config.max_files_per_batch)):
            chunks.append(current)
            current = []
            current_files = set()
            current_est_loc = 0

        current.append(finding)
        current_files.add(finding.path)
        current_est_loc += 1  # estimate: 1 line per micro fix

        # Soft check for LOC — only split if we already have findings
        if (current_est_loc >= rule_config.max_loc_per_batch and len(current) > 1):
            chunks.append(current)
            current = []
            current_files = set()
            current_est_loc = 0

    if current:
        chunks.append(current)

    return chunks


def _severity_batch_cap(severity: str, rule_max: int) -> int:
    """Return effective max_batch_size based on severity level.

    - critical → 1 (always solo)
    - high → min(rule_max, 5)
    - normal → rule_max
    - low → min(max(rule_max, 30), 30)  # allow up to 30
    """
    if severity == "critical":
        return 1
    if severity == "high":
        return min(rule_max, 5)
    if severity == "low":
        return 30
    return rule_max  # normal


SEVERITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}


def group_findings_for_batch(
    queue_candidates: List[Tuple[dict, Finding]],
    batch_rules: List[BatchRule],
) -> List[BatchGroup]:
    """Main grouping entry point.

    Input: list of (issue_dict, Finding) tuples from pr-cycle queue
    Output: list of BatchGroup objects (some solo, some multi-finding)

    Algorithm:
    1. Sort batch rules by priority
    2. For each rule, find matching findings
    3. Handle cross-rule grouping (group_by="cross-rule")
    4. Apply severity-based caps (critical=solo, high=max5, low=max30)
    5. Separate isolated findings (always solo)
    6. Chunk remaining findings into batches
    7. Sort resulting batches by severity order
    8. Any ungrouped findings become solo batches
    """
    batches: List[BatchGroup] = []
    batched_ids: set = set()

    # Filter to enabled rules, sort by priority
    enabled_rules = sorted(
        [r for r in batch_rules if r.enabled],
        key=lambda r: r.priority,
    )

    for rule_config in enabled_rules:
        # Cross-rule: collect findings matching wildcard across all specific rules
        if rule_config.group_by == "cross-rule":
            matching = [
                (issue, f) for issue, f in queue_candidates
                if rule_matches(f.rule, rule_config.rule_pattern)
                and f.finding_id not in batched_ids
            ]
            if not matching:
                continue

            # Separate isolated from batchable
            isolated: List[Tuple[dict, Finding]] = []
            batchable: List[Tuple[dict, Finding]] = []
            for issue, f in matching:
                if is_isolated(f, rule_config.isolation):
                    isolated.append((issue, f))
                else:
                    batchable.append((issue, f))

            # Create solo batches for isolated findings
            for issue, f in isolated:
                batches.append(BatchGroup.from_solo(issue, f))
                batched_ids.add(f.finding_id)

            # Chunk and create cross-rule batches
            findings_only = [f for _, f in batchable]
            if findings_only:
                # Apply severity cap
                effective_cap = _severity_batch_cap(rule_config.severity, rule_config.max_batch_size)
                capped_config = BatchRule(
                    rule_pattern=rule_config.rule_pattern,
                    group_by=rule_config.group_by,
                    max_batch_size=effective_cap,
                    max_files_per_batch=rule_config.max_files_per_batch,
                    max_loc_per_batch=rule_config.max_loc_per_batch,
                )
                for chunk in chunk_findings(findings_only, capped_config):
                    issues_map = {f.finding_id: issue for issue, f in batchable}
                    group = BatchGroup.from_findings(chunk, issues_map, capped_config)
                    # Override rule_pattern to the wildcard pattern
                    group.rule_pattern = rule_config.rule_pattern
                    batches.append(group)
                    batched_ids.update(f.finding_id for f in chunk)
            continue

        # Standard per-rule grouping
        matching = [
            (issue, f) for issue, f in queue_candidates
            if rule_matches(f.rule, rule_config.rule_pattern)
            and f.finding_id not in batched_ids
        ]

        if not matching:
            continue

        # Severity: critical findings → always solo
        if rule_config.severity == "critical":
            for issue, f in matching:
                batches.append(BatchGroup.from_solo(issue, f))
                batched_ids.add(f.finding_id)
            continue

        # Separate isolated from batchable
        isolated: List[Tuple[dict, Finding]] = []
        batchable: List[Tuple[dict, Finding]] = []
        for issue, f in matching:
            if is_isolated(f, rule_config.isolation):
                isolated.append((issue, f))
            else:
                batchable.append((issue, f))

        # Create solo batches for isolated findings
        for issue, f in isolated:
            batches.append(BatchGroup.from_solo(issue, f))
            batched_ids.add(f.finding_id)

        # Chunk and create multi-finding batches with severity cap
        findings_only = [f for _, f in batchable]
        if findings_only:
            effective_cap = _severity_batch_cap(rule_config.severity, rule_config.max_batch_size)
            capped_config = BatchRule(
                rule_pattern=rule_config.rule_pattern,
                group_by=rule_config.group_by,
                max_batch_size=effective_cap,
                max_files_per_batch=rule_config.max_files_per_batch,
                max_loc_per_batch=rule_config.max_loc_per_batch,
                isolation=rule_config.isolation,
                priority=rule_config.priority,
                severity=rule_config.severity,
            )
            for chunk in chunk_findings(findings_only, capped_config):
                issues_map = {f.finding_id: issue for issue, f in batchable}
                group = BatchGroup.from_findings(chunk, issues_map, capped_config)
                batches.append(group)
                batched_ids.update(f.finding_id for f in chunk)

    # Any remaining findings become solo
    for issue, f in queue_candidates:
        if f.finding_id not in batched_ids:
            batches.append(BatchGroup.from_solo(issue, f))

    # Sort batches by severity order (critical first → low last)
    def _batch_severity_sort_key(bg: BatchGroup) -> int:
        # Solo batches get highest priority (0 = critical tier)
        if bg.is_solo:
            return 0
        # Find the rule config that produced this batch to get severity
        for r in enabled_rules:
            if r.rule_pattern == bg.rule_pattern:
                return SEVERITY_ORDER.get(r.severity, 2)
        # Fallback: try matching by finding rules
        for r in enabled_rules:
            if any(rule_matches(f.rule, r.rule_pattern) for f in bg.findings):
                return SEVERITY_ORDER.get(r.severity, 2)
        return 2  # default normal

    batches.sort(key=_batch_severity_sort_key)

    return batches


# ────────────────────────────────────────────────────────────────
# Phase 2: Batch Fix Execution
# ────────────────────────────────────────────────────────────────


def _find_issue_for_finding(issues: list, finding_id: str) -> Optional[dict]:
    """Look up the issue dict corresponding to a finding_id."""
    for issue in issues:
        fid = issue.get('finding_id') or issue.get('id')
        if fid == finding_id:
            return issue
    return None


def _create_worktree(repo_path: Path, worktree_path: Path, branch: str, log_file: Path) -> bool:
    """Create a git worktree for batch fixes.

    Returns True if the worktree was created successfully.
    """
    from .utils import run_capture, run_no_capture
    from .state import _append_text

    # Prune stale worktree metadata
    run_no_capture(['git', 'worktree', 'prune'], cwd=repo_path)
    if worktree_path.exists():
        run_no_capture(['rm', '-rf', str(worktree_path)], cwd=repo_path)

    rc, out = run_capture(
        ['git', 'worktree', 'add', '-B', branch, str(worktree_path)],
        cwd=repo_path,
    )
    if rc != 0:
        _append_text(log_file, f'batch-worktree: failed to create worktree output={(out or "<empty>")[:300]}')
        return False
    _append_text(log_file, f'batch-worktree: created worktree={worktree_path} branch={branch}')
    return True


def verify_finding_closed(worktree_path: Path, finding: Finding, log_file: Path) -> bool:
    """Re-run the specific linter rule for one finding and check it's resolved.

    Uses the existing verify_fix_closed from lifecycle when possible,
    falling back to file-level pattern matching for batch-internal use.
    """
    from .lifecycle import verify_fix_closed as _verify_fix_closed

    try:
        return _verify_fix_closed(worktree_path, finding, log_file)
    except Exception:
        logger.debug('verify_finding_closed: lifecycle verify failed for %s, assuming open', finding.finding_id)
        return False


def apply_batch_fixes(
    batch: BatchGroup,
    worktree_path: Path,
    repo_path: Path,
    args,
    log_file: Path,
) -> Tuple[int, int]:
    """Apply all fixes within a shared worktree sequentially.

    For each finding in the batch:
    - If safe_to_autofix: try apply_autofix(), then apply_contextual_fix() fallback
    - If LLM-fixable: try apply_claude_fix()
    - Otherwise: skip

    Returns (successes, failures) tally.
    """
    from .lifecycle import apply_autofix
    from .state import _append_text

    successes = 0
    failures = 0

    for finding in batch.findings:
        result = _apply_single_fix(
            finding=finding,
            worktree_path=worktree_path,
            repo_path=repo_path,
            args=args,
            log_file=log_file,
        )
        batch.fix_results[finding.finding_id] = result

        if result.status == 'success':
            successes += 1
            _append_text(
                log_file,
                f'batch-fix: finding={finding.finding_id[:8]} rule={finding.rule} '
                f'path={finding.path}:{finding.line} status=success method={result.fix_method}',
            )
        elif result.status == 'skipped':
            _append_text(
                log_file,
                f'batch-fix: finding={finding.finding_id[:8]} rule={finding.rule} status=skipped '
                f'reason={result.error}',
            )
        else:
            failures += 1
            _append_text(
                log_file,
                f'batch-fix: finding={finding.finding_id[:8]} rule={finding.rule} '
                f'path={finding.path}:{finding.line} status=failed error={result.error}',
            )

    return successes, failures


def _apply_single_fix(
    finding: Finding,
    worktree_path: Path,
    repo_path: Path,
    args,
    log_file: Path,
) -> FixResult:
    """Apply one fix within a shared batch worktree.

    Follows the same fix strategy as the existing pr-cycle:
    1. If safe_to_autofix → apply_autofix, verify, fallback to contextual
    2. If LLM-fixable → apply_claude_fix
    3. Otherwise → skip
    """
    from .lifecycle import apply_autofix, build_target_checks, apply_claude_fix
    from .constants import BASELINE_VALIDATION_CHECKS, CLAUDE_REQUIRED_RULES, load_llm_fixable_rules
    from .state import _append_text
    import subprocess

    llm_rules = load_llm_fixable_rules()
    is_llm_fixable = (
        not finding.safe_to_autofix and
        finding.rule in llm_rules
    )
    use_claude = (
        getattr(args, 'fix_engine', 'deterministic') == 'claude' or
        finding.rule in CLAUDE_REQUIRED_RULES or
        is_llm_fixable
    )

    # FIX: Even in 'claude' mode, always try apply_autofix first for safe_to_autofix
    # findings. apply_autofix runs ruff --fix in seconds; Claude takes ~60s/finding
    # and cannot apply edits in non-interactive --print mode.
    if finding.safe_to_autofix:
        applied = apply_autofix(worktree_path, finding, log_file)
        if applied:
            closed = verify_finding_closed(worktree_path, finding, log_file)
            if closed:
                return FixResult(
                    finding_id=finding.finding_id, status='success',
                    diff_lines=1, fix_method='autofix',
                )
            return FixResult(
                finding_id=finding.finding_id, status='failed',
                error='verification-failed', fix_method='autofix',
            )
        # Autofix failed or couldn't apply; try contextual fallback before Claude
        try:
            from .context_fix import apply_contextual_fix
            _append_text(
                log_file,
                f'batch-fix: contextual fallback for rule={finding.rule} path={finding.path}',
            )
            applied = apply_contextual_fix(
                repo_path=repo_path,
                finding=finding,
                log_file=log_file,
                worktree_path=worktree_path,
            )
            if applied:
                closed = verify_finding_closed(worktree_path, finding, log_file)
                if closed:
                    return FixResult(
                        finding_id=finding.finding_id, status='success',
                        diff_lines=1, fix_method='contextual',
                    )
                return FixResult(
                    finding_id=finding.finding_id, status='failed',
                    error='contextual-verification-failed', fix_method='contextual',
                )
        except Exception as exc:
            _append_text(
                log_file,
                f'batch-fix: contextual fallback exception for {finding.finding_id[:8]}: {exc}',
            )
        # safe_to_autofix finding: both autofix and contextual couldn't apply.
        # If Claude is available, try it as final fallback.
        # Otherwise return 'failed' (not 'skipped') — autofix was available but didn't work.
        if not use_claude:
            return FixResult(
                finding_id=finding.finding_id, status='failed',
                error='autofix-unavailable', fix_method='autofix',
            )
        # fall through to Claude path

    elif not use_claude:
        # Not safe_to_autofix and not going to Claude → nothing we can try
        return FixResult(
            finding_id=finding.finding_id, status='skipped',
            error='not-llm-fixable', fix_method='autofix',
        )

    # Claude fix path
    target_checks = build_target_checks(finding)
    # Capture worktree state before Claude so we can detect if anything changed
    try:
        before_result = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=str(worktree_path),
            text=True,
            capture_output=True,
        )
        before_commit = before_result.stdout.strip()
    except Exception:
        before_commit = None

    try:
        rc, output, prompt_file = apply_claude_fix(
            worktree_path=worktree_path,
            finding=finding,
            baseline_checks=BASELINE_VALIDATION_CHECKS,
            target_checks=target_checks,
            claude_cmd_template=args.claude_cmd_template,
            max_files_changed=args.max_files_changed,
            max_loc_diff=args.max_loc_diff,
            log_file=log_file,
        )
    except Exception as exc:
        _append_text(log_file, f'batch-fix: claude exception for {finding.finding_id[:8]}: {exc}')
        return FixResult(
            finding_id=finding.finding_id, status='failed',
            error=f'claude-exception: {exc}', fix_method='claude',
        )

    # FIX: rc=0 does not mean Claude applied a fix. In --print non-interactive mode,
    # Claude returns 0 after analyzing but cannot use Edit/Bash tools.
    # Detect this by checking (a) Claude output mentions blocked tools, or
    # (b) the worktree HEAD commit is unchanged.
    tools_blocked = (
        output and (
            'Edit' in output and ('blocked' in output or 'denied' in output or 'cannot' in output.lower()) or
            'cannot apply' in output.lower() or
            'all file-modifying tools are blocked' in output
        )
    )

    worktree_changed = False
    if not tools_blocked and before_commit:
        try:
            after_result = subprocess.run(
                ['git', 'rev-parse', '--short', 'HEAD'],
                cwd=str(worktree_path),
                text=True,
                capture_output=True,
            )
            after_commit = after_result.stdout.strip()
            worktree_changed = after_commit != before_commit
        except Exception:
            pass

    # Fallback: OpenCode (and some other engines) edit files in-place without
    # auto-committing. If git HEAD is unchanged, check for unstaged file diffs.
    if not tools_blocked and not worktree_changed:
        try:
            diff_result = subprocess.run(
                ['git', 'diff', '--stat'],
                cwd=str(worktree_path),
                text=True,
                capture_output=True,
            )
            if diff_result.stdout and diff_result.stdout.strip():
                worktree_changed = True
        except Exception:
            pass

    if rc == 0 and not tools_blocked and worktree_changed:
        return FixResult(
            finding_id=finding.finding_id, status='success',
            fix_method='claude',
        )
    elif tools_blocked:
        return FixResult(
            finding_id=finding.finding_id, status='failed',
            error='claude-tools-blocked', fix_method='claude',
        )
    else:
        return FixResult(
            finding_id=finding.finding_id, status='failed',
            error=f'claude rc={rc} no-change', fix_method='claude',
        )


def create_batch_pr(batch: BatchGroup, repo_slug: str, log_file: Path) -> Dict[str, Any]:
    """Create a GitHub PR for the batch.

    Uses the standard `gh pr create` flow with batch-aware title and body.

    Returns dict with 'number' and 'url'.
    Raises RuntimeError on failure.
    """
    from .utils import run_capture
    from .state import _append_text

    title = batch.pr_title()
    body = batch.pr_body()
    branch = batch.branch

    _append_text(log_file, f'batch-pr: creating PR for {batch.batch_id} title={title}')

    rc, output = run_capture(
        ['gh', 'pr', 'create',
         '--repo', repo_slug,
         '--title', title,
         '--body', body,
         '--head', branch,
         '--base', 'main'],
        cwd=batch.worktree_path,
    )

    if rc != 0:
        _append_text(
            log_file,
            f'batch-pr: gh pr create failed rc={rc} output={(output or "<empty>")[:300]}',
        )
        raise RuntimeError(f'Failed to create batch PR: {output}')

    # Find the line containing the PR URL (gh may output warnings before it)
    pr_url = ''
    for line in output.strip().splitlines():
        if '/pull/' in line:
            pr_url = line.strip()
            break
    pr_number = None
    if pr_url:
        try:
            pr_number = int(pr_url.rstrip('/').split('/')[-1])
        except (ValueError, IndexError):
            pass

    _append_text(log_file, f'batch-pr: created PR #{pr_number} url={pr_url}')
    return {'number': pr_number, 'url': pr_url}


def link_issues_to_batch_pr(
    batch: BatchGroup,
    pr_number: int,
    pr_url: str,
    repo_slug: str,
    repo_path: Path,
    log_file: Path,
) -> None:
    """Update all issues in the batch to point to the shared PR.

    For each issue:
    - Set issue.github['pr_number'], ['pr_url'], ['batch_id']
    - Call set_issue_status(issue, 'pr_opened', ...)
    - Comment on the GitHub issue linking to the batch PR
    """
    from .orchestrator import set_issue_status
    from .gh import gh_issue_comment
    from .state import _append_text

    for issue in batch.issues:
        issue_github = issue.setdefault('github', {})
        issue_github['pr_number'] = pr_number
        issue_github['pr_url'] = pr_url
        issue_github['batch_id'] = batch.batch_id

        set_issue_status(issue, 'pr_opened', f'batched in PR #{pr_number}')

        issue_number = issue_github.get('issue_number')
        if issue_number is not None:
            try:
                gh_issue_comment(
                    repo_slug,
                    issue_number,
                    f'This finding has been batched into PR #{pr_number}: {pr_url}',
                    cwd=repo_path,
                )
            except Exception as exc:
                _append_text(
                    log_file,
                    f'batch-link: failed to comment on issue #{issue_number}: {exc}',
                )

        _append_text(
            log_file,
            f'batch-link: issue={issue.get("issue_id") or issue.get("id")} '
            f'linked to PR #{pr_number} batch={batch.batch_id}',
        )


def _hydrate_batch_worktree_deps(repo_path: Path, worktree_path: Path, log_file: Path) -> None:
    """Best-effort link shared dependency folders into a fresh git worktree.

    Duplicated from cli.py to avoid circular imports.
    """
    from .state import _append_text

    for dirname in ('node_modules',):
        source = repo_path / dirname
        target = worktree_path / dirname
        if not source.exists() or target.exists():
            continue
        try:
            os.symlink(source, target, target_is_directory=True)
            _append_text(log_file, f'worktree-deps: linked {dirname} from repo into worktree')
        except Exception as exc:
            _append_text(log_file, f'worktree-deps: failed to link {dirname}: {exc}')


# ────────────────────────────────────────────────────────────────
# Phase 3: Split/Recovery Logic and Conflict Detection
# ────────────────────────────────────────────────────────────────


def should_split_batch(batch: BatchGroup, max_depth: int = 3) -> bool:
    """Decide whether a batch should be split due to excessive failures.

    Returns True when:
    - Failure rate > 50%
    - AND batch hasn't exceeded max_split_depth

    Returns False otherwise.
    """
    # Count only attempted (non-skipped) results
    attempted = 0
    failed = 0
    for fid, result in batch.fix_results.items():
        status = result.status if isinstance(result, FixResult) else result.get('status')
        if status == 'skipped':
            continue
        attempted += 1
        if status == 'failed':
            failed += 1

    if attempted == 0:
        return False

    failure_rate = failed / attempted
    if failure_rate <= 0.5:
        return False

    if batch.retry_count >= max_depth:
        return False

    return True


def commit_partial_batch(
    successful_findings: List[Finding],
    batch: BatchGroup,
    log_file: Path,
) -> bool:
    """Commit partial results for successful findings.

    The worktree already has the successful fixes applied.
    Commits only those changes, pushes the branch, and creates a PR.

    Returns True if partial PR was created successfully.
    """
    from .lifecycle import git_commit_all, git_push_branch
    from .state import _append_text

    if not successful_findings:
        return False

    if batch.worktree_path is None or batch.branch is None:
        _append_text(log_file, f'batch-partial: no worktree/branch for {batch.batch_id}')
        return False

    # Stage only the files touched by successful findings
    from .utils import run_capture, run_no_capture
    successful_files = sorted({f.path for f in successful_findings})
    for filepath in successful_files:
        run_no_capture(['git', 'add', filepath], cwd=batch.worktree_path)

    commit_message = (
        f'fix: resolve {len(successful_findings)} {batch.rule_pattern} findings '
        f'(partial — {len(batch.findings) - len(successful_findings)} deferred)'
    )
    commit_result = git_commit_all(
        batch.worktree_path, commit_message,
        log_file=log_file,
        dry_run=False,
    )
    if commit_result == 'no_changes':
        _append_text(log_file, f'batch-partial: no changes to commit for {batch.batch_id}')
        return False

    pushed = git_push_branch(
        batch.worktree_path, batch.branch,
        log_file=log_file,
        dry_run=False,
    )
    if not pushed:
        _append_text(log_file, f'batch-partial: push failed for {batch.batch_id}')
        return False

    _append_text(
        log_file,
        f'batch-partial: committed {len(successful_findings)} successful fixes for {batch.batch_id}',
    )
    return True


def split_batch(
    batch: BatchGroup,
    repo_path: Path,
    args: Any,
    log_file: Path,
) -> List[BatchGroup]:
    """Split failed findings into sub-batches for retry.

    Strategy:
    - 1-2 failures → convert to solo batches
    - 3+ failures → halve into smaller sub-batches
    - Respects max_split_depth (from args, default 3)
    - Each sub-batch inherits batch_id prefix with split suffix
    """
    from .state import _append_text

    failed_findings = []
    for f in batch.findings:
        result = batch.fix_results.get(f.finding_id)
        if isinstance(result, FixResult):
            is_failed = result.status == 'failed'
        elif isinstance(result, dict):
            is_failed = result.get('status') == 'failed'
        else:
            is_failed = True  # No result → treat as failed
        if is_failed:
            failed_findings.append(f)

    if not failed_findings:
        return []

    sub_batches: List[BatchGroup] = []
    max_depth = getattr(args, 'max_split_depth', 3)

    if batch.retry_count >= max_depth:
        _append_text(
            log_file,
            f'batch-split: {batch.batch_id} at max depth {batch.retry_count}, not splitting further',
        )
        return []

    split_suffix = f'-s{len(batch.split_history) + 1}'

    if len(failed_findings) <= 2:
        # Convert each to a solo batch
        for f in failed_findings:
            issue = _find_issue_for_finding(batch.issues, f.finding_id)
            solo = BatchGroup.from_solo(issue, f)
            solo.batch_id = f'{batch.batch_id}{split_suffix}-{f.finding_id[:8]}'
            solo.retry_count = batch.retry_count + 1
            sub_batches.append(solo)
            _append_text(
                log_file,
                f'batch-split: finding {f.finding_id[:8]} → solo batch {solo.batch_id}',
            )
    else:
        # Halve into smaller sub-batches
        half = max(len(failed_findings) // 2, 1)
        for chunk_idx, i in enumerate(range(0, len(failed_findings), half)):
            chunk = failed_findings[i:i + half]
            issues_map = {
                f.finding_id: _find_issue_for_finding(batch.issues, f.finding_id)
                for f in chunk
            }
            rule_config = BatchRule(
                rule_pattern=batch.rule_pattern,
                max_batch_size=half,
            )
            sub_batch = BatchGroup.from_findings(chunk, issues_map, rule_config)
            sub_batch.batch_id = f'{batch.batch_id}{split_suffix}-h{chunk_idx + 1}'
            sub_batch.retry_count = batch.retry_count + 1
            sub_batches.append(sub_batch)
            _append_text(
                log_file,
                f'batch-split: {len(chunk)} findings → sub-batch {sub_batch.batch_id}',
            )

    return sub_batches


def split_on_conflicts(batch: BatchGroup) -> List[BatchGroup]:
    """Enhanced conflict handling: split batch at conflict boundaries.

    Builds a conflict graph from check_batch_conflicts() results.
    Separates non-conflicting findings into a clean batch.
    Isolates conflicting findings as solo batches.
    Returns list of conflict-free sub-batches.
    """
    conflicts = check_batch_conflicts(batch.findings)
    if not conflicts:
        return [batch]

    # Collect IDs of all findings involved in conflicts
    conflict_ids: set = set()
    for a, b in conflicts:
        conflict_ids.add(a.finding_id)
        conflict_ids.add(b.finding_id)

    non_conflicting = [f for f in batch.findings if f.finding_id not in conflict_ids]
    conflicting = [f for f in batch.findings if f.finding_id in conflict_ids]

    groups: List[BatchGroup] = []

    if non_conflicting:
        issues_map = {
            f.finding_id: _find_issue_for_finding(batch.issues, f.finding_id)
            for f in non_conflicting
        }
        rule_config = BatchRule(rule_pattern=batch.rule_pattern)
        clean_batch = BatchGroup.from_findings(non_conflicting, issues_map, rule_config)
        clean_batch.retry_count = batch.retry_count
        clean_batch.split_history = list(batch.split_history)
        groups.append(clean_batch)

    # Each conflicting finding gets its own solo batch
    for f in conflicting:
        issue = _find_issue_for_finding(batch.issues, f.finding_id)
        solo = BatchGroup.from_solo(issue, f)
        solo.retry_count = batch.retry_count
        solo.split_history = list(batch.split_history)
        groups.append(solo)

    return groups


def handle_batch_failure(
    batch: BatchGroup,
    repo_path: Path,
    args: Any,
    log_file: Path,
) -> List[BatchGroup]:
    """Main failure handler for a batch with excessive fix failures.

    1. Separates successful and failed findings from batch.fix_results
    2. If successful findings exist → commit and create PR for those
    3. If failed findings exist → split into sub-batches or solo
    4. Records split in batch.split_history
    5. Returns list of sub-BatchGroup objects for retry
    """
    from .state import _append_text

    successful_findings: List[Finding] = []
    failed_findings: List[Finding] = []

    for f in batch.findings:
        result = batch.fix_results.get(f.finding_id)
        if isinstance(result, FixResult):
            status = result.status
        elif isinstance(result, dict):
            status = result.get('status', 'failed')
        else:
            status = 'failed'

        if status == 'success':
            successful_findings.append(f)
        else:
            failed_findings.append(f)

    # Commit successful fixes if any
    if successful_findings:
        commit_partial_batch(successful_findings, batch, log_file)

    # Split failed findings into sub-batches
    sub_batches = split_batch(batch, repo_path, args, log_file)

    # Record split in history
    batch.split_history.append({
        'split_at': now_iso(),
        'successful_count': len(successful_findings),
        'failed_count': len(failed_findings),
        'sub_batches_created': len(sub_batches),
        'reason': 'too_many_fix_failures',
    })
    batch.status = BatchStatus.SPLIT.value

    _append_text(
        log_file,
        f'batch-failure-handler: {batch.batch_id} split: '
        f'{len(successful_findings)} succeeded, {len(failed_findings)} failed, '
        f'{len(sub_batches)} sub-batches created',
    )

    return sub_batches


def recover_interrupted_batch(
    batch_id: str,
    batches_file: Path,
    worktree_root: Path,
) -> Optional[BatchGroup]:
    """Recover from an interrupted batch.

    Loads batch by ID from batches file and attempts recovery:
    - If status is FIXING or FIXING_PARTIAL:
      - If worktree exists and branch pushed → mark PR_CREATED
      - If worktree exists but not pushed → mark ABORTED, clean up worktree
      - If no worktree → mark ABORTED
    - Saves updated status to batches file.
    - Returns recovered BatchGroup or None.
    """
    from .state import load_batches, update_batch_record, _append_text
    from .utils import run_no_capture, run_capture

    batches = load_batches(batches_file)
    record = None
    for b in batches:
        if b.get('batch_id') == batch_id:
            record = b
            break

    if record is None:
        return None

    status = record.get('status', '')
    if status not in (BatchStatus.FIXING.value, BatchStatus.FIXING_PARTIAL.value):
        return None

    # Derive batch object from record
    batch = _batch_from_record(record)
    if batch is None:
        return None

    worktree_path_str = record.get('worktree_path')
    branch = record.get('branch')
    worktree_path = Path(worktree_path_str) if worktree_path_str else None

    new_status: Optional[str] = None

    if worktree_path and worktree_path.exists():
        # Worktree exists — check if branch was pushed to remote
        pushed = False
        if branch:
            rc, _ = run_capture(
                ['git', 'branch', '-r', '--list', f'origin/{branch}'],
                cwd=worktree_path,
            )
            # Alternative: check if branch exists on remote
            rc2, remote_branches = run_capture(
                ['git', 'ls-remote', '--heads', 'origin', branch],
                cwd=worktree_path,
            )
            pushed = (rc2 == 0 and remote_branches and remote_branches.strip() != '')

        if pushed:
            new_status = BatchStatus.PR_CREATED.value
        else:
            # Worktree exists but not pushed → abort and clean up
            new_status = BatchStatus.ABORTED.value
            # Clean up worktree
            try:
                run_no_capture(
                    ['git', 'worktree', 'remove', '--force', str(worktree_path)],
                    cwd=worktree_root,
                )
                run_no_capture(['git', 'worktree', 'prune'], cwd=worktree_root)
            except Exception:
                pass
    else:
        # No worktree → abort
        new_status = BatchStatus.ABORTED.value

    if new_status:
        batch.status = new_status
        update_batch_record(batches_file, batch_id, {'status': new_status})

    return batch


def _batch_from_record(record: Dict[str, Any]) -> Optional[BatchGroup]:
    """Reconstruct a BatchGroup from a persisted record dict.

    Note: Finding objects are reconstructed with minimal fields needed
    for split/recovery operations.
    """
    findings_data = record.get('findings', [])
    findings: List[Finding] = []
    issues: list = []

    for fd in findings_data:
        finding = Finding(
            finding_id=fd.get('finding_id', ''),
            repo='',
            path=fd.get('path', ''),
            line=fd.get('line', 0),
            rule=fd.get('rule', ''),
            snippet='',
            confidence=0.0,
            quick_win=False,
            safe_to_autofix=False,
        )
        findings.append(finding)
        issues.append({
            'finding_id': fd.get('finding_id', ''),
            'id': fd.get('issue_id'),
        })

    worktree_path_str = record.get('worktree_path')

    return BatchGroup(
        batch_id=record.get('batch_id', ''),
        rule_pattern=record.get('rule_pattern', ''),
        group_by=record.get('group_by', ''),
        findings=findings,
        issues=issues,
        status=record.get('status', 'open'),
        worktree_path=Path(worktree_path_str) if worktree_path_str else None,
        branch=record.get('branch'),
        pr_number=record.get('pr_number'),
        pr_url=record.get('pr_url'),
        fix_results=record.get('fix_results', {}),
        retry_count=record.get('retry_count', 0),
        split_history=record.get('split_history', []),
    )


def process_batch(
    batch: BatchGroup,
    repo_path: Path,
    args,
    log_file: Path,
) -> Tuple[bool, Optional[str]]:
    """Process a multi-finding batch: worktree → fixes → PR.

    Returns (success: bool, detail: str).
    success=True if a PR was created (even with partial fixes).

    For solo batches, returns (False, 'solo-delegated') so the caller
    can route to the existing single-finding path.
    """
    from .lifecycle import git_commit_all, git_push_branch
    from .state import _append_text, save_batch_record
    from .constants import DEFAULT_BATCH_STATE, DEFAULT_WORKTREE_ROOT

    # Solo batches should use the existing single-finding path
    if batch.is_solo:
        return False, 'solo-delegated'

    # ── Multi-finding batch ──
    ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    rule_short = batch.rule_pattern.replace('ruff-', '')[:8]
    branch = f'qa/batch-{rule_short}-{ts}'
    worktree_root = Path(getattr(args, 'worktree_root', str(DEFAULT_WORKTREE_ROOT)))
    worktree_path = worktree_root.resolve() / f'qa-batch-{batch.batch_id}'

    batch.branch = branch
    batch.worktree_path = worktree_path
    batch.status = BatchStatus.FIXING.value

    _append_text(
        log_file,
        f'batch: processing {batch.batch_id} findings={len(batch.findings)} '
        f'rule={batch.rule_pattern} branch={branch}',
    )

    # Derive repo slug — needed for PR creation (not needed in dry-run)
    if not getattr(args, 'dry_run', True):
        from .gh import get_origin_url, parse_github_repo
        origin_url = get_origin_url(repo_path)
        gh_owner, gh_name = parse_github_repo(origin_url)
        repo_slug = f'{gh_owner}/{gh_name}' if gh_owner and gh_name else ''

        if not repo_slug:
            batch.status = BatchStatus.FAILED.value
            _append_text(log_file, f'batch-abort: {batch.batch_id} no repo slug could be derived from {origin_url}')
            return False, 'no-repo-slug'

    # Create shared worktree
    if not _create_worktree(repo_path, worktree_path, branch, log_file):
        batch.status = BatchStatus.FAILED.value
        return False, 'worktree-creation-failed'

    try:
        # Hydrate dependencies (e.g. node_modules symlink)
        _hydrate_batch_worktree_deps(repo_path, worktree_path, log_file)

        # Apply all fixes sequentially
        successes, failures = apply_batch_fixes(
            batch=batch,
            worktree_path=worktree_path,
            repo_path=repo_path,
            args=args,
            log_file=log_file,
        )

        _append_text(
            log_file,
            f'batch-fixes: {batch.batch_id} successes={successes} failures={failures}',
        )

        # No successful fixes → abort
        if successes == 0:
            batch.status = BatchStatus.FAILED.value
            _append_text(log_file, f'batch-abort: {batch.batch_id} no successful fixes')
            return False, 'no-successful-fixes'

        # Check if too many failures — split if needed
        failure_rate = failures / max(successes + failures, 1)
        if failure_rate > 0.5 and getattr(args, 'batch_pr_split_on_failure', True):
            if batch.retry_count < getattr(args, 'max_split_depth', 3):
                batch.retry_count += 1
                sub_batches = handle_batch_failure(batch, repo_path, args, log_file)
                # Process sub-batches recursively
                for sub_batch in sub_batches:
                    process_batch(sub_batch, repo_path, args, log_file)
                return True, 'split-and-retried'
            else:
                _append_text(log_file, f'batch: {batch.batch_id} max split depth reached, aborting')
                batch.status = BatchStatus.ABORTED.value
                return False, 'max-split-depth-exceeded'

        # Commit all successful changes
        commit_message = batch.pr_title()
        commit_result = git_commit_all(
            worktree_path, commit_message,
            log_file=log_file,
            dry_run=getattr(args, 'dry_run', True),
        )
        if commit_result == 'no_changes':
            batch.status = BatchStatus.FAILED.value
            _append_text(log_file, f'batch-abort: {batch.batch_id} commit=no_changes')
            return False, 'commit-no-changes'

        # Push branch
        pushed = git_push_branch(
            worktree_path, branch,
            log_file=log_file,
            dry_run=getattr(args, 'dry_run', True),
        )
        if not pushed:
            batch.status = BatchStatus.FAILED.value
            return False, 'push-failed'

        # Create PR (skip in dry-run)
        if getattr(args, 'dry_run', True):
            _append_text(
                log_file,
                f'batch-dry-run: would create PR for {batch.batch_id} '
                f'branch={branch} title={commit_message}',
            )
            batch.status = BatchStatus.DRY_RUN.value
            return True, 'dry-run-pr-simulated'

        pr = create_batch_pr(batch, repo_slug, log_file)
        pr_number = pr.get('number')
        pr_url = pr.get('url', '')
        batch.pr_number = pr_number
        batch.pr_url = pr_url
        batch.status = BatchStatus.PR_CREATED.value

        # Link all issues to the batch PR
        if pr_number is not None:
            link_issues_to_batch_pr(
                batch=batch,
                pr_number=pr_number,
                pr_url=pr_url,
                repo_slug=repo_slug,
                repo_path=repo_path,
                log_file=log_file,
            )

        # Save batch state
        batch_state_file = getattr(args, 'batch_state_file', None)
        if batch_state_file:
            save_batch_record(Path(batch_state_file), batch.to_record())

        _append_text(
            log_file,
            f'batch-success: {batch.batch_id} PR #{pr_number} '
            f'successes={successes} failures={failures}',
        )
        return True, f'pr-created-#{pr_number}'

    finally:
        # Cleanup worktree
        from .utils import run_no_capture
        if worktree_path.exists():
            run_no_capture(['git', 'worktree', 'remove', '--force', str(worktree_path)], cwd=repo_path)
            run_no_capture(['git', 'worktree', 'prune'], cwd=repo_path)
        if not getattr(args, 'live_github_actions', False):
            run_no_capture(['git', 'branch', '-D', branch], cwd=repo_path)
        _append_text(log_file, f'batch-cleanup: branch={branch}')

