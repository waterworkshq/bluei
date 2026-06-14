"""batch_pr.py — Batch PR grouping and execution engine.

Phase 1: Pure grouping logic (rules, isolation, chunking, conflict detection).
Phase 2: Batch fix execution (shared worktrees, sequential fixes, batch PRs).
Phase 3: Split/recovery logic and conflict detection.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import yaml

from bluei.engine.models import (
    BatchGroup,
    BatchRule,
    BatchStatus,
    Finding,
    FixEngine,
    FixResult,
    FixStatus,
    now_iso,
)
from bluei.engine.worktree import (
    create_worktree,
    hydrate_worktree,
    remove_worktree,
)
from bluei.engine.batch_recovery import (
    should_split_batch,
    commit_partial_batch,
    split_batch,
    split_on_conflicts,
    handle_batch_failure,
    recover_interrupted_batch,
    _batch_from_record,
)

logger = logging.getLogger(__name__)


def load_batch_rules(rules_path: Path) -> List[BatchRule]:
    """Load batch rules from a YAML file.

    Returns a list of BatchRule objects.
    Raises FileNotFoundError if the file does not exist.
    """
    text = rules_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not data or "rules" not in data:
        return []

    rules: List[BatchRule] = []
    for entry in data["rules"]:
        rules.append(
            BatchRule(
                rule_pattern=str(entry.get("rule_pattern", "")),
                enabled=bool(entry.get("enabled", True)),
                group_by=str(entry.get("group_by", "rule")),
                max_batch_size=int(entry.get("max_batch_size", 20)),
                max_files_per_batch=int(entry.get("max_files_per_batch", 15)),
                max_loc_per_batch=int(entry.get("max_loc_per_batch", 500)),
                isolation=dict(entry.get("isolation", {})),
                priority=int(entry.get("priority", 99)),
                severity=str(entry.get("severity", "normal")),
            )
        )
    return rules


def rule_matches(finding_rule: str, rule_pattern: str) -> bool:
    """Match a finding's rule against a batch rule pattern.

    Supports:
    - Exact match: "ruff-c408" == "ruff-c408"
    - Glob/prefix match: "ruff-b904" matches "ruff-*"
    """
    if "*" in rule_pattern:
        return fnmatch.fnmatch(finding_rule, rule_pattern)
    return finding_rule == rule_pattern


def is_isolated(finding: Finding, isolation_config: dict) -> bool:
    """Check if a finding should be excluded from batching.

    Reasons for isolation:
    - File matches an isolation file_pattern (e.g., migrations, middleware)
    """
    if not isolation_config:
        return False

    file_patterns = isolation_config.get("file_patterns", [])
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


def chunk_findings(
    findings: List[Finding], rule_config: BatchRule
) -> List[List[Finding]]:
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

        if len(current) >= effective_max_batch or (
            file_in_chunk and len(current_files) >= rule_config.max_files_per_batch
        ):
            chunks.append(current)
            current = []
            current_files = set()
            current_est_loc = 0

        current.append(finding)
        current_files.add(finding.path)
        current_est_loc += 1  # estimate: 1 line per micro fix

        # Soft check for LOC — only split if we already have findings
        if current_est_loc >= rule_config.max_loc_per_batch and len(current) > 1:
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
                (issue, f)
                for issue, f in queue_candidates
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
                effective_cap = _severity_batch_cap(
                    rule_config.severity, rule_config.max_batch_size
                )
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
            (issue, f)
            for issue, f in queue_candidates
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
            effective_cap = _severity_batch_cap(
                rule_config.severity, rule_config.max_batch_size
            )
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
        fid = issue.get("finding_id") or issue.get("id")
        if fid == finding_id:
            return issue
    return None


def _create_worktree(
    repo_path: Path, worktree_path: Path, branch: str, log_file: Path
) -> bool:
    """Create a git worktree for batch fixes.

    Returns True if the worktree was created successfully.
    Delegates to bluei.engine.worktree.create_worktree.
    """
    result = create_worktree(
        repo_path=repo_path,
        branch=branch,
        worktree_path=worktree_path,
        log_file=log_file,
    )
    return result.success


def verify_finding_closed(
    worktree_path: Path, finding: Finding, log_file: Path
) -> bool:
    """Re-run the specific linter rule for one finding and check it's resolved.

    Uses the verify module to check if the finding is closed.
    """
    from bluei.engine.verify import verify_fix_closed

    try:
        result = verify_fix_closed(worktree_path, finding, log_file)
        return result.is_closed
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.debug(
            "verify_finding_closed: lifecycle verify failed for %s: %s %s",
            finding.finding_id,
            type(exc).__name__,
            exc,
        )
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
    from bluei.engine.lifecycle import apply_autofix
    from bluei.engine.state import _append_text

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

        if result.status == FixStatus.SUCCESS.value:
            successes += 1
            _append_text(
                log_file,
                f"batch-fix: finding={finding.finding_id[:8]} rule={finding.rule} "
                f"path={finding.path}:{finding.line} status=success method={result.fix_method}",
            )
        elif result.status == FixStatus.SKIPPED.value:
            _append_text(
                log_file,
                f"batch-fix: finding={finding.finding_id[:8]} rule={finding.rule} status=skipped "
                f"reason={result.error}",
            )
        else:
            failures += 1
            _append_text(
                log_file,
                f"batch-fix: finding={finding.finding_id[:8]} rule={finding.rule} "
                f"path={finding.path}:{finding.line} status=failed error={result.error}",
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
    from bluei.engine.lifecycle import (
        ClaudeFixRequest,
        apply_autofix,
        apply_claude_fix,
    )
    from bluei.engine.validation import build_target_checks
    from bluei.engine.constants import (
        BASELINE_VALIDATION_CHECKS,
        CLAUDE_REQUIRED_RULES,
        load_llm_fixable_rules,
    )
    from bluei.engine.state import _append_text
    import subprocess

    llm_rules = load_llm_fixable_rules()
    is_llm_fixable = not finding.safe_to_autofix and finding.rule in llm_rules
    use_claude = (
        getattr(args, "fix_engine", "deterministic") == FixEngine.CLAUDE.value
        or finding.rule in CLAUDE_REQUIRED_RULES
        or is_llm_fixable
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
                    finding_id=finding.finding_id,
                    status=FixStatus.SUCCESS.value,
                    diff_lines=1,
                    fix_method="autofix",
                )
            return FixResult(
                finding_id=finding.finding_id,
                status=FixStatus.FAILED.value,
                error="verification-failed",
                fix_method="autofix",
            )
        # Autofix failed or couldn't apply; try contextual fallback before Claude
        try:
            from bluei.engine.context_fix import apply_contextual_fix

            _append_text(
                log_file,
                f"batch-fix: contextual fallback for rule={finding.rule} path={finding.path}",
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
                        finding_id=finding.finding_id,
                        status=FixStatus.SUCCESS.value,
                        diff_lines=1,
                        fix_method="contextual",
                    )
                return FixResult(
                    finding_id=finding.finding_id,
                    status=FixStatus.FAILED.value,
                    error="contextual-verification-failed",
                    fix_method="contextual",
                )
        except (subprocess.CalledProcessError, OSError) as exc:
            _append_text(
                log_file,
                f"batch-fix: contextual fallback exception for {finding.finding_id[:8]}: {exc}",
            )
        # safe_to_autofix finding: both autofix and contextual couldn't apply.
        # If Claude is available, try it as final fallback.
        # Otherwise return 'failed' (not 'skipped') — autofix was available but didn't work.
        if not use_claude:
            return FixResult(
                finding_id=finding.finding_id,
                status=FixStatus.FAILED.value,
                error="autofix-unavailable",
                fix_method="autofix",
            )
        # fall through to Claude path

    elif not use_claude:
        # Not safe_to_autofix and not going to Claude → nothing we can try
        return FixResult(
            finding_id=finding.finding_id,
            status=FixStatus.SKIPPED.value,
            error="not-llm-fixable",
            fix_method="autofix",
        )

    # Claude fix path
    target_checks = build_target_checks(finding)
    # Capture worktree state before Claude so we can detect if anything changed
    try:
        before_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(worktree_path),
            text=True,
            capture_output=True,
        )
        before_commit = before_result.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        before_commit = None

    try:
        rc, output, prompt_file = apply_claude_fix(
            ClaudeFixRequest(
                worktree_path=worktree_path,
                finding=finding,
                baseline_checks=BASELINE_VALIDATION_CHECKS,
                target_checks=target_checks,
                claude_cmd_template=args.claude_cmd_template,
                max_files_changed=args.max_files_changed,
                max_loc_diff=args.max_loc_diff,
                log_file=log_file,
            ),
        )
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        _append_text(
            log_file, f"batch-fix: claude exception for {finding.finding_id[:8]}: {exc}"
        )
        return FixResult(
            finding_id=finding.finding_id,
            status=FixStatus.FAILED.value,
            error=f"claude-exception: {exc}",
            fix_method="claude",
        )

    # FIX: rc=0 does not mean Claude applied a fix. In --print non-interactive mode,
    # Claude returns 0 after analyzing but cannot use Edit/Bash tools.
    # Detect this by checking (a) Claude output mentions blocked tools, or
    # (b) the worktree HEAD commit is unchanged.
    tools_blocked = output and (
        "Edit" in output
        and ("blocked" in output or "denied" in output or "cannot" in output.lower())
        or "cannot apply" in output.lower()
        or "all file-modifying tools are blocked" in output
    )

    worktree_changed = False
    if not tools_blocked and before_commit:
        try:
            after_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(worktree_path),
                text=True,
                capture_output=True,
            )
            after_commit = after_result.stdout.strip()
            worktree_changed = after_commit != before_commit
        except (subprocess.CalledProcessError, OSError):
            logger.debug("Failed to check worktree commit status")

    # Fallback: OpenCode (and some other engines) edit files in-place without
    # auto-committing. If git HEAD is unchanged, check for unstaged file diffs.
    if not tools_blocked and not worktree_changed:
        try:
            diff_result = subprocess.run(
                ["git", "diff", "--stat"],
                cwd=str(worktree_path),
                text=True,
                capture_output=True,
            )
            if diff_result.stdout and diff_result.stdout.strip():
                worktree_changed = True
        except (subprocess.CalledProcessError, OSError):
            logger.debug("Failed to check worktree diff status")

    if rc == 0 and not tools_blocked and worktree_changed:
        return FixResult(
            finding_id=finding.finding_id,
            status=FixStatus.SUCCESS.value,
            fix_method="claude",
        )
    elif tools_blocked:
        return FixResult(
            finding_id=finding.finding_id,
            status=FixStatus.FAILED.value,
            error="claude-tools-blocked",
            fix_method="claude",
        )
    else:
        return FixResult(
            finding_id=finding.finding_id,
            status=FixStatus.FAILED.value,
            error=f"claude rc={rc} no-change",
            fix_method="claude",
        )


def create_batch_pr(
    batch: BatchGroup, repo_slug: str, log_file: Path
) -> Dict[str, Any]:
    """Create a GitHub PR for the batch.

    Uses the standard `gh pr create` flow with batch-aware title and body.

    Returns dict with 'number' and 'url'.
    Raises RuntimeError on failure.
    """
    from bluei.engine.utils import run_capture
    from bluei.engine.state import _append_text

    title = batch.pr_title()
    body = batch.pr_body()
    branch = batch.branch

    _append_text(log_file, f"batch-pr: creating PR for {batch.batch_id} title={title}")

    rc, output = run_capture(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo_slug,
            "--title",
            title,
            "--body",
            body,
            "--head",
            branch,
            "--base",
            "main",
        ],
        cwd=batch.worktree_path,
    )

    if rc != 0:
        _append_text(
            log_file,
            f"batch-pr: gh pr create failed rc={rc} output={(output or '<empty>')[:300]}",
        )
        raise RuntimeError(f"Failed to create batch PR: {output}")

    # Find the line containing the PR URL (gh may output warnings before it)
    pr_url = ""
    for line in output.strip().splitlines():
        if "/pull/" in line:
            pr_url = line.strip()
            break
    pr_number = None
    if pr_url:
        try:
            pr_number = int(pr_url.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            logger.debug("Failed to parse PR number from URL")

    _append_text(log_file, f"batch-pr: created PR #{pr_number} url={pr_url}")
    return {"number": pr_number, "url": pr_url}


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
    from bluei.engine.orchestrator import set_issue_status
    from bluei.engine.gh import gh_issue_comment
    from bluei.engine.state import _append_text

    for issue in batch.issues:
        issue_github = issue.setdefault("github", {})
        issue_github["pr_number"] = pr_number
        issue_github["pr_url"] = pr_url
        issue_github["batch_id"] = batch.batch_id

        set_issue_status(issue, "pr_opened", f"batched in PR #{pr_number}")

        issue_number = issue_github.get("issue_number")
        if issue_number is not None:
            try:
                gh_issue_comment(
                    repo_slug,
                    issue_number,
                    f"This finding has been batched into PR #{pr_number}: {pr_url}",
                    cwd=repo_path,
                )
            except (subprocess.CalledProcessError, OSError) as exc:
                _append_text(
                    log_file,
                    f"batch-link: failed to comment on issue #{issue_number}: {exc}",
                )

        _append_text(
            log_file,
            f"batch-link: issue={issue.get('issue_id') or issue.get('id')} "
            f"linked to PR #{pr_number} batch={batch.batch_id}",
        )


# Phase 3 symbols (should_split_batch, commit_partial_batch, split_batch,
# split_on_conflicts, handle_batch_failure, recover_interrupted_batch,
# _batch_from_record) are imported from bluei.engine.batch_recovery at top.


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
    from bluei.engine.git_ops import git_commit_all, git_push_branch
    from bluei.engine.state import _append_text, save_batch_record
    from bluei.engine.constants import DEFAULT_BATCH_STATE, DEFAULT_WORKTREE_ROOT

    # Solo batches should use the existing single-finding path
    if batch.is_solo:
        return False, "solo-delegated"

    # ── Multi-finding batch ──
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    uniq = uuid4().hex[:6]
    rule_short = batch.rule_pattern.replace("ruff-", "")[:8]
    branch = f"qa/batch-{rule_short}-{ts}-{uniq}"
    worktree_root = Path(getattr(args, "worktree_root", str(DEFAULT_WORKTREE_ROOT)))
    worktree_path = worktree_root.resolve() / f"qa-batch-{batch.batch_id}"

    batch.branch = branch
    batch.worktree_path = worktree_path
    batch.status = BatchStatus.FIXING.value

    _append_text(
        log_file,
        f"batch: processing {batch.batch_id} findings={len(batch.findings)} "
        f"rule={batch.rule_pattern} branch={branch}",
    )

    # Derive repo slug — needed for PR creation (not needed in dry-run)
    if not getattr(args, "dry_run", True):
        from bluei.engine.gh import get_origin_url, parse_github_repo

        origin_url = get_origin_url(repo_path)
        gh_owner, gh_name = parse_github_repo(origin_url)
        repo_slug = f"{gh_owner}/{gh_name}" if gh_owner and gh_name else ""

        if not repo_slug:
            batch.status = BatchStatus.FAILED.value
            _append_text(
                log_file,
                f"batch-abort: {batch.batch_id} no repo slug could be derived from {origin_url}",
            )
            return False, "no-repo-slug"

        # Dedup check: skip if an equivalent batch PR is already open
        from bluei.engine.gh import find_batch_pr_by_rule

        dup_pr = find_batch_pr_by_rule(
            repo_slug,
            batch.rule_pattern,
            cwd=repo_path,
            max_age_hours=getattr(args, "batch_dedup_hours", 24),
        )
        if dup_pr is not None:
            _append_text(
                log_file,
                f"batch-skip-duplicate: {batch.batch_id} existing PR #{dup_pr['number']} "
                f"title={dup_pr.get('title', '')} url={dup_pr.get('url', '')}",
            )
            batch.status = BatchStatus.SKIPPED.value
            _append_text(
                log_file,
                f"batch-abort: {batch.batch_id} duplicate PR #{dup_pr['number']}",
            )
            return False, f"duplicate-existing-pr-#{dup_pr['number']}"

    # Create shared worktree
    if not _create_worktree(repo_path, worktree_path, branch, log_file):
        batch.status = BatchStatus.FAILED.value
        return False, "worktree-creation-failed"

    try:
        # Hydrate dependencies (e.g. node_modules symlink)
        hydrate_worktree(repo_path, worktree_path, log_file=log_file)

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
            f"batch-fixes: {batch.batch_id} successes={successes} failures={failures}",
        )

        # No successful fixes → abort
        if successes == 0:
            batch.status = BatchStatus.FAILED.value
            _append_text(log_file, f"batch-abort: {batch.batch_id} no successful fixes")
            return False, "no-successful-fixes"

        # Check if too many failures — split if needed
        max_depth = getattr(args, "max_split_depth", 3)
        split_warranted = should_split_batch(batch, max_depth=max_depth)
        if split_warranted and getattr(args, "batch_pr_split_on_failure", True):
            batch.retry_count += 1
            sub_batches = handle_batch_failure(batch, repo_path, args, log_file)
            # Process sub-batches recursively
            for sub_batch in sub_batches:
                process_batch(sub_batch, repo_path, args, log_file)
            return True, "split-and-retried"
        elif (
            not split_warranted
            and batch.retry_count >= max_depth
            and failures > 0
            and failures > successes
        ):
            _append_text(
                log_file,
                f"batch: {batch.batch_id} max split depth reached, aborting",
            )
            batch.status = BatchStatus.ABORTED.value
            return False, "max-split-depth-exceeded"

        # Commit all successful changes
        commit_message = batch.pr_title()
        commit_result = git_commit_all(
            worktree_path,
            commit_message,
            log_file=log_file,
            dry_run=getattr(args, "dry_run", True),
        )
        if commit_result == "no_changes":
            batch.status = BatchStatus.FAILED.value
            _append_text(log_file, f"batch-abort: {batch.batch_id} commit=no_changes")
            return False, "commit-no-changes"

        # Push branch
        pushed = git_push_branch(
            worktree_path,
            branch,
            log_file=log_file,
            dry_run=getattr(args, "dry_run", True),
        )
        if not pushed:
            batch.status = BatchStatus.FAILED.value
            return False, "push-failed"

        # Create PR (skip in dry-run)
        if getattr(args, "dry_run", True):
            _append_text(
                log_file,
                f"batch-dry-run: would create PR for {batch.batch_id} "
                f"branch={branch} title={commit_message}",
            )
            batch.status = BatchStatus.DRY_RUN.value
            return True, "dry-run-pr-simulated"

        try:
            pr = create_batch_pr(batch, repo_slug, log_file)
        except RuntimeError:
            batch.status = BatchStatus.FAILED.value
            _append_text(
                log_file,
                f"batch-pr-failed: {batch.batch_id} create_batch_pr raised RuntimeError",
            )
            return False, "pr-creation-failed"

        pr_number = pr.get("number")
        pr_url = pr.get("url", "")
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
        batch_state_file = getattr(args, "batch_state_file", None)
        if batch_state_file:
            save_batch_record(Path(batch_state_file), batch.to_record())

        _append_text(
            log_file,
            f"batch-success: {batch.batch_id} PR #{pr_number} "
            f"successes={successes} failures={failures}",
        )
        return True, f"pr-created-#{pr_number}"

    finally:
        # Cleanup worktree
        remove_worktree(
            worktree_path=worktree_path,
            repo_path=repo_path,
            branch=branch,
            delete_branch=not getattr(args, "live_github_actions", False),
            log_file=log_file,
        )
        _append_text(log_file, f"batch-cleanup: branch={branch}")
