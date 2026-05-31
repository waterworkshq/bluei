"""Fix lifecycle engine — the core value proposition of the QA agent.

Implements the detect → scaffold → apply → validate → commit state machine.
Three fix strategies are tried in cascade order: recipe → cascade → LLM.
Safety gates block destructive operations; failed validations auto-rollback.
"""

import hashlib
import logging
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

from bluei.engine.models import Finding
from bluei.engine.mnemo_client import MnemoClient
from bluei.engine.reforge import (
    RefactorClass,
    RefactorPhase,
    RefactorWork,
    classify_finding,
    can_auto_refactor,
)
from bluei.engine.utils import (
    run_capture,
    run_no_capture,
    sanitize_command_template,
    append_lesson,
    load_lessons_for_finding,
    load_lessons_for_rule,
    load_failure_clusters_for_rule,
)
from bluei.engine.state import (
    _append_text,
    load_finding_record,
    update_finding_record,
    increment_fix_attempt,
    repair_state,
    load_batches,
)
from bluei.engine.linters import discover_python_linter_findings
from bluei.engine.orchestrator import discover_findings
from bluei.engine.prompts import render_claude_fix_prompt
from bluei.engine.refactor_queue import RefactorQueue, QueueStatus
from bluei.engine.constants import (
    DEFAULT_CLAUDE_CMD_TEMPLATE,
    DEFAULT_DOCS_INDEX,
    DEFAULT_WORKTREE_ROOT,
    DEFAULT_BATCH_STATE,
    CLAUDE_REQUIRED_RULES,
    BASELINE_VALIDATION_CHECKS,
    MAX_LINES_REFACTOR_LIMIT,
    MAX_LINES_REFACTOR_TARGET,
    QA_FIX_PROMPT_FILENAME,
)
from bluei.engine.recipe_engine import (
    RecipeEngine,
    builtin_recipe_dir,
    staged_recipe_dir,
)
from bluei.engine.fix_tiers import (
    FixTier,
    resolve_tier,
    TieredValidator,
    check_tier_escalation,
)

_logger = logging.getLogger(__name__)

# Module-level singletons: recipe engine (builtins + staged) and tiered validator
_recipe_engine = RecipeEngine([builtin_recipe_dir(), staged_recipe_dir()])
_tiered_validator = TieredValidator()


def _is_pattern_sharing_enabled(workspace: Path) -> bool:
    config_file = workspace / "config.yaml"
    if not config_file.exists():
        return True
    try:
        import yaml

        with open(config_file) as f:
            config = yaml.safe_load(f)
        if config and isinstance(config, dict):
            return config.get("pattern_sharing", {}).get("enabled", True)
        return True
    except Exception:
        return True


def _extract_fix_pattern(
    worktree_path: Path,
    finding: Finding,
    fix_source: str,
    pattern_store_path: Optional[Path],
    log_file: Path,
) -> None:
    """Best-effort learned-pattern extraction for successful fixes."""
    if pattern_store_path is None:
        return
    try:
        from bluei.engine.pattern_extractor import extract
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(pattern_store_path)
        pattern_id = extract(worktree_path, finding, fix_source, store, log_file)
        if pattern_id:
            _append_text(
                log_file,
                f"pattern-extract-hook: source={fix_source} finding_id={finding.finding_id} pattern_id={pattern_id}",
            )
            try:
                workspace = Path(pattern_store_path).parent.parent
                if _is_pattern_sharing_enabled(workspace):
                    from bluei.engine.shared_pattern_library import SharedPatternLibrary

                    lib_path = workspace / "state" / "shared_patterns"
                    lib = SharedPatternLibrary(lib_path)
                    stored = store.get_pattern(pattern_id)
                    if stored is not None and stored.confidence >= 0.8:
                        repo_id = str(worktree_path.resolve().stem)
                        lib.publish(
                            rule=stored.rule,
                            language=stored.language,
                            before_snippet=stored.before_snippet,
                            after_snippet=stored.after_snippet,
                            confidence=stored.confidence,
                            repo_id=repo_id,
                            structural_hash=stored.structural_hash,
                            success_count=stored.success_count,
                            failure_count=stored.failure_count,
                        )
                        try:
                            from bluei.engine.shared_pattern_library import (
                                check_promotion_eligibility,
                                promote_pattern_to_recipe,
                            )
                            from bluei.engine.recipe_engine import staged_recipe_dir

                            cross_pat = (
                                lib._get_cached(stored.language).get(
                                    f"{stored.rule}::{stored.structural_hash}"
                                )
                                if stored.structural_hash
                                else None
                            )
                            if cross_pat and check_promotion_eligibility(cross_pat):
                                promote_pattern_to_recipe(
                                    cross_pat,
                                    stored.language,
                                    staged_recipe_dir(),
                                )
                                try:
                                    _recipe_engine.reload()
                                except Exception:
                                    _logger.debug(
                                        "Pattern extract hook failed (top-level)"
                                    )
                        except Exception:
                            _logger.debug("Pattern extract hook failed (outer)")
            except Exception:
                _logger.debug("Failed to reload recipe engine after fix")
        else:
            _append_text(
                log_file,
                f"pattern-extract-hook: source={fix_source} finding_id={finding.finding_id} result=no-pattern",
            )
    except Exception as exc:
        try:
            _append_text(
                log_file,
                f"pattern-extract-hook: source={fix_source} finding_id={finding.finding_id} "
                f"error={type(exc).__name__}",
            )
        except Exception:
            _logger.debug("Failed to log pattern-extract error")


def apply_claude_fix(
    worktree_path: Path,
    finding: Finding,
    baseline_checks: Dict[str, List[str]],
    target_checks: Dict[str, List[str]],
    claude_cmd_template: str,
    max_files_changed: int,
    max_loc_diff: int,
    log_file: Path,
    findings_file: Optional[Path] = None,
    lessons_file: Optional[Path] = None,
    repo_path: Optional[Path] = None,  # Mnemo integration (reserved)
    extra_prompt: Optional[str] = None,  # LLM rule prompt hint (reserved)
    prompt: Optional[str] = None,  # context_fix inline prompt (reserved)
    pattern_store_path: Optional[Path] = None,
    learned_patterns: Optional[
        str
    ] = None,  # pattern replay hint from format_pattern_hint()
) -> Tuple[int, str, str]:
    """Apply a fix using the Claude fix engine.

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
    # --- Directive seeding: load prior context from LESSONS_LOG + findings.jsonl ---
    fix_history: Optional[List[Dict[str, Any]]] = None
    finding_record: Optional[Dict[str, Any]] = None
    rule_history: Optional[List[Dict[str, Any]]] = None
    failure_clusters: Optional[str] = None

    if lessons_file is not None:
        fix_history = load_lessons_for_finding(finding.finding_id, lessons_file)
        rule_history = load_lessons_for_rule(finding.rule, lessons_file, limit=5)
        failure_clusters = load_failure_clusters_for_rule(finding.rule, lessons_file)
    if findings_file is not None:
        finding_record = load_finding_record(finding.finding_id, findings_file)

    prompt_path = worktree_path / QA_FIX_PROMPT_FILENAME
    prompt_text = render_claude_fix_prompt(
        finding=finding,
        baseline_checks=baseline_checks,
        target_checks=target_checks,
        max_files_changed=max_files_changed,
        max_loc_diff=max_loc_diff,
        fix_history=fix_history,
        finding_record=finding_record,
        extra_prompt=extra_prompt,
        learned_patterns=learned_patterns,
        rule_history=rule_history,
        failure_clusters=failure_clusters,
    )
    prompt_path.write_text(prompt_text, encoding="utf-8")

    try:
        try:
            command = claude_cmd_template.format(
                prompt_file=shlex.quote(str(prompt_path)),
                finding_id=shlex.quote(finding.finding_id),
                rule=shlex.quote(finding.rule),
                path=shlex.quote(finding.path),
            )
        except KeyError as exc:
            error = f"invalid claude command template placeholder: {exc}"
            _append_text(log_file, f"claude-fix: {error}")
            return 2, error, str(prompt_path)

        _append_text(
            log_file,
            "claude-fix: "
            f"finding_id={finding.finding_id} prompt_file={prompt_path} "
            f"cmd={sanitize_command_template(command)}",
        )
        res = subprocess.run(
            ["bash", "-l", "-c", command],
            cwd=str(worktree_path),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
        )
        output = (res.stdout or "").strip()
        _append_text(
            log_file,
            "claude-fix-result: "
            f"finding_id={finding.finding_id} rc={res.returncode} output={(output or '<empty>')[:1000]}",
        )

        # --- Directive seeding: record fix attempt outcome ---
        if findings_file is not None:
            success = res.returncode == 0
            increment_fix_attempt(
                finding.finding_id,
                findings_file,
                error=None if success else (output or "fix failed")[:500],
            )
            if success:
                update_finding_record(
                    finding.finding_id, findings_file, {"fix_success": True}
                )
        if lessons_file is not None:
            location = f"{finding.path}:{finding.line}"
            if res.returncode == 0:
                append_lesson(
                    lessons_file,
                    "fix",
                    finding_id=finding.finding_id,
                    what_changed=f"fix succeeded rule={finding.rule} path={location}",
                )
            else:
                error_summary = (output or "fix failed")[:200].split("\n")[-1]
                append_lesson(
                    lessons_file,
                    "fix",
                    finding_id=finding.finding_id,
                    what_broke=f"fix failed rc={res.returncode} rule={finding.rule} path={location}: {error_summary}",
                )
        if res.returncode == 0:
            _extract_fix_pattern(
                worktree_path, finding, "claude", pattern_store_path, log_file
            )

        return res.returncode, output, str(prompt_path)
    finally:
        try:
            prompt_path.unlink(missing_ok=True)
        except Exception:
            _logger.debug("Failed to unlink prompt temp file")


def route_to_human_review(
    finding: Finding,
    refactor_work: RefactorWork,
    worktree_path: Path,
    log_file: Path,
) -> str:
    """Route a REFACTOR_CLASS finding to the human-review refactor queue.

    Args:
        finding: The Finding being routed.
        refactor_work: The associated RefactorWork state record.
        worktree_path: Root path of the worktree (used to store worktree-rooted file paths).
        log_file: Path to the run log.

    Returns:
        The work_id of the created queue entry.
    """
    try:
        from bluei.engine.refactor_queue import (
            enqueue_refactor_work,
            RefactorQueue,
            QueueStatus,
        )

        entry = enqueue_refactor_work(finding, refactor_work, worktree_path)
        _append_text(
            log_file,
            f"human-review: enqueued finding_id={finding.finding_id} "
            f"work_id={entry.work_id} rule={finding.rule} "
            f"phase={refactor_work.phase.value} "
            f"reason={refactor_work.review_outcome or 'safety_gate'}",
        )
        return entry.work_id
    except Exception as exc:
        _append_text(
            log_file,
            f"human-review: failed to enqueue finding_id={finding.finding_id} "
            f"error={exc}",
        )
        return ""


def run_validation_gate(
    repo_path: Path,
    worktree_path: Path,
    checks: Optional[Dict[str, List[str]]],
    baseline_results: Optional[Dict[str, Dict[str, Any]]] = None,
    post_fix_results: Optional[Dict[str, Dict[str, Any]]] = None,
    target_results: Optional[Dict[str, Dict[str, Any]]] = None,
    allow_unchanged_baseline_failures: bool = True,
    log_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run baseline + post-fix + target checks and return structured result.

    Args:
        repo_path: Repository root path.
        worktree_path: Worktree path.
        checks: Dict of check-name → command list. If None, runs no checks.
        baseline_results: Optional pre-computed baseline results.
        post_fix_results: Optional pre-computed post-fix results.
        target_results: Optional pre-computed target results.
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
            repo_path=repo_path,
            checks=checks,
            log_file=log_file,
            phase=f"{phase_prefix}-baseline",
        )

    # Run post-fix checks (or reuse pre-computed results)
    if post_fix_results is not None:
        result["post_results"] = post_fix_results
    else:
        result["post_results"] = run_named_checks(
            repo_path=repo_path,
            checks=checks,
            log_file=log_file,
            phase=f"{phase_prefix}-postfix",
        )

    # Run target checks (or reuse pre-computed results)
    if target_results is not None:
        pass
    elif checks:
        target_results = run_named_checks(
            repo_path=repo_path,
            checks=checks,
            log_file=log_file,
            phase=f"{phase_prefix}-target",
        )
    else:
        target_results = {}

    # Evaluate regressions: a check that passed at baseline but failed post-fix is a regression
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
        result["message"] = (
            f"target checks failed: {', '.join(result['target_failures'])}"
        )
        _append_text(log_file, f"validation-gate: FAIL {result['message']}")
        return result

    result["message"] = "all checks passed"
    _append_text(log_file, "validation-gate: PASS")
    return result


def run_smoke_test(repo_path: Path, log_file: Path) -> Dict[str, Any]:
    """Run a cheap pre-flight smoke test on a registered repo.

    Args:
        repo_path: Path to the repository to smoke-test.
        log_file: Path to the run log.

    Returns:
        Dict with keys:
            passed: bool  (True only if all checks pass)
            checks: dict of {name: bool}
            duration_ms: int  (total wall-clock time)
            errors: List[str]  (one per failed check)
    """
    import time

    errors: list[str] = []
    checks: dict[str, bool] = {
        "git": False,
        "worktree": False,
        "linter": False,
    }
    t0 = time.monotonic()

    # --- Check 1: repo path exists and is a git repository ---
    t_git = time.monotonic()
    if not repo_path.exists():
        errors.append(f"repo path does not exist: {repo_path}")
    elif not (repo_path / ".git").exists() and not repo_path.is_dir():
        errors.append(f"not a directory or missing .git: {repo_path}")
    else:
        rc, out = run_capture(["git", "rev-parse", "--git-dir"], cwd=repo_path)
        if rc == 0:
            checks["git"] = True
        else:
            errors.append(f"git rev-parse failed rc={rc} out={out[:200]}")
    _append_text(
        log_file,
        f"smoke-test: git-check passed={checks['git']} duration_ms={int((time.monotonic() - t_git) * 1000)}",
    )

    # --- Check 2: worktree is clean ---
    t_wt = time.monotonic()
    rc, out = run_capture(["git", "status", "--porcelain"], cwd=repo_path)
    if rc == 0:
        if not out.strip():
            checks["worktree"] = True
        else:
            dirty_count = len([l for l in out.splitlines() if l.strip()])
            errors.append(f"worktree is dirty: {dirty_count} uncommitted change(s)")
    else:
        errors.append(f"git status --porcelain failed rc={rc} out={out[:200]}")
    _append_text(
        log_file,
        f"smoke-test: worktree-check passed={checks['worktree']} duration_ms={int((time.monotonic() - t_wt) * 1000)}",
    )

    # --- Check 3: linter (ruff) is available and can analyze a .py file ---
    t_li = time.monotonic()
    # Discover ruff using the same strategy as discover_python_linter_findings
    ruff_path: str | None = None
    for candidate in ["ruff", "~/.local/bin/ruff"]:
        expanded = Path(candidate).expanduser()
        if expanded.exists():
            ruff_path = str(expanded)
            break
    if ruff_path is None:
        import shutil as _shutil

        found = _shutil.which("ruff")
        if found:
            ruff_path = found

    if ruff_path is None:
        checks["linter"] = False
        errors.append("ruff not found \u2014 cannot run linter check")
    else:
        # Find a single .py file to lint quickly
        py_files = list(repo_path.rglob("*.py"))
        if not py_files:
            checks["linter"] = True  # no Python files = trivially OK
        else:
            # Lint just one small file for a quick check
            target = py_files[0]
            rc, out = run_capture(
                [
                    ruff_path,
                    "check",
                    str(target),
                    "--select=B,E,W,F,S,C4",
                    "--output-format=concise",
                ],
                cwd=repo_path,
            )
            # rc=0 means no issues, rc=1 means issues found but linter works
            if rc in (0, 1):
                checks["linter"] = True
            else:
                errors.append(f"ruff check failed rc={rc} out={out[:200]}")
    _append_text(
        log_file,
        f"smoke-test: linter-check passed={checks['linter']} duration_ms={int((time.monotonic() - t_li) * 1000)}",
    )

    t1 = time.monotonic()
    duration_ms = int((t1 - t0) * 1000)
    passed = all(checks.values())

    result: Dict[str, Any] = {
        "passed": passed,
        "checks": checks,
        "duration_ms": duration_ms,
        "errors": errors,
    }

    _append_text(
        log_file,
        f"smoke-test: result passed={passed} duration_ms={duration_ms} "
        f"git={checks['git']} worktree={checks['worktree']} linter={checks['linter']}",
    )
    if errors:
        for err in errors:
            _append_text(log_file, f"smoke-test: error {err}")

    return result


# Phase 3: mnemo client (lazy-initialized per repo, best-effort only)
_mnemo_clients: Dict[str, MnemoClient] = {}


def _get_mnemo_client(
    repo_path: Path, log_file: Optional[Path] = None
) -> Optional[MnemoClient]:
    """Return a lazily-initialized MnemoClient for the given repo (best-effort)."""
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
            _append_text(log_file, f"mnemo-init: unavailable ({exc})")
        return None


def _should_use_mnemo(
    finding: Finding,
    finding_record: Optional[Dict[str, Any]],
    fix_history: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Decide whether Mnemo should be queried for this fix attempt."""
    attempts = max(
        int(getattr(finding, "fix_attempts", 0) or 0),
        int((finding_record or {}).get("fix_attempts", 0) or 0),
    )

    if finding.rule in CLAUDE_REQUIRED_RULES:
        return True, "claude-required-rule"
    if attempts > 0:
        return True, f"retry-attempts={attempts}"
    if fix_history:
        return True, f"lesson-history={len(fix_history)}"
    if not finding.quick_win:
        return True, "not-quick-win"
    if not finding.safe_to_autofix:
        return True, "unsafe-to-autofix"
    if len((finding.snippet or "").strip()) >= 120:
        return True, "long-snippet"

    return False, "trivial-first-pass-quick-win"


def verify_fix_closed(
    worktree_path: Path,
    finding: Finding,
    log_file: Path,
    docs_index_file: Path = DEFAULT_DOCS_INDEX,
) -> bool:
    """Re-scan the worktree and confirm the finding no longer fires.

    Args:
        worktree_path: Path to the worktree to re-scan.
        finding: The Finding that should be resolved.
        log_file: Path to the run log.
        docs_index_file: Path to the docs index used by the discovery engine.

    Returns:
        True if the finding is gone (fix closed), False if it still fires.
    """
    rescanned = discover_findings(worktree_path, docs_index_file=docs_index_file)
    still_firing = [
        f
        for f in rescanned
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


def _tier_validate(
    tier: FixTier,
    worktree_path: Path,
    finding: Finding,
    log_file: Path,
    state: Optional[Dict[str, Any]] = None,
) -> bool:
    """Run tiered validation and auto-rollback on failure.

    Args:
        tier: The FixTier to validate against.
        worktree_path: Path to the worktree.
        finding: The Finding being validated.
        log_file: Path to the run log.
        state: Optional runner state dict for tier escalation checks.

    Returns:
        True if validation passed, False if rolled back or routed to human review.
    """
    result = _tiered_validator.validate(tier, worktree_path, finding, log_file=log_file)
    if not result.passed:
        # Auto-rollback: revert the changed file since validation failed
        rc, _ = run_capture(["git", "checkout", "--", finding.path], cwd=worktree_path)
        if rc != 0:
            _append_text(
                log_file,
                f"tier-validation: ROLLBACK_FAILED finding_id={finding.finding_id} "
                f"path={finding.path} git_rc={rc}",
            )
        _append_text(
            log_file,
            f"tier-validation: ROLLBACK finding_id={finding.finding_id} "
            f"tier={tier.name} reason={result.message}",
        )

        # Check if repeated failures warrant tier escalation
        if state is not None:
            escalated = check_tier_escalation(
                rule=finding.rule,
                state=state,
                current_tier=tier,
            )
            if escalated is not None:
                _append_text(
                    log_file,
                    f"tier-escalation: finding_id={finding.finding_id} "
                    f"rule={finding.rule} {tier.name} → {escalated.name}",
                )

        return False
    if result.requires_human_review:
        from bluei.engine.reforge import RefactorWork

        rw = RefactorWork(finding_id=finding.finding_id)
        rw.mark_aborted("tier4_structural_review")
        route_to_human_review(finding, rw, worktree_path, log_file)
        _append_text(
            log_file,
            f"tier-validation: HUMAN_REVIEW finding_id={finding.finding_id} tier={tier.name}",
        )
        return False
    return True


def apply_autofix(
    worktree_path: Path,
    finding: Finding,
    log_file: Path,
    pattern_store_path: Optional[Path] = None,
    detected_frameworks: Optional[list] = None,
) -> bool:
    """Apply a deterministic fix for a finding using recipes or hardcoded patterns.

    Args:
        worktree_path: Path to the worktree containing the target file.
        finding: The Finding to fix.
        log_file: Path to the run log.
        pattern_store_path: Optional path to the fix-pattern store for learning.
        detected_frameworks: Optional list of detected frameworks for classification.

    Returns:
        True if a deterministic fix was applied and tier-validated, False otherwise.
    """
    # --- Refactor-class scaffolding: classify finding ---
    rc = classify_finding(finding, detected_frameworks)
    finding.refactor_class = rc.value
    _append_text(
        log_file,
        f"reforge: classify finding_id={finding.finding_id} rule={finding.rule} "
        f"class={rc.value}",
    )

    # Safety gate: REFACTOR_CLASS findings that exceed size limits bypass deterministic engine
    # and are routed to the human-review queue instead of attempting auto-fix.
    if rc == RefactorClass.REFACTOR_CLASS:
        allowed, reason = can_auto_refactor(finding, worktree_path)
        if not allowed:
            _append_text(
                log_file,
                f"reforge: SAFETY_GATE in apply_autofix finding_id={finding.finding_id} "
                f"reason={reason}",
            )
            finding.refactor_phase = RefactorPhase.ABORTED.value
            # Persist RefactorWork state and route to human-review queue
            rw = RefactorWork(finding_id=finding.finding_id)
            rw.mark_aborted(reason)
            route_to_human_review(finding, rw, worktree_path, log_file)
            return False  # Bypass deterministic engine; route to human review queue

    # Guard: bail out if the target file doesn't exist (except for test-gap-missing-file)
    file_path = worktree_path / finding.path

    if not file_path.exists() and finding.rule != "test-gap-missing-file":
        _append_text(
            log_file,
            f"autofix: target file missing finding_id={finding.finding_id} path={finding.path}",
        )
        return False

    # --- Phase 1: Try recipe engine for declarative YAML recipes ---
    file_text = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    recipe = _recipe_engine.match(finding.rule, "python", file_text=file_text)
    if recipe:
        result = _recipe_engine.apply(recipe, file_path, worktree_path, finding=finding)
        if result.success:
            _append_text(
                log_file,
                f"autofix: recipe={recipe.id} applied finding_id={finding.finding_id} rule={finding.rule}",
            )
            _extract_fix_pattern(
                worktree_path, finding, "recipe", pattern_store_path, log_file
            )
            tier = resolve_tier(finding, recipe=recipe)
            if not _tier_validate(tier, worktree_path, finding, log_file):
                return False
            return True
        _append_text(
            log_file,
            f"autofix: recipe={recipe.id} failed finding_id={finding.finding_id} rule={finding.rule} error={result.error}",
        )

    # --- Cascade fallback: recipe failed or absent → try legacy hardcoded fixes ---
    # --- Phase 2: Legacy hardcoded fixes (deprecated, remove after migration) ---
    text = file_path.read_text(encoding="utf-8")
    updated = text

    if (
        finding.rule == "test-gap-missing-case"
        and finding.path == "tests/test_orders.py"
    ):
        marker = "test_apply_coupon_invalid_code_returns_original_total"
        if marker not in updated:
            updated = updated.rstrip() + (
                "\n\n\ndef test_apply_coupon_invalid_code_returns_original_total() -> None:\n"
                '    assert apply_coupon(100.0, "INVALID") == 100.0\n'
            )
    elif (
        finding.rule == "test-gap-missing-case"
        and finding.path == "tests/test_inventory.py"
    ):
        marker = "test_reserve_stock_rejects_negative_quantity"
        if marker not in updated:
            updated = updated.rstrip() + (
                "\n\n\ndef test_reserve_stock_rejects_negative_quantity() -> None:\n"
                '    stock = {"SKU-1": 5}\n'
                '    assert reserve_stock(stock, "SKU-1", -1) is False\n'
                '    assert stock["SKU-1"] == 5\n'
            )

    # Performance fixes - deterministic patterns (not yet migrated to recipes)
    if finding.rule == "perf-pop-front-loop":
        if "from collections import deque" not in updated:
            import_match = re.search(r"(from \S+ import .+\n|import .+\n)+", updated)
            if import_match:
                insert_pos = import_match.end()
                updated = (
                    updated[:insert_pos]
                    + "from collections import deque\n"
                    + updated[insert_pos:]
                )
        if ".pop(0)" in updated:
            updated = re.sub(r"(\w+)\.pop\(0\)", r"\1.popleft()", updated)

    if finding.rule == "perf-list-membership-loop":
        list_membership_pattern = (
            r"(\w+)\s*=\s*\[([^\]]+)\][\s\S]*?if\s+(\w+)\s+in\s+\1:"
        )
        match = re.search(list_membership_pattern, updated)
        if match:
            list_name = match.group(1)
            list_content = match.group(2)
            set_declaration = f"{list_name}_set = {{{list_content}}}"
            updated = re.sub(
                rf"if\s+(\w+)\s+in\s+{list_name}:",
                rf"if \1 in {list_name}_set:",
                updated,
            )
            updated = re.sub(
                rf"({list_name}\s*=\s*\[[^\]]+\])", rf"\1\n{set_declaration}", updated
            )

    # --- Cascade fallback: test coverage and max-lines findings require Claude ---
    # These cannot be fixed deterministically; return False so the caller falls to LLM.
    if finding.rule in ("test-coverage-branch", "test-coverage-function"):
        _append_text(
            log_file,
            f"autofix: test coverage finding requires Claude fix engine finding_id={finding.finding_id} rule={finding.rule}",
        )
        # Add to CLAUDE_REQUIRED_RULES equivalent check in fix workflow
        return False

    # Max-lines refactor: files exceeding the auto-refactor limit are skipped;
    # caller should fall to the Claude fix engine for supervised refactoring.
    if finding.rule == "xo-max-lines":
        line_count = len(text.splitlines())
        if line_count > MAX_LINES_REFACTOR_LIMIT:
            _append_text(
                log_file,
                f"autofix: skip max-lines refactor finding_id={finding.finding_id} "
                f"path={finding.path} lines={line_count} limit={MAX_LINES_REFACTOR_LIMIT} (too large)",
            )
            return False
        _append_text(
            log_file,
            f"autofix: max-lines refactor eligible finding_id={finding.finding_id} "
            f"path={finding.path} lines={line_count} target={MAX_LINES_REFACTOR_TARGET} (requires Claude fix engine)",
        )
        return False

    if updated == text:
        _append_text(
            log_file,
            f"autofix: no-op finding_id={finding.finding_id} rule={finding.rule}",
        )
        return False

    file_path.write_text(updated, encoding="utf-8")
    _append_text(
        log_file,
        f"autofix: applied finding_id={finding.finding_id} rule={finding.rule}",
    )
    _extract_fix_pattern(
        worktree_path, finding, "autofix", pattern_store_path, log_file
    )
    tier = resolve_tier(finding)
    if not _tier_validate(tier, worktree_path, finding, log_file):
        return False
    return True


_cached_stores: Dict[str, Any] = {}


def _get_or_create_store(pattern_store_path: Path) -> Optional["FixPatternStore"]:
    key = str(pattern_store_path)
    cached = _cached_stores.get(key)
    if cached is not None:
        return cached
    from bluei.engine.pattern_store import FixPatternStore

    store = FixPatternStore(pattern_store_path)
    _cached_stores[key] = store
    return store


def _get_or_create_shared_library(workspace: Path) -> Any:
    key = f"shared:{workspace}"
    cached = _cached_stores.get(key)
    if cached is not None:
        return cached
    from bluei.engine.shared_pattern_library import SharedPatternLibrary

    lib_path = workspace / "state" / "shared_patterns"
    lib = SharedPatternLibrary(lib_path)
    _cached_stores[key] = lib
    return lib


def apply_cascade_fix(
    worktree_path: Path,
    finding: Finding,
    log_file: Path,
    pattern_store_path: Optional[Path] = None,
    detected_frameworks: Optional[list] = None,
    deterministic_only: bool = False,
) -> bool:
    """Apply the deterministic cascade to fix a finding."""
    from bluei.engine.cascade import (
        CascadeContext,
        DeterministicCascade,
        default_cascade_stages,
    )

    language = "python"
    if finding.path:
        ext = Path(finding.path).suffix.lower()
        if ext in (".ts", ".tsx"):
            language = "typescript"
        elif ext == ".go":
            language = "go"
        elif ext == ".rs":
            language = "rust"

    pattern_store: Optional[FixPatternStore] = None
    if pattern_store_path is not None:
        try:
            pattern_store = _get_or_create_store(pattern_store_path)
        except Exception:
            logging.debug("pattern store creation failed for %s", pattern_store_path)
            pass

    shared_library = None
    try:
        workspace = (
            Path(pattern_store_path).parent.parent if pattern_store_path else Path(".")
        )
        sharing_enabled = _is_pattern_sharing_enabled(workspace)
        if sharing_enabled:
            shared_library = _get_or_create_shared_library(workspace)
    except Exception:
        logging.debug("shared pattern library creation failed")
        pass

    recipe_engine = _recipe_engine

    replay_threshold_overrides: Dict[str, float] = {}

    context = CascadeContext(
        worktree=worktree_path,
        repo_path=worktree_path,
        log_file=log_file,
        language=language,
        pattern_store=pattern_store,
        recipe_engine=recipe_engine,
        allow_llm=not deterministic_only,
        shared_library=shared_library,
        replay_threshold_overrides=replay_threshold_overrides,
    )

    cascade = DeterministicCascade(default_cascade_stages())
    result = cascade.execute(finding, worktree_path, context)

    if result.success:
        # Deterministic stage succeeded — extract patterns and validate
        _extract_fix_pattern(
            worktree_path,
            finding,
            result.stage_name,
            pattern_store_path,
            log_file,
        )
        tier = resolve_tier(finding)
        if not _tier_validate(tier, worktree_path, finding, log_file):
            return False
        return True

    # Cascade fallback: all deterministic stages failed → hand off to LLM
    if context.allow_llm:
        _append_text(
            log_file,
            f"cascade: falling to LLM finding_id={finding.finding_id} rule={finding.rule}",
        )
        from bluei.engine.constants import (
            BASELINE_VALIDATION_CHECKS,
            DEFAULT_CLAUDE_CMD_TEMPLATE,
        )

        rc, _output, _prompt = apply_claude_fix(
            worktree_path,
            finding,
            dict(BASELINE_VALIDATION_CHECKS),
            {},
            DEFAULT_CLAUDE_CMD_TEMPLATE,
            5,
            200,
            log_file,
            pattern_store_path=pattern_store_path,
        )
        return rc == 0

    return False


def git_commit_all(repo_path: Path, message: str, log_file: Path, dry_run: bool) -> str:
    """Stage all changes and commit with the given message.

    Args:
        repo_path: Path to the git repository.
        message: Commit message.
        log_file: Path to the run log.
        dry_run: If True, log the would-be commit without executing it.

    Returns:
        'committed', 'no_changes', or 'error'.
    """
    rc, _ = run_capture(["git", "add", "-A"], cwd=repo_path)
    if rc != 0:
        _append_text(log_file, "error: git add failed")
        return "error"

    rc, _ = run_capture(["git", "diff", "--cached", "--quiet"], cwd=repo_path)
    if rc == 0:
        _append_text(log_file, "autofix: no staged changes to commit")
        return "no_changes"
    if rc not in (0, 1):
        _append_text(log_file, f"error: git diff --cached --quiet failed rc={rc}")
        return "error"

    if dry_run:
        _append_text(log_file, f'dry-run-live: would git commit message="{message}"')
        return "committed"

    rc, out = run_capture(["git", "commit", "-m", message], cwd=repo_path)
    if rc != 0:
        _append_text(log_file, f"error: git commit failed output={out[:300]}")
        return "error"
    return "committed"


def git_push_branch(
    repo_path: Path, branch: str, log_file: Path, dry_run: bool
) -> bool:
    """Push the given branch to origin.

    Args:
        repo_path: Path to the git repository.
        branch: Branch name to push.
        log_file: Path to the run log.
        dry_run: If True, log the would-be push without executing it.

    Returns:
        True if push succeeded (or dry-run), False on failure.
    """
    if dry_run:
        _append_text(log_file, f"dry-run-live: would git push -u origin {branch}")
        return True
    rc, out = run_capture(["git", "push", "-u", "origin", branch], cwd=repo_path)
    if rc != 0:
        _append_text(
            log_file, f"error: git push failed branch={branch} output={out[:300]}"
        )
        return False
    return True


def diff_stats(repo_path: Path) -> Tuple[int, int]:
    """Return (files_changed, total_loc_diff) from the working tree diff.

    Args:
        repo_path: Path to the git repository.

    Returns:
        Tuple of (files_changed, lines_added_plus_deleted). (0, 0) on error.
    """
    rc, out = run_capture(["git", "diff", "--numstat"], cwd=repo_path)
    if rc != 0:
        return 0, 0
    files_changed = 0
    loc_diff = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_s, del_s = parts[0], parts[1]
        if add_s != "-" and del_s != "-":
            try:
                loc_diff += int(add_s) + int(del_s)
            except ValueError:
                _logger.debug("Failed to parse diff stat line")
        files_changed += 1
    return files_changed, loc_diff


def _normalize_check_output(out: str, cwd: Path) -> str:
    """Normalize check output for fingerprinting: strip CWD, line numbers, and noise."""
    normalized = out.replace(str(cwd), "<CWD>")
    normalized = re.sub(r"/qa-sandbox-v2-[^/]+", "/<WORKTREE>", normalized)
    normalized = re.sub(r"line\s+\d+", "line <N>", normalized)

    filtered_lines: List[str] = []
    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^[✔✓]", line):
            continue
        if re.match(r"^\d+\s+warnings?$", line, re.IGNORECASE):
            continue
        if re.match(r"^\d+(?:\.\d+)?s$", line, re.IGNORECASE):
            continue
        filtered_lines.append(line)

    normalized = "\n".join(filtered_lines)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized[:2000]


def run_named_checks(
    repo_path: Path,
    checks: Dict[str, List[str]],
    log_file: Path,
    phase: str,
) -> Dict[str, Dict[str, Any]]:
    """Execute a dict of named check commands and return structured results.

    Args:
        repo_path: Working directory for command execution.
        checks: Mapping of check name → command (as a list of strings).
        log_file: Path to the run log.
        phase: Label for log lines (e.g. ``"validation-baseline"``).

    Returns:
        Dict of check name → ``{"rc", "cmd", "output", "fingerprint"}``.
    """
    results: Dict[str, Dict[str, Any]] = {}
    for name, cmd in checks.items():
        rc, out = run_capture(cmd, cwd=repo_path)
        normalized = _normalize_check_output(out, repo_path)
        fingerprint = (
            hashlib.sha256(normalized.encode("utf-8")).hexdigest() if rc != 0 else ""
        )
        results[name] = {
            "rc": rc,
            "cmd": cmd,
            "output": out,
            "fingerprint": fingerprint,
        }
        _append_text(log_file, f"{phase}: check={name} rc={rc} cmd={' '.join(cmd)}")
        if out:
            _append_text(log_file, f"{phase}-output[{name}]: {out[:500]}")
    return results


def build_target_checks(finding: Finding) -> Dict[str, List[str]]:
    """Build rule-specific target checks that must pass after a fix is applied.

    Args:
        finding: The Finding being fixed.

    Returns:
        Dict of check name → command. Empty dict if no target checks apply.
    """
    if finding.rule == "docs-legacy-reference":
        return {
            "target_docs_legacy_reference": [
                "python3",
                "-c",
                (
                    f'from pathlib import Path; text = Path("{finding.path}").read_text(encoding="utf-8"); '
                    'raise SystemExit(0 if "legacy_pricer.py" not in text else 1)'
                ),
            ]
        }

    if (
        finding.rule == "test-gap-missing-case"
        and finding.path == "tests/test_orders.py"
    ):
        return {
            "target_test_gap_orders_case": [
                "python3",
                "-c",
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
    """Process approved refactor-queue items through the Claude fix engine.

    Args:
        worktree_path: Path to the worktree for fix application.
        repo_path: Path to the git repository (for validation).
        dry_run: If True, report what would happen without executing.
        max_items: Cap on number of items to process.
        auto_approve: If True, move pending_review items to approved first.

    Returns:
        Dict with keys ``"processed"``, ``"approved"``, ``"pending"``, ``"failed"``,
        each mapping to a list of work IDs.
    """
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
                    claude_cmd_template=DEFAULT_CLAUDE_CMD_TEMPLATE,
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
    """Choose between repo-level and worktree-level baseline results."""
    if worktree_baseline_results:
        first_val = next(iter(worktree_baseline_results.values()), None)
        if first_val is not None and first_val.get("rc") is not None:
            _append_text(
                log_file, "validation-baseline: using worktree-specific baseline"
            )
            return worktree_baseline_results
    _append_text(log_file, "validation-baseline: using repo-level baseline")
    return repo_baseline_results


def classify_review_feedback(feedback: str) -> str:
    """Classify review feedback as either 'needs-human' or 'actionable'."""
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
    """Decide whether a review loop is permitted for a PR."""
    author_ok = pr_author == bot_author
    tag_ok = explicit_tag and explicit_tag in pr_tags
    if author_ok or tag_ok:
        return True, "review-loop-policy-pass"
    return False, "review-loop-policy-block: non-bot PR without explicit tag"


# --- Wave 1, Item 3: Stale lock/worktree self-healing ---
# --- Wave 2.4: Reboot-resilient state enhancements ---

STALE_LOCK_HOURS = 4  # Locks older than this are considered stale
STALE_WORKTREE_DAYS = 7  # Worktree directories older than this may be cleaned


def _parse_worktree_list_porcelain(repo_path: Path) -> List[Dict[str, str]]:
    """Parse `git worktree list --porcelain` and return a list of dicts.

    Each dict has keys:
        - worktree: str (absolute path)
        - head: str (commit sha or empty)
        - branch: str (ref name, or 'detached' if detached HEAD)
        - bare: bool (True if this is the bare repo)

    Returns an empty list on error.
    """
    try:
        rc, output = run_capture(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_path,
        )
        if rc != 0 or not output:
            return []
    except Exception:
        return []

    worktrees: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            if current and "worktree" in current:
                worktrees.append(current)
            current = {}
            continue
        parts = line.split(" ", 1)
        key = parts[0]
        value = parts[1] if len(parts) > 1 else ""
        if key == "worktree":
            current["worktree"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value
        elif key == "detached":
            current["branch"] = "detached"
        elif key == "bare":
            current["bare"] = "true"
    if current and "worktree" in current:
        worktrees.append(current)
    return worktrees


def run_startup_self_healing(
    repo_path: Path,
    log_file: Optional[Path] = None,
    locks_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run self-healing on startup: clean stale locks, prune orphaned worktrees.

    Args:
        repo_path: Path to the git repository.
        log_file: Optional log file path.
        locks_dir: Optional path to lock files directory.
        dry_run: If True, only report what would be cleaned.

    Returns:
        Dict with keys:
            - stale_locks_removed: int
            - worktrees_pruned: bool
            - errors: List[str]
    """
    result: Dict[str, Any] = {
        "stale_locks_removed": 0,
        "worktrees_pruned": False,
        "errors": [],
    }

    # 1. Clean stale lock files
    if locks_dir is not None and locks_dir.exists():
        try:
            now = datetime.now(timezone.utc)
            for lock_file in locks_dir.iterdir():
                if not lock_file.is_file():
                    continue
                try:
                    mtime = datetime.fromtimestamp(
                        lock_file.stat().st_mtime, tz=timezone.utc
                    )
                    age_hours = (now - mtime).total_seconds() / 3600
                    if age_hours >= STALE_LOCK_HOURS:
                        if not dry_run:
                            lock_file.unlink()
                        result["stale_locks_removed"] += 1
                        if log_file:
                            _append_text(
                                log_file,
                                f"self-heal: removed stale lock {lock_file.name} "
                                f"age={age_hours:.1f}h",
                            )
                except (OSError, ValueError) as e:
                    result["errors"].append(f"lock-check:{lock_file.name}:{e}")
        except Exception as e:
            result["errors"].append(f"locks-dir:{e}")

    # 2. Prune orphaned git worktrees
    try:
        rc, output = run_capture(
            ["git", "worktree", "prune", "--verbose"],
            cwd=repo_path,
        )
        if rc == 0:
            result["worktrees_pruned"] = True
            if log_file:
                _append_text(log_file, "self-heal: git worktree prune completed")
        else:
            result["errors"].append(f"worktree-prune:rc={rc}")
    except Exception as e:
        result["errors"].append(f"worktree-prune:{e}")

    # 3. Orphaned worktree detection via git worktree list --porcelain
    orphaned_wt_count = 0
    try:
        listed_worktrees = _parse_worktree_list_porcelain(repo_path)
        listed_paths = {
            Path(w["worktree"]).resolve() for w in listed_worktrees if "worktree" in w
        }

        # Check directories under known worktree root for orphaned worktrees
        from bluei.engine.constants import DEFAULT_WORKTREE_ROOT

        if DEFAULT_WORKTREE_ROOT.exists():
            for wt_dir in DEFAULT_WORKTREE_ROOT.iterdir():
                if not wt_dir.is_dir():
                    continue
                resolved = wt_dir.resolve()
                if resolved in listed_paths:
                    continue
                if resolved == repo_path.resolve():
                    continue

                dotgit = resolved / ".git"
                if dotgit.exists():
                    if not dry_run:
                        run_no_capture(["rm", "-rf", str(resolved)], cwd=repo_path)
                    orphaned_wt_count += 1
                    if log_file:
                        _append_text(
                            log_file,
                            f"self-heal: removed orphaned worktree dir {wt_dir.name}",
                        )

        # Listed worktrees whose directories no longer exist on disk
        for wt in listed_worktrees:
            wt_path = Path(wt.get("worktree", "")).resolve()
            if wt_path == repo_path.resolve():
                continue
            if not wt_path.exists():
                branch_ref = wt.get("branch", "")
                if branch_ref and branch_ref != "detached":
                    branch_name = branch_ref.replace("refs/heads/", "", 1)
                    if not dry_run:
                        run_no_capture(
                            ["git", "branch", "-D", branch_name], cwd=repo_path
                        )
                if log_file:
                    _append_text(
                        log_file,
                        f"self-heal: cleaned missing worktree reference {wt_path}",
                    )
    except Exception as e:
        result["errors"].append(f"worktree-list-porcelain:{e}")

    if orphaned_wt_count > 0:
        if log_file:
            _append_text(
                log_file,
                f"self-heal: removed {orphaned_wt_count} orphaned worktree dir(s)",
            )

    # 4. Repair corrupted/missing state.json
    try:
        from bluei.engine.constants import DEFAULT_STATE

        state_path = (
            repo_path / "state.json" if not DEFAULT_STATE.exists() else DEFAULT_STATE
        )
        if state_path.exists():
            was_repaired = repair_state(state_path)
            if was_repaired:
                if log_file:
                    _append_text(
                        log_file,
                        f"self-heal: repaired corrupted state.json at {state_path}",
                    )
    except Exception as e:
        result["errors"].append(f"state-repair:{e}")
        if log_file:
            _append_text(log_file, f"self-heal: state repair failed ({e})")

    # 5. Clean stale batch state (batches.jsonl records with no corresponding worktree)
    stale_batch_count = 0
    try:
        from bluei.engine.constants import DEFAULT_BATCH_STATE

        if DEFAULT_BATCH_STATE.exists():
            batches = load_batches(DEFAULT_BATCH_STATE)
            active_batches: list[dict[str, Any]] = []
            for batch in batches:
                batch_branch = batch.get("branch", "")
                batch_wt_path = batch.get("worktree_path", "")
                if batch_branch:
                    # Check if the branch still exists
                    rc_check, _ = run_capture(
                        ["git", "rev-parse", "--verify", f"refs/heads/{batch_branch}"],
                        cwd=repo_path,
                    )
                    if rc_check != 0:
                        stale_batch_count += 1
                        if log_file:
                            _append_text(
                                log_file,
                                f"self-heal: stale batch record batch_id={batch.get('batch_id', '?')} branch={batch_branch}",
                            )
                        continue
                if batch_wt_path:
                    wt_p = Path(batch_wt_path)
                    if not wt_p.exists():
                        stale_batch_count += 1
                        if log_file:
                            _append_text(
                                log_file,
                                f"self-heal: stale batch record batch_id={batch.get('batch_id', '?')} worktree={batch_wt_path}",
                            )
                        continue
                active_batches.append(batch)

            if stale_batch_count > 0:
                if not dry_run:
                    with open(DEFAULT_BATCH_STATE, "w", encoding="utf-8") as f:
                        for batch in active_batches:
                            import json

                            f.write(json.dumps(batch, sort_keys=True) + "\n")
                if log_file:
                    _append_text(
                        log_file,
                        f"self-heal: removed {stale_batch_count} stale batch record(s) from {DEFAULT_BATCH_STATE.name}",
                    )
    except Exception as e:
        result["errors"].append(f"batch-state-clean:{e}")

    return result
