"""Fix lifecycle engine — the core value proposition of the QA agent.

Implements the detect → scaffold → apply → validate → commit state machine.
Three fix strategies are tried in cascade order: recipe → cascade → LLM.
Safety gates block destructive operations; failed validations auto-rollback.
"""

import logging
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
        baseline_checks=req.baseline_checks,
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

        # --- Directive seeding: record fix attempt outcome ---
        if req.findings_file is not None:
            success = res.returncode == 0
            increment_fix_attempt(
                req.finding.finding_id,
                req.findings_file,
                error=None if success else (output or "fix failed")[:500],
            )
            if success:
                update_finding_record(
                    req.finding.finding_id,
                    req.findings_file,
                    {"fix_success": True},
                )
        if req.lessons_file is not None:
            location = f"{req.finding.path}:{req.finding.line}"
            if res.returncode == 0:
                append_lesson(
                    req.lessons_file,
                    "fix",
                    finding_id=req.finding.finding_id,
                    what_changed=f"fix succeeded rule={req.finding.rule} path={location}",
                )
            else:
                error_summary = (output or "fix failed")[:200].split("\n")[-1]
                append_lesson(
                    req.lessons_file,
                    "fix",
                    finding_id=req.finding.finding_id,
                    what_broke=f"fix failed rc={res.returncode} rule={req.finding.rule} path={location}: {error_summary}",
                )
        if res.returncode == 0:
            _extract_fix_pattern(
                req.worktree_path,
                req.finding,
                "claude",
                req.pattern_store_path,
                req.log_file,
                detected_frameworks=req.detected_frameworks,
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
    result = _get_tiered_validator().validate(
        tier, worktree_path, finding, log_file=log_file
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
            _extract_fix_pattern(
                worktree_path,
                finding,
                "recipe",
                pattern_store_path,
                log_file,
                detected_frameworks=detected_frameworks,
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
    _extract_fix_pattern(
        worktree_path,
        finding,
        "autofix",
        pattern_store_path,
        log_file,
        detected_frameworks=detected_frameworks,
    )
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
            detected_frameworks=detected_frameworks,
        )
        tier = resolve_tier(finding)
        if not _tier_validate(tier, worktree_path, finding, log_file):
            # NOTE: the cascade already emitted a resolved_deterministic ledger
            # record (success path). Tier-validation is a stricter gate, so a
            # rejection here means the finding was NOT actually resolved, yet the
            # ledger record stands. This over-report is accepted per ADR-0001
            # (option C — apply_cascade_fix keeps its bool return); the record
            # measures "a deterministic stage produced a cascade-level-valid fix."
            return False
        return True

    # Cascade fallback: all deterministic stages failed → hand off to LLM
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
            ClaudeFixRequest(
                worktree_path=worktree_path,
                finding=finding,
                baseline_checks=dict(BASELINE_VALIDATION_CHECKS),
                target_checks={},
                claude_cmd_template=DEFAULT_CLAUDE_CMD_TEMPLATE,
                max_files_changed=5,
                max_loc_diff=200,
                log_file=log_file,
                pattern_store_path=pattern_store_path,
                detected_frameworks=detected_frameworks,
            ),
        )
        if rc == 0:
            _emit_fallback_record("resolved_llm", "llm")
        else:
            _emit_fallback_record("exhausted", "cascade_exhausted")
        return rc == 0

    _emit_fallback_record("exhausted", "cascade_exhausted")
    return False


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
                from bluei.engine.validation import build_target_checks

                target_checks = build_target_checks(finding)

                # Apply the fix via Claude fix engine
                rc, output, _ = apply_claude_fix(
                    ClaudeFixRequest(
                        worktree_path=wt,
                        finding=finding,
                        baseline_checks=BASELINE_VALIDATION_CHECKS,
                        target_checks=target_checks,
                        claude_cmd_template=DEFAULT_CLAUDE_CMD_TEMPLATE,
                        max_files_changed=20,
                        max_loc_diff=500,
                        log_file=Path("/dev/null"),
                    ),
                )

                if rc == 0:
                    from bluei.engine.verify import run_validation

                    validation = run_validation(
                        repo_path=repo_path,
                        worktree_path=wt,
                        checks=target_checks,
                        allow_unchanged_baseline_failures=True,
                        log_file=Path("/dev/null"),
                    )
                    if validation.passed:
                        queue.complete(item.work_id)
                        result["processed"].append(item.work_id)
                    else:
                        error_msg = validation.message or "validation failed"
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
