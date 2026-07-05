"""Batch fix execution — subprocess-heavy functions for applying fixes in worktrees.

Extracted from bluei/engine/batch_pr.py during god-module decomposition
(branch refactor/god-batch-pr-decomp, phase 2). These functions take
batch groups and produce file changes + verification results.

Heavy subprocess use (git, gh, ruff, claude). All cross-module imports
are deferred (function-local) to avoid import cycles.

Public API (re-exported from bluei.engine.batch_pr for backward compat):
    apply_batch_fixes

Internal helpers also used by process_batch and tests:
    _create_worktree, verify_finding_closed, _apply_single_fix
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Optional, Tuple, TYPE_CHECKING

from bluei.engine.models import (
    BatchGroup,
    Finding,
    FixEngine,
    FixResult,
    FixStatus,
)
from bluei.engine.worktree import (
    create_worktree,
)

if TYPE_CHECKING:
    from bluei.engine.model_discovery import ModelDiscovery
    from bluei.engine.model_governor import SelectionFn

logger = logging.getLogger(__name__)


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
    *,
    selection_fn: Optional[SelectionFn] = None,
    governor_ledger_path: Optional[Path] = None,
    run_id: str = "",
    discovery: Optional[ModelDiscovery] = None,
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
            selection_fn=selection_fn,
            governor_ledger_path=governor_ledger_path,
            run_id=run_id,
            discovery=discovery,
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
    *,
    selection_fn: Optional[SelectionFn] = None,
    governor_ledger_path: Optional[Path] = None,
    run_id: str = "",
    discovery: Optional[ModelDiscovery] = None,
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

    # Model Governor resolution (ADR-0022 amendment 1) — mirror pr-cycle M5
    # (helper before the apply_claude_fix call; consume at ClaudeFixRequest).
    # Guard: only records when governor_ledger_path is not None (preserves
    # batch tests that don't pass it). pattern_store=None — the batch path has
    # no store in scope; coverage counts patterns as 0. apply_claude_fix is
    # unchanged (AC-P1-3): the resolved template reaches it via
    # ClaudeFixRequest.claude_cmd_template below.
    if governor_ledger_path is not None:
        from bluei.engine.model_governor import resolve_governed_model

        _gov_rec, _gov_resolved, resolved_tmpl, _model_name = resolve_governed_model(
            finding=finding,
            selection_fn=selection_fn,
            pattern_store=None,
            discovery=discovery,
            base_template=args.claude_cmd_template,
            ledger_path=governor_ledger_path,
            run_id=run_id,
        )
    else:
        resolved_tmpl = args.claude_cmd_template

    try:
        rc, output, prompt_file = apply_claude_fix(
            ClaudeFixRequest(
                worktree_path=worktree_path,
                finding=finding,
                baseline_checks=BASELINE_VALIDATION_CHECKS,
                target_checks=target_checks,
                claude_cmd_template=resolved_tmpl,
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
