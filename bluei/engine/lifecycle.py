"""Fix lifecycle engine — the core value proposition of the QA agent.

Implements the detect → scaffold → apply → validate → commit state machine.
Three fix strategies are tried in cascade order: recipe → cascade → LLM.
Safety gates block destructive operations; failed validations auto-rollback.
"""

import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Any, Optional, Tuple

if TYPE_CHECKING:
    from bluei.engine.pattern_store import FixPatternStore

from bluei.engine.jsonl import append_jsonl
from bluei.engine.models import Finding, now_iso
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
)
from bluei.engine.prompts import render_claude_fix_prompt
from bluei.engine.refactor_queue import RefactorQueue, QueueStatus
from bluei.engine.constants import (
    DEFAULT_CLAUDE_CMD_TEMPLATE,
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

# Module-level singletons: lazy-initialized to avoid import-time side effects
# All mutable module state is declared here for auditability.
_recipe_engine: Optional[RecipeEngine] = None
_tiered_validator: Optional[TieredValidator] = None
_mnemo_clients: Dict[str, MnemoClient] = {}
_cached_stores: Dict[str, Any] = {}


def _get_recipe_engine() -> RecipeEngine:
    global _recipe_engine
    if _recipe_engine is None:
        _recipe_engine = RecipeEngine([builtin_recipe_dir(), staged_recipe_dir()])
    return _recipe_engine


def _get_tiered_validator() -> TieredValidator:
    global _tiered_validator
    if _tiered_validator is None:
        _tiered_validator = TieredValidator()
    return _tiered_validator


def _reset_lifecycle_cache() -> None:
    global _recipe_engine, _tiered_validator
    _recipe_engine = None
    _tiered_validator = None
    _mnemo_clients.clear()
    _cached_stores.clear()


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
    detected_frameworks: Optional[List[str]] = None,
) -> None:
    """Best-effort learned-pattern extraction for successful fixes.

    ``detected_frameworks`` is threaded through to ``extract()`` so the
    engine never reaches into the app layer for framework detection
    (rec-08). Callers that already have the list pass it; others rely on
    the default ``None``.
    """
    if pattern_store_path is None:
        return
    try:
        from bluei.engine.pattern_extractor import extract
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(pattern_store_path)
        # Only thread ``detected_frameworks`` when populated, so legacy
        # mocks of ``extract()`` with the original 5-positional-arg
        # signature keep working. Behavior is unchanged when the list
        # is None/empty (``_detect_framework`` returns None either way).
        extract_kwargs: Dict[str, Any] = {}
        if detected_frameworks:
            extract_kwargs["detected_frameworks"] = detected_frameworks
        pattern_id = extract(
            worktree_path,
            finding,
            fix_source,
            store,
            log_file,
            **extract_kwargs,
        )
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
                            from bluei.engine.governance import PromotionResult
                            from bluei.engine.recipe_engine import staged_recipe_dir

                            cross_pat = (
                                lib._get_cached(stored.language).get(
                                    f"{stored.rule}::{stored.structural_hash}"
                                )
                                if stored.structural_hash
                                else None
                            )
                            if cross_pat and check_promotion_eligibility(cross_pat):
                                from bluei.app.config import load_global_config

                                _promote_config = load_global_config()
                                _state_dir = (
                                    pattern_store_path.parent
                                    if pattern_store_path
                                    else None
                                )
                                _ar_path = (
                                    _state_dir / "approval_records.jsonl"
                                    if _state_dir
                                    else None
                                )

                                result = promote_pattern_to_recipe(
                                    cross_pat,
                                    stored.language,
                                    staged_recipe_dir(),
                                    config=_promote_config,
                                    repo_state_dir=_state_dir,
                                    approval_records_path=_ar_path,
                                )
                                if result == PromotionResult.PROMOTED:
                                    try:
                                        _get_recipe_engine().reload()
                                    except Exception:
                                        _logger.debug(
                                            "Pattern extract hook failed (top-level)"
                                        )
                                elif result == PromotionResult.PENDING_APPROVAL:
                                    _logger.info(
                                        "Pattern promotion queued for approval: %s",
                                        cross_pat.pattern_id,
                                    )
                                # NOT_ELIGIBLE: no action
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


# ── Claude fix engine ──────────────────────────────────────────────────


@dataclass
class ClaudeFixRequest:
    """Bundle of parameters for ``apply_claude_fix``.

    Replaces the former 15-param signature; two reserved params
    (``repo_path``, ``prompt``) were never read and are removed.
    """

    worktree_path: Path
    finding: Finding
    baseline_checks: Dict[str, List[str]]
    target_checks: Dict[str, List[str]]
    claude_cmd_template: str
    max_files_changed: int
    max_loc_diff: int
    log_file: Path
    findings_file: Optional[Path] = None
    lessons_file: Optional[Path] = None
    extra_prompt: Optional[str] = None
    pattern_store_path: Optional[Path] = None
    learned_patterns: Optional[str] = None
    detected_frameworks: Optional[List[str]] = None
    authoritative_guidelines: Optional[str] = None
    repo_taste: Optional[str] = None
    baseline_statuses: Optional[List[Optional[int]]] = None
    allow_unchanged_baseline_failures: bool = True


def apply_claude_fix(req: ClaudeFixRequest) -> Tuple[int, str, str]:
    """Apply a fix using the Claude fix engine."""
    # --- Directive seeding: load prior context from LESSONS_LOG + findings.jsonl ---
    fix_history: Optional[List[Dict[str, Any]]] = None
    finding_record: Optional[Dict[str, Any]] = None
    rule_history: Optional[List[Dict[str, Any]]] = None
    failure_clusters: Optional[str] = None

    if req.lessons_file is not None:
        fix_history = load_lessons_for_finding(req.finding.finding_id, req.lessons_file)
        rule_history = load_lessons_for_rule(
            req.finding.rule, req.lessons_file, limit=5
        )
        failure_clusters = load_failure_clusters_for_rule(
            req.finding.rule, req.lessons_file
        )
    if req.findings_file is not None:
        finding_record = load_finding_record(req.finding.finding_id, req.findings_file)

    prompt_path = req.worktree_path / QA_FIX_PROMPT_FILENAME
    prompt_text = render_claude_fix_prompt(
        finding=req.finding,
        baseline_checks={} if req.baseline_statuses is not None else req.baseline_checks,
        target_checks=req.target_checks,
        max_files_changed=req.max_files_changed,
        max_loc_diff=req.max_loc_diff,
        fix_history=fix_history,
        finding_record=finding_record,
        extra_prompt=req.extra_prompt,
        learned_patterns=req.learned_patterns,
        rule_history=rule_history,
        failure_clusters=failure_clusters,
        authoritative_guidelines=req.authoritative_guidelines,
        repo_taste=req.repo_taste,
        **({"baseline_statuses": req.baseline_statuses,
            "allow_unchanged_baseline_failures": req.allow_unchanged_baseline_failures}
           if req.baseline_statuses is not None else {}),
    )
    prompt_path.write_text(prompt_text, encoding="utf-8")

    try:
        try:
            command = req.claude_cmd_template.format(
                prompt_file=shlex.quote(str(prompt_path)),
                finding_id=shlex.quote(req.finding.finding_id),
                rule=shlex.quote(req.finding.rule),
                path=shlex.quote(req.finding.path),
            )
        except KeyError as exc:
            error = f"invalid claude command template placeholder: {exc}"
            _append_text(req.log_file, f"claude-fix: {error}")
            return 2, error, str(prompt_path)

        _append_text(
            req.log_file,
            "claude-fix: "
            f"finding_id={req.finding.finding_id} prompt_file={prompt_path} "
            f"cmd={sanitize_command_template(command)}",
        )
        res = subprocess.run(
            ["bash", "-l", "-c", command],
            cwd=str(req.worktree_path),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
        )
        output = (res.stdout or "").strip()
        _append_text(
            req.log_file,
            "claude-fix-result: "
            f"finding_id={req.finding.finding_id} rc={res.returncode} output={(output or '<empty>')[:1000]}",
        )

        # --- Directive seeding: record attempt/failure observations only ---
        # A04 acceptance boundary: a bare rc0 from the provider transport is
        # NOT acceptance. Positive emissions (fix_success record, success
        # lesson, pattern extraction) live in record_accepted_fix(), invoked
        # by the caller only at final acceptance — rc0 alone never learns.
        success = res.returncode == 0
        if req.findings_file is not None:
            increment_fix_attempt(
                req.finding.finding_id,
                req.findings_file,
                error=None if success else (output or "fix failed")[:500],
            )
        if req.lessons_file is not None and not success:
            location = f"{req.finding.path}:{req.finding.line}"
            error_summary = (output or "fix failed")[:200].split("\n")[-1]
            append_lesson(
                req.lessons_file,
                "fix",
                finding_id=req.finding.finding_id,
                what_broke=f"fix failed rc={res.returncode} rule={req.finding.rule} path={location}: {error_summary}",
            )

        return res.returncode, output, str(prompt_path)
    finally:
        try:
            prompt_path.unlink(missing_ok=True)
        except Exception:
            _logger.debug("Failed to unlink prompt temp file")


def record_accepted_fix(req: ClaudeFixRequest, fix_source: str = "claude") -> None:
    """Record the positive outcome of an accepted fix.

    Call only after final acceptance; ``apply_claude_fix`` alone (including
    a bare rc0) never emits these. Writes the ``fix_success`` finding-record
    flag, the success lesson, and learned-pattern extraction — a ``None``
    ``findings_file`` / ``lessons_file`` / ``pattern_store_path`` disables
    the respective write, so paused learning suppresses the learning sinks
    (lesson/pattern) while the accepted fix status may still be recorded.
    ``fix_source`` labels the winning strategy in the pattern store. Sink
    failures are best-effort (logged, skipped) since acceptance is already
    durable; exactly-once invocation is the caller's contract.
    """
    if req.findings_file is not None:
        try:
            update_finding_record(
                req.finding.finding_id,
                req.findings_file,
                {"fix_success": True},
            )
        except Exception as exc:
            _logger.warning(
                "record_accepted_fix: fix_success write failed finding_id=%s: %s",
                req.finding.finding_id,
                exc,
            )
    if req.lessons_file is not None:
        location = f"{req.finding.path}:{req.finding.line}"
        try:
            append_lesson(
                req.lessons_file,
                "fix",
                finding_id=req.finding.finding_id,
                what_changed=f"fix succeeded rule={req.finding.rule} path={location}",
            )
        except Exception as exc:
            _logger.warning(
                "record_accepted_fix: success lesson write failed finding_id=%s: %s",
                req.finding.finding_id,
                exc,
            )
    if req.pattern_store_path is not None:
        _extract_fix_pattern(
            req.worktree_path,
            req.finding,
            fix_source,
            req.pattern_store_path,
            req.log_file,
            detected_frameworks=req.detected_frameworks,
        )


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


def _tier_validate(
    tier: FixTier,
    worktree_path: Path,
    finding: Finding,
    log_file: Path,
    state: Optional[Dict[str, Any]] = None,
    checks: Optional[Dict[str, List[str]]] = None,
    environment_repo: Optional[Path] = None,
) -> bool:
    """Run tiered validation and auto-rollback on failure.

    Args:
        tier: The FixTier to validate against.
        worktree_path: Path to the worktree.
        finding: The Finding being validated.
        log_file: Path to the run log.
        state: Optional runner state dict for tier escalation checks.
        checks: Optional configured check commands (tool → args). When
            supplied the tiers run the caller's REAL checks instead of
            validating against nothing; when None the legacy tier behavior
            is unchanged (explicitly weaker — callers owe an outer
            qualified gate).
        environment_repo: Optional source repo whose local venv defines the
            intended tool runtime (threaded to the T1 check runner's
            existing shadow guard; None keeps existing default semantics).

    Returns:
        True if validation passed, False if rolled back or routed to human review.
    """
    result = _get_tiered_validator().validate(
        tier,
        worktree_path,
        finding,
        log_file=log_file,
        checks=checks,
        environment_repo=environment_repo,
    )
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


def _transactional_autofix(func):
    """Protect the public deterministic recipe and legacy fix path.

    Pattern learning is suppressed inside the guarded call
    (``pattern_store_path=None``) and performed once here, only after tier
    acceptance and the scope check, so a rejected fix can never be recorded
    as a learned pattern.
    """
    from functools import wraps

    @wraps(func)
    def wrapped(worktree_path, finding, log_file, pattern_store_path=None, detected_frameworks=None):
        from bluei.engine.cascade import default_checkpoint_dir
        from bluei.engine.worktree_checkpoint import (
            CheckpointError, capture_checkpoint, changed_paths, discard_checkpoint, restore_checkpoint,
        )

        root = Path(worktree_path).resolve()
        target = (root / finding.path).resolve().relative_to(root)
        checkpoint = capture_checkpoint(root, default_checkpoint_dir(), exclude_paths=[log_file])
        restored = False

        def _restore_and_release() -> None:
            nonlocal restored
            restore_checkpoint(checkpoint)
            restored = True
            discard_checkpoint(checkpoint)

        try:
            accepted = func(worktree_path, finding, log_file, None, detected_frameworks)
            if accepted:
                allowed = {target.as_posix()}
                allowed.update(
                    p.as_posix() for p in target.parents
                    if p.as_posix() != "." and p.as_posix() not in checkpoint.entries
                )
                changes = changed_paths(checkpoint)
                index = checkpoint.index_target
                if index is not None:
                    if checkpoint.index_backup is None:
                        if index.exists():
                            changes.add("<git-index>")
                    elif not index.is_file() or index.read_bytes() != checkpoint.index_backup.read_bytes():
                        changes.add("<git-index>")
                if changes - allowed:
                    _append_text(log_file, f"autofix: out-of-scope changes: {sorted(changes - allowed)}")
                    accepted = False
            if not accepted:
                _restore_and_release()
                return False
            # Accepted: learn once BEFORE releasing the backup, so a raising
            # extraction still restores the pre-fix state.
            try:
                source = "autofix"
                try:
                    before = checkpoint._backup_for(target.as_posix()).read_text(
                        encoding="utf-8", errors="replace"
                    )
                    if _get_recipe_engine().match(finding.rule, "python", file_text=before) is not None:
                        source = "recipe"
                except Exception:
                    pass
                _extract_fix_pattern(
                    worktree_path, finding, source, pattern_store_path, log_file,
                    detected_frameworks=detected_frameworks,
                )
            except BaseException:
                _restore_and_release()
                raise
        except BaseException:
            # Includes KeyboardInterrupt/SystemExit: restore unless already
            # restored or the failed restore retained its backup. Bare raise
            # keeps the original exception.
            if not restored and not checkpoint.retained:
                _restore_and_release()
            raise
        # Acceptance and learning finished. Cleanup cannot undo accepted source
        # using a checkpoint that may already have been deleted or closed.
        try:
            discard_checkpoint(checkpoint)
        except OSError as exc:
            raise CheckpointError(
                f"autofix accepted, but checkpoint cleanup failed; stop before further fixes: {checkpoint.backup_dir}"
            ) from exc
        finally:
            if checkpoint.git_dir_fd is not None:
                os.close(checkpoint.git_dir_fd)
                checkpoint.git_dir_fd = None
        return True

    return wrapped


@_transactional_autofix
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
    recipe = _get_recipe_engine().match(finding.rule, "python", file_text=file_text)
    if recipe:
        result = _get_recipe_engine().apply(
            recipe, file_path, worktree_path, finding=finding
        )
        if result.success:
            _append_text(
                log_file,
                f"autofix: recipe={recipe.id} applied finding_id={finding.finding_id} rule={finding.rule}",
            )
            # Pattern learning is deferred to the accepted boundary in the
            # _transactional_autofix wrapper (pattern_store_path is None here).
            tier = resolve_tier(finding, recipe=recipe)
            if not _tier_validate(tier, worktree_path, finding, log_file):
                return False
            return True
        _append_text(
            log_file,
            f"autofix: recipe={recipe.id} failed finding_id={finding.finding_id} rule={finding.rule} error={result.error}",
        )
        # A failed recipe may have partially mutated any file. The outer
        # checkpoint restores it before control returns to the caller.
        return False

    # --- No recipe matched: try legacy hardcoded fixes ---
    # --- Phase 2: Legacy hardcoded fixes (deprecated, remove after migration) ---
    text = file_path.read_text(encoding="utf-8")
    updated = text

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
    # Pattern learning is deferred to the accepted boundary in the
    # _transactional_autofix wrapper (pattern_store_path is None here).
    tier = resolve_tier(finding)
    if not _tier_validate(tier, worktree_path, finding, log_file):
        return False
    return True


def _get_or_create_store(pattern_store_path: Path) -> Optional["FixPatternStore"]:
    key = str(pattern_store_path)
    cached = _cached_stores.get(key)
    if cached is not None:
        return cached
    from bluei.engine.pattern_store import open_pattern_store

    store = open_pattern_store(pattern_store_path)
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


def _deferred_candidate_record(
    *,
    origin: str,
    backend: str,
    finding: Finding,
    stage_name: str,
    pattern_id: Optional[str],
    authorized_scope: Any,
    telemetry: Any,
    latency_ms: int,
    model_attempt: bool,
    deterministic_attempt: bool,
    model_invocations: int,
    deterministic_invocations: int,
    model_request: Optional[Dict[str, Any]],
    environment_repo: Optional[Path],
    qualified: bool,
    confirmations: List[Any],
    log_file: Path,
    outcome: Optional[str] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one structured deferred-acceptance candidate record.

    The record is honest evidence for the caller's outer acceptance gate
    (attempt counters are recorded independently of acceptance).
    ``model_invocations`` / ``deterministic_invocations`` are ACTUAL
    invocation counts for this apply_cascade_fix call including failed
    attempts (failed cascade stages, the model engine call); they are
    never inferred from the outcome. ``model_request`` carries the model
    request identity (backend + configured command template) and is None
    when no model invocation happened. The
    embedded one-shot ``confirm`` callable is the ONLY path by which the
    deferred positive effects (replay HIT, composite success count,
    savings, pattern extraction, ledger success emission) run. Each
    confirmation is best-effort at confirm time — acceptance already
    committed the source, so a failing record write must never roll it
    back (records-must-match-tree rule); it logs instead.
    """
    done = {"confirmed": False}

    def _confirm() -> None:
        if done["confirmed"]:
            return
        done["confirmed"] = True
        for fn in confirmations:
            try:
                fn()
            except Exception as exc:
                try:
                    _append_text(
                        log_file,
                        f"cascade-deferred-confirm: confirmation error="
                        f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    pass

    return {
        "kind": "cascade-attempt" if outcome is not None else "cascade-candidate",
        "outcome": outcome,
        "error": error,
        "origin": origin,
        "backend": backend,
        "finding_id": finding.finding_id,
        "rule": finding.rule,
        "stage_name": stage_name,
        "pattern_id": pattern_id,
        "authorized_scope": (
            sorted(authorized_scope)
            if isinstance(authorized_scope, (set, frozenset, list))
            else None
        ),
        "latency_ms": latency_ms,
        "model_attempt": model_attempt,
        "deterministic_attempt": deterministic_attempt,
        "model_invocations": model_invocations,
        "deterministic_invocations": deterministic_invocations,
        "model_request": model_request,
        "stages_tried": (
            list(telemetry.stages_tried) if telemetry is not None else []
        ),
        "stages_failed": (
            [dict(x) for x in telemetry.stages_failed]
            if telemetry is not None
            else []
        ),
        "stages_skipped": (
            list(telemetry.stages_skipped) if telemetry is not None else []
        ),
        "telemetry": telemetry.to_dict() if telemetry is not None else None,
        "environment_repo": str(environment_repo) if environment_repo else None,
        "qualified": qualified,
        "confirm": _confirm,
    }


def apply_cascade_fix(
    worktree_path: Path,
    finding: Finding,
    log_file: Path,
    pattern_store_path: Optional[Path] = None,
    detected_frameworks: Optional[list] = None,
    deterministic_only: bool = False,
    ledger_path: Optional[Path] = None,
    cycle: Optional[str] = None,
    ledger_records: Optional[List[Dict[str, Any]]] = None,
    run_id: str = "",
    cost_tracker: Any = None,
    governance_state: Optional[Dict[str, str]] = None,
    attempted_pattern_ids: Optional[set] = None,
    baseline_checks: Optional[Dict[str, List[str]]] = None,
    baseline_results: Optional[Dict[str, Dict[str, Any]]] = None,
    environment_repo: Optional[Path] = None,
    claude_cmd_template: Optional[str] = None,
    backend: str = "claude",
    allow_unchanged_baseline_failures: bool = True,
    deferred_acceptance: Optional[List[Dict[str, Any]]] = None,
    max_files_changed: int = 5,
    max_loc_diff: int = 200,
) -> bool:
    """Apply the deterministic cascade to fix a finding.

    ``attempted_pattern_ids`` carries pattern ids the caller already tried
    for this finding (standalone replay); the cascade's pattern stage
    excludes them so outcomes record exactly once per pattern.

    Configured acceptance context (A03/A10): ``baseline_checks`` /
    ``baseline_results`` / ``environment_repo`` /
    ``claude_cmd_template`` / ``backend`` /
    ``allow_unchanged_baseline_failures`` carry the caller's real checks,
    source runtime, governed model template with explicit backend identity
    and baseline failure policy. The tier gate runs the configured checks
    (never an invented sample); the model fallback runs ONLY with a
    configured ``claude_cmd_template`` — without one the fallback is an
    explicit unqualified refusal, and the built-in default template and
    sample baseline checks are never substituted. ``deterministic_only``
    blocks the model fallback regardless, a cost tracker at its hard limit
    blocks it before any invocation, and ``max_files_changed`` /
    ``max_loc_diff`` bound the candidate diff handed to the model engine
    (defaults 5/200 preserved for direct callers).

    ``deferred_acceptance`` (a list supplied by the caller) switches the
    success paths to candidate mode: instead of emitting the ledger record
    and learned pattern here, a structured candidate record is appended
    (origin/backend/stage/pattern/scope/latency, honest attempt counters
    ``model_attempt`` / ``deterministic_attempt`` / stage lists, and a
    one-shot ``confirm`` callable). Nothing positive — pattern HIT,
    composite success count, replay savings, pattern extraction, ledger
    success — persists until the caller's outer durable acceptance invokes
    ``record["confirm"]()``. Negative outcomes (exhausted, rejected) are
    recorded immediately and honestly. When ``deferred_acceptance`` is
    None the legacy eager behavior is preserved for direct callers.

    Transactional: a worktree checkpoint spans the cascade AND the tier
    validation below. The acceptance checks (tier validation, scope
    recheck) plus learning are guarded: any abort — interruption, logging
    failure, or error — restores the exact pre-attempt state. Ordinary
    scope-check errors are honest rejections (False) rather than cycle
    aborts; interrupts and restore failures always propagate (a failed
    restore halts — no LLM fallback). Once the checks and learning pass,
    the fix is COMMITTED: cleanup and ledger emission run past the guard so
    an interrupt there never rolls back accepted source (records must match
    the tree), and rejection records an honest exhausted outcome instead.
    """
    from bluei.engine.cascade import (
        CascadeAcceptanceContext,
        CascadeContext,
        DeterministicCascade,
        default_cascade_stages,
        default_checkpoint_dir,
    )
    from bluei.engine.worktree_checkpoint import (
        CheckpointError,
        capture_checkpoint,
        discard_checkpoint,
        restore_checkpoint,
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

    pattern_store: Optional["FixPatternStore"] = None
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

    recipe_engine = _get_recipe_engine()

    replay_threshold_overrides: Dict[str, float] = {}

    checkpoint = None
    try:
        checkpoint = capture_checkpoint(
            worktree_path,
            default_checkpoint_dir(),
            exclude_paths=[
                log_file,
                *([ledger_path] if ledger_path is not None else []),
            ],
        )
    except CheckpointError as exc:
        try:
            _append_text(log_file, f"cascade: checkpoint capture failed: {exc}")
        except BaseException:
            pass  # Preserve the checkpoint failure so dispatch cannot fall back.
        raise

    acceptance = None
    if (
        baseline_checks
        or baseline_results
        or environment_repo is not None
        or claude_cmd_template
        or backend != "claude"
    ):
        acceptance = CascadeAcceptanceContext(
            baseline_checks=baseline_checks or {},
            baseline_results=baseline_results or {},
            environment_repo=environment_repo,
            claude_cmd_template=claude_cmd_template,
            backend=backend,
            allow_unchanged_baseline_failures=allow_unchanged_baseline_failures,
        )

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
        ledger_path=ledger_path,
        cycle=cycle,
        ledger_records=ledger_records,
        run_id=run_id,
        cost_tracker=cost_tracker,
        governance_state=governance_state or {},
        attempted_pattern_ids=attempted_pattern_ids,
        checkpoint=checkpoint,
        # Real configured check commands for the replay stage (previously
        # always {} — replay validated against nothing).
        baseline_results=baseline_checks or {},
        acceptance=acceptance,
        defer_success_evidence=deferred_acceptance is not None,
    )

    cascade = DeterministicCascade(default_cascade_stages())
    try:
        result = cascade.execute(finding, worktree_path, context)
    except BaseException:
        # Includes KeyboardInterrupt/SystemExit: restore unless the failed
        # restore already retained its backup. Bare raise keeps the original.
        if not checkpoint.retained:
            restore_checkpoint(checkpoint)
            discard_checkpoint(checkpoint)
        raise

    def _emit_fallback_record(outcome: str, final_stage: str) -> None:
        """Emit a fallback resolution record (LLM success or exhaustion)."""
        if ledger_path is None and ledger_records is None:
            return
        record: Dict[str, Any] = {
            "cycle": cycle,
            "finding_id": finding.finding_id,
            "rule": finding.rule,
            "stages_tried": [],
            "final_stage": final_stage,
            "outcome": outcome,
            "via": "cascade",
            "pattern_id": None,
            "latency_ms": result.latency_ms,
            "timestamp": now_iso(),
        }
        if ledger_records is not None:
            ledger_records.append(record)
        if ledger_path is not None:
            try:
                append_jsonl(ledger_path, record)
            except Exception:
                pass

    def _discard_checkpoint() -> None:
        nonlocal checkpoint
        if checkpoint is None:
            return
        releasing = checkpoint
        checkpoint = None  # Cleanup must never make an accepted/restored source retry rollback.
        try:
            discard_checkpoint(releasing)
        except OSError as exc:
            raise CheckpointError(
                f"checkpoint cleanup failed; stop before further fixes: {releasing.backup_dir}"
            ) from exc
        finally:
            if releasing.git_dir_fd is not None:
                os.close(releasing.git_dir_fd)
                releasing.git_dir_fd = None

    restored = False

    def _log_best_effort(message: str) -> None:
        """Append to the run log without aborting an already-rolled-back flow."""
        try:
            _append_text(log_file, message)
        except Exception:
            pass

    def _restore_release() -> None:
        """Restore the exact pre-attempt state, then release the backup.

        The worktree is marked restored BEFORE cleanup so an interrupt
        during cleanup can never trigger a second restore from an already
        discarded backup. A failed restore raises CheckpointRestoreError
        (fd closed, backup retained for recovery) — the caller halts; no
        LLM fallback runs.
        """
        nonlocal restored
        restore_checkpoint(checkpoint)
        restored = True
        _discard_checkpoint()

    def _attempt_record(
        *,
        outcome: str,
        origin: str,
        stage_name: str,
        model_invocations: int,
        model_request: Optional[Dict[str, Any]],
        error: Optional[str] = None,
    ) -> None:
        """Append one honest attempt/failure record in defer mode.

        Every False return path in defer mode appends exactly one record so
        the caller's counters reflect ACTUAL invocations — failed cascade
        stages and failed model engine calls included — instead of a blanket
        fallback count. Attempt records carry no positive confirmations
        (``confirm`` is a one-shot no-op); negative ledger evidence stays
        immediate.
        """
        if deferred_acceptance is None:
            return
        det = (
            len(result.telemetry.stages_tried)
            if result.telemetry is not None
            and isinstance(getattr(result.telemetry, "stages_tried", None), list)
            else (1 if result.success else 0)
        )
        deferred_acceptance.append(
            _deferred_candidate_record(
                origin=origin,
                backend=backend,
                finding=finding,
                stage_name=stage_name,
                pattern_id=None,
                authorized_scope=None,
                telemetry=result.telemetry,
                latency_ms=result.latency_ms,
                model_attempt=model_invocations > 0,
                deterministic_attempt=det > 0,
                model_invocations=model_invocations,
                deterministic_invocations=det,
                model_request=model_request,
                environment_repo=environment_repo,
                qualified=bool(baseline_checks),
                confirmations=[],
                log_file=log_file,
                outcome=outcome,
                error=error,
            )
        )

    if result.success:
        # Acceptance-phase guard: tier validation, scope recheck, and
        # learning roll back on ANY abort — the winning fix is already
        # applied, so even a read-only check being interrupted must
        # restore. Ordinary scope-check errors become honest rejections
        # (False), not cycle aborts; interrupts and restore failures
        # always propagate.
        committed = False
        try:
            tier = resolve_tier(finding)
            reject_reason: Optional[str] = None
            if (
                deferred_acceptance is None
                and acceptance is not None
                and not baseline_checks
            ):
                # Production context with zero configured checks cannot
                # earn legacy eager success: the tier gate would validate
                # against nothing. Fail closed (restore + exhausted); the
                # deferred lane instead records an honest unqualified
                # candidate via its own contract.
                reject_reason = (
                    "unqualified: acceptance context supplied without "
                    "configured baseline checks"
                )
            else:
                accepted = _tier_validate(
                    tier,
                    worktree_path,
                    finding,
                    log_file,
                    checks=baseline_checks,
                    environment_repo=environment_repo,
                )
                if not accepted:
                    reject_reason = f"tier validation rejected stage={result.stage_name}"
                else:
                    # Post-tier scope recheck: tier validation runs AFTER
                    # execute() verified the declared stage scope, and its
                    # checks can themselves edit undeclared files or the Git
                    # index. Re-verify the cascade's declared scope (carried on
                    # the result — never the stage's claimed diff). A missing
                    # carrier fails closed.
                    allowed = result.authorized_scope
                    if allowed is None:
                        reject_reason = (
                            f"scope carrier missing stage={result.stage_name}"
                        )
                    else:
                        try:
                            out_of_scope = cascade._out_of_scope_edits(
                                worktree_path, allowed, checkpoint
                            )
                        except Exception as exc:
                            # Ordinary check error (disk, permissions): honest
                            # rejection. Interrupts are BaseException and fall
                            # through to the guard below.
                            reject_reason = (
                                f"scope recheck failed stage={result.stage_name} "
                                f"error={type(exc).__name__}: {exc}"
                            )
                        else:
                            if out_of_scope is not None:
                                reject_reason = (
                                    f"tier validation introduced out-of-scope "
                                    f"edits={sorted(out_of_scope)} "
                                    f"stage={result.stage_name}"
                                )
            if reject_reason is not None:
                # Rejection: restore first (marking restored before cleanup),
                # then log best-effort and record the honest outcome
                # (consumers only distinguish resolved_deterministic /
                # resolved_llm / exhausted).
                if checkpoint is not None:
                    _restore_release()
                _log_best_effort(
                    f"cascade: {reject_reason} — restored pre-attempt state"
                )
                _emit_fallback_record("exhausted", result.stage_name)
                _attempt_record(
                    outcome="rejected",
                    origin="deterministic",
                    stage_name=result.stage_name,
                    model_invocations=0,
                    model_request=None,
                )
                return False
            if deferred_acceptance is None:
                _extract_fix_pattern(
                    worktree_path, finding, result.stage_name,
                    pattern_store_path, log_file,
                    detected_frameworks=detected_frameworks,
                )
            # Acceptance checks and learning passed: COMMIT. The applied
            # fix is final from here — cleanup and ledger emission run
            # past the guard so an interrupt there never rolls back
            # accepted source (a success record must match the tree).
            committed = True
        except BaseException:
            # Includes KeyboardInterrupt/SystemExit. Restore unless this
            # flow already committed, already restored, or a failed restore
            # retained the backup; a failed restore's
            # CheckpointRestoreError wins (halt — no ledger record, no LLM
            # fallback).
            if (
                not committed
                and not restored
                and checkpoint is not None
                and not checkpoint.retained
            ):
                restore_checkpoint(checkpoint)
            _discard_checkpoint()
            raise
        _discard_checkpoint()
        if deferred_acceptance is not None:
            # Candidate mode: success is provisional. Hand the structured
            # record (with honest attempt counters and the one-shot
            # confirm) to the caller; no ledger emission, no learning,
            # no positive store outcome until the caller's outer durable
            # acceptance confirms.
            confirmations: List[Any] = list(result.pending_confirmations)
            if pattern_store_path is not None:
                confirmations.append(
                    lambda: _extract_fix_pattern(
                        worktree_path, finding, result.stage_name,
                        pattern_store_path, log_file,
                        detected_frameworks=detected_frameworks,
                    )
                )
            if result.telemetry is not None:
                confirmations.append(
                    lambda: cascade._emit_ledger_record(
                        context, result.telemetry,
                        outcome="resolved_deterministic",
                    )
                )
            deferred_acceptance.append(
                _deferred_candidate_record(
                    origin="deterministic",
                    backend=backend,
                    finding=finding,
                    stage_name=result.stage_name,
                    pattern_id=result.pattern_id,
                    authorized_scope=result.authorized_scope,
                    telemetry=result.telemetry,
                    latency_ms=result.latency_ms,
                    model_attempt=False,
                    deterministic_attempt=(
                        bool(result.telemetry.stages_tried)
                        if result.telemetry is not None
                        and isinstance(
                            getattr(result.telemetry, "stages_tried", None), list
                        )
                        else True
                    ),
                    # Actual invocation counts for this call: the winning
                    # deterministic stages (failures included), no model run.
                    model_invocations=0,
                    deterministic_invocations=(
                        len(result.telemetry.stages_tried)
                        if result.telemetry is not None
                        and isinstance(
                            getattr(result.telemetry, "stages_tried", None), list
                        )
                        else 1
                    ),
                    model_request=None,
                    environment_repo=environment_repo,
                    qualified=bool(baseline_checks),
                    confirmations=confirmations,
                    log_file=log_file,
                )
            )
            _log_best_effort(
                f"cascade: candidate deferred finding_id={finding.finding_id} "
                f"stage={result.stage_name} — awaiting outer acceptance"
            )
            return True
        # Committed: NOW persist the deterministic success (deferred from
        # the cascade so a tier rejection can never over-report).
        if result.telemetry is not None:
            cascade._emit_ledger_record(
                context, result.telemetry, outcome="resolved_deterministic"
            )
        return True

    # Cascade failed (exhausted or checkpoint_unsupported): every failed stage
    # already restored the pre-attempt state inside execute(); release the
    # backup before any LLM handoff.
    _discard_checkpoint()

    if context.allow_llm:
        # A10: the model fallback runs ONLY with a caller-configured
        # command template carrying its explicit backend identity. The
        # built-in default template and sample baseline checks are never
        # substituted — an unconfigured fallback is an explicit refusal.
        unqualified_fallback = None
        if not claude_cmd_template:
            unqualified_fallback = "no configured command template"
        elif (
            acceptance is not None
            and not baseline_checks
            and deferred_acceptance is None
        ):
            # Production context (any acceptance kwarg) with zero configured
            # checks must not bypass honesty through an eager model success.
            unqualified_fallback = (
                "production context without configured baseline checks"
            )
        if unqualified_fallback:
            _append_text(
                log_file,
                f"cascade: model fallback unqualified: {unqualified_fallback} "
                f"backend={backend} finding_id={finding.finding_id}",
            )
            _emit_fallback_record("exhausted", "cascade_exhausted")
            _attempt_record(
                outcome="unqualified-fallback",
                origin="deterministic",
                stage_name="cascade_exhausted",
                model_invocations=0,  # model never invoked: nothing to hide
                model_request=None,
            )
            return False
        if cost_tracker is not None and cost_tracker.exceeded_limit():
            # Cost guard BEFORE the model: nothing is invoked, estimated or
            # recorded — an honest unqualified attempt with zero counts.
            _append_text(
                log_file,
                f"cascade: model fallback unqualified: cost limit exceeded "
                f"backend={backend} finding_id={finding.finding_id}",
            )
            _emit_fallback_record("exhausted", "cascade_exhausted")
            _attempt_record(
                outcome="cost-limit",
                origin="deterministic",
                stage_name="cascade_exhausted",
                model_invocations=0,
                model_request=None,
            )
            return False
        _append_text(
            log_file,
            f"cascade: falling to model finding_id={finding.finding_id} "
            f"rule={finding.rule} backend={backend}",
        )
        llm_baseline_statuses: Optional[List[Optional[int]]] = None
        if baseline_results is not None:
            # Private baseline statuses (rc per configured check) — the
            # prompt never receives fabricated sample checks; the request's
            # check dict is blanked inside apply_claude_fix for this path.
            llm_baseline_statuses = [
                record.get("rc") for record in baseline_results.values()
            ]

        def _record_model_cost() -> None:
            """Ledger accounting labeled by the backend identity with
            nominal token estimates — never a billing claim. Best-effort:
            a ledger write failure must not mask a repair outcome."""
            if cost_tracker is None:
                return
            try:
                cost_tracker.record_invocation(
                    model=backend, input_tokens=3000, output_tokens=300
                )
            except Exception as exc:
                _log_best_effort(
                    f"cascade: cost ledger write failed: "
                    f"{type(exc).__name__}: {exc}"
                )

        rc: Optional[int] = None
        model_error: Optional[str] = None
        model_request = {
            "backend": backend,
            "claude_cmd_template": claude_cmd_template,
        }
        try:
            rc, _output, _prompt = apply_claude_fix(
                ClaudeFixRequest(
                    worktree_path=worktree_path,
                    finding=finding,
                    baseline_checks=dict(baseline_checks or {}),
                    target_checks={},
                    claude_cmd_template=claude_cmd_template,
                    max_files_changed=max_files_changed,
                    max_loc_diff=max_loc_diff,
                    log_file=log_file,
                    pattern_store_path=pattern_store_path,
                    detected_frameworks=detected_frameworks,
                    baseline_statuses=llm_baseline_statuses,
                    allow_unchanged_baseline_failures=(
                        allow_unchanged_baseline_failures
                    ),
                ),
            )
        except Exception as exc:
            # Ordinary engine failure (subprocess/OS/timeout): the model WAS
            # invoked — count it and categorize the error honestly.
            model_error = f"{type(exc).__name__}: {exc}"
        except BaseException:
            # Interrupts: the engine call was launched. Account for the
            # invocation first so the caller's counters never lose it, then
            # re-raise unchanged (caller's finally consumes the record).
            _record_model_cost()
            _attempt_record(
                outcome="model-interrupted",
                origin="llm",
                stage_name="llm",
                model_invocations=1,
                model_request=model_request,
            )
            raise
        _record_model_cost()
        if model_error is not None:
            _append_text(
                log_file,
                f"cascade: model engine error={model_error} "
                f"finding_id={finding.finding_id}",
            )
            _emit_fallback_record("exhausted", "cascade_exhausted")
            _attempt_record(
                outcome="model-error",
                origin="llm",
                stage_name="llm",
                model_invocations=1,
                model_request=model_request,
                error=model_error,
            )
            return False
        if rc == 0:
            if deferred_acceptance is not None:
                deferred_acceptance.append(
                    _deferred_candidate_record(
                        origin="llm",
                        backend=backend,
                        finding=finding,
                        stage_name="llm",
                        pattern_id=None,
                        authorized_scope=None,
                        telemetry=result.telemetry,
                        latency_ms=result.latency_ms,
                        model_attempt=True,
                        deterministic_attempt=(
                            bool(result.telemetry.stages_tried)
                            if result.telemetry is not None
                            and isinstance(
                                getattr(result.telemetry, "stages_tried", None),
                                list,
                            )
                            else False
                        ),
                        # Actual invocation counts for this call: every
                        # failed deterministic stage plus the one model
                        # engine invocation that produced the candidate.
                        model_invocations=1,
                        deterministic_invocations=(
                            len(result.telemetry.stages_tried)
                            if result.telemetry is not None
                            and isinstance(
                                getattr(result.telemetry, "stages_tried", None),
                                list,
                            )
                            else 0
                        ),
                        model_request={
                            "backend": backend,
                            "claude_cmd_template": claude_cmd_template,
                        },
                        environment_repo=environment_repo,
                        qualified=bool(baseline_checks),
                        confirmations=[
                            lambda: _emit_fallback_record("resolved_llm", "llm")
                        ],
                        log_file=log_file,
                    )
                )
                return True
            _emit_fallback_record("resolved_llm", "llm")
        else:
            _emit_fallback_record("exhausted", "cascade_exhausted")
            # Failed model invocation: the actual model call count (1)
            # must not be hidden from the caller's deferred counters.
            _attempt_record(
                outcome="model-failed",
                origin="llm",
                stage_name="llm",
                model_invocations=1,
                model_request={
                    "backend": backend,
                    "claude_cmd_template": claude_cmd_template,
                },
            )
        return rc == 0

    _emit_fallback_record("exhausted", "cascade_exhausted")
    _attempt_record(
        outcome="exhausted",
        origin="deterministic",
        stage_name="cascade_exhausted",
        model_invocations=0,
        model_request=None,
    )
    return False


def process_refactor_queue(
    worktree_path: Path,
    repo_path: Path,
    dry_run: bool = False,
    max_items: Optional[int] = None,
    auto_approve: bool = False,
) -> Dict[str, Any]:
    """Leave review items untouched until structural execution is qualified."""
    # ponytail: no structural execution until configured scope, validation,
    # closure and durable-artifact acceptance are shared with the solo lane.
    return {
        "processed": [],
        "approved": [],
        "pending": [],
        "failed": [],
        "unqualified": (
            "structural repair unqualified: missing configured scope, validation, "
            "closure and durable-artifact acceptance"
        ),
    }
