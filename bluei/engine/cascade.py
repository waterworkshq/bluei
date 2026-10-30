"""Multi-engine deterministic cascade for automated fix attempts.

Tries fix engines in tier order (most deterministic first). First stage that
both succeeds and passes validation wins. Falls through to LLM only when all
cheaper stages fail. Saves roughly 65% of LLM calls on typical repos.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

_logger = logging.getLogger(__name__)

from bluei.engine.governance import format_asset_ref, is_governance_active
from bluei.engine.jsonl import append_jsonl
from bluei.engine.models import Finding, now_iso
from bluei.engine.state import _append_text
from bluei.engine.utils import run_capture
from bluei.engine.worktree_checkpoint import (
    CheckpointError,
    CheckpointRestoreError,
    WorktreeCheckpoint,
    capture_checkpoint,
    changed_paths,
    discard_checkpoint,
    restore_checkpoint,
)

# Verbatim ruff selector as emitted by the discovery plugins ("ruff-F401" → "F401").
# Casing is respected (no transformation): anything the core/plugins do not
# produce in this exact shape is malformed and must be declined, never run
# unscoped.
_RUFF_RULE_RE = re.compile(r"^ruff-([A-Za-z]+[0-9]{3,})$")


def ruff_rule_code(rule: str) -> Optional[str]:
    """Return the ruff selector for a plugin-emitted rule id, or None if malformed."""
    m = _RUFF_RULE_RE.match(rule or "")
    return m.group(1).upper() if m else None


def default_checkpoint_dir() -> Path:
    """Backup root for cascade-created checkpoints; always outside the worktree."""
    env = os.environ.get("BLUEI_CHECKPOINT_DIR")
    if env:
        return Path(env)
    workspace = os.environ.get("QA_AGENT_WORKSPACE")
    if workspace:
        return Path(workspace) / "state" / "checkpoints"
    return Path(tempfile.gettempdir()) / "bluei-checkpoints"


@dataclass
class CascadeResult:
    """Outcome of a single cascade stage attempt."""

    success: bool
    stage_name: str
    changes_made: List[str] = field(default_factory=list)
    validation_passed: bool = False
    pattern_id: Optional[str] = None
    latency_ms: int = 0
    error: Optional[str] = None
    # Telemetry for the winning run; set on cascade-level success so the
    # lifecycle tier can emit the ledger record AFTER its own validation.
    telemetry: Optional["CascadeTelemetry"] = None
    # Declared stage scope (execute()'s authorized set), carried so the
    # lifecycle tier can re-verify it AFTER its own validation runs.
    authorized_scope: Optional[Set[str]] = None


@dataclass
class CascadeTelemetry:
    """Structured telemetry for one cascade execution across all stages."""

    finding_id: str = ""
    rule: str = ""
    stages_tried: List[str] = field(default_factory=list)
    stages_skipped: List[str] = field(default_factory=list)
    stages_failed: List[Dict[str, str]] = field(default_factory=list)
    final_stage: str = ""
    total_latency_ms: int = 0
    success: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize telemetry fields to a plain dict for JSON logging."""
        return {
            "finding_id": self.finding_id,
            "rule": self.rule,
            "stages_tried": self.stages_tried,
            "stages_skipped": self.stages_skipped,
            "stages_failed": self.stages_failed,
            "final_stage": self.final_stage,
            "total_latency_ms": self.total_latency_ms,
            "success": self.success,
        }


@dataclass
class CascadeContext:
    """Shared configuration and services available to every cascade stage."""

    worktree: Path = field(default_factory=Path)
    repo_path: Path = field(default_factory=Path)
    log_file: Path = field(default_factory=Path)
    language: str = ""
    baseline_results: Dict[str, Any] = field(default_factory=dict)
    pattern_store: Any = None
    recipe_engine: Any = None
    max_stages: int = 10
    allow_llm: bool = True
    shared_library: Any = None
    replay_threshold_overrides: Dict[str, float] = field(default_factory=dict)
    ledger_path: Optional[Path] = None
    cycle: Optional[str] = None
    ledger_records: Optional[List[Dict[str, Any]]] = None
    run_id: str = ""
    cost_tracker: Any = None
    # Governance State projection (ADR-0008) — consulted by cascade stages to
    # skip PAUSED/RETIRED assets. Empty dict ⇒ all assets ACTIVE (no-op).
    governance_state: Dict[str, str] = field(default_factory=dict)
    # Pattern ids the caller already attempted for THIS finding (e.g. the
    # standalone mainline replay) — the pattern stage excludes them so one
    # finding yields at most one attempt and one recorded outcome per pattern.
    attempted_pattern_ids: Optional[set] = None
    # Caller-owned checkpoint spanning the cascade AND the lifecycle tier
    # validation that follows; execute() restores it on every failed stage but
    # never discards it — the owner decides after its tier gate.
    checkpoint: Optional[WorktreeCheckpoint] = None
    # Backup root when execute() must create its own checkpoint (direct callers).
    checkpoint_dir: Optional[Path] = None


class CascadeStage(ABC):
    """Abstract base for a single fix strategy within the cascade pipeline."""

    name: str = ""
    tier: int = 1
    estimated_cost: float = 0.0
    estimated_latency_ms: int = 1000

    @abstractmethod
    def can_handle(self, finding: Finding, context: CascadeContext) -> bool:
        """Return True if this stage is applicable to the given finding."""
        ...

    @abstractmethod
    def attempt(
        self, finding: Finding, worktree: Path, context: CascadeContext
    ) -> CascadeResult:
        """Try to fix the finding; return a CascadeResult."""
        ...

    def rollback(self, finding: Finding, worktree: Path) -> None:
        """Revert any changes made by attempt(). No-op by default."""
        pass


class DeterministicCascade:
    """Orchestrator that runs registered stages in tier order until one wins."""

    def __init__(self, stages: Optional[List[CascadeStage]] = None):
        """
        Args:
            stages: Initial stage list; sorted by tier automatically.
        """
        self._stages: List[CascadeStage] = sorted(stages or [], key=lambda s: s.tier)

    @property
    def stages(self) -> List[CascadeStage]:
        return list(self._stages)

    def register(self, stage: CascadeStage) -> None:
        """Add a stage and re-sort by tier."""
        self._stages.append(stage)
        self._stages.sort(key=lambda s: s.tier)

    def execute(
        self, finding: Finding, worktree: Path, context: CascadeContext
    ) -> CascadeResult:
        """Run each applicable stage in tier order; first validated success wins.

        Transactional: a worktree checkpoint is captured before the first stage
        attempt. Every failed stage, stage exception/timeout, validation failure
        and out-of-scope edit restores the exact pre-attempt worktree state
        (bytes, modes, symlinks, staged/untracked/ignored files, git index).
        A failed restore raises CheckpointRestoreError (backup retained) — the
        caller must halt; no further stage and no LLM fallback runs.

        The resolved_deterministic ledger record is NOT emitted here: success
        is provisional until the lifecycle tier validation accepts it. The
        winning telemetry rides on CascadeResult.telemetry; the caller emits
        after acceptance (or records an honest exhausted outcome on rejection).

        Args:
            finding: The lint/analysis finding to fix.
            worktree: Git worktree where edits are applied.
            context: Shared cascade configuration and services.

        Returns:
            CascadeResult from the first successful stage, or cascade_exhausted
            after all failed stages have been restored. The caller may then use
            its normal LLM fallback. Checkpoint capture and restore errors raise;
            they must never be treated as clean exhaustion or permit fallback.
        """
        telemetry = CascadeTelemetry(
            finding_id=finding.finding_id or "",
            rule=finding.rule,
        )
        attempts = 0
        t0 = time.monotonic()
        checkpoint: Optional[WorktreeCheckpoint] = None
        owned_checkpoint = False
        accepted = False
        pending = None

        try:
            for stage in self._stages:
                if attempts >= context.max_stages:
                    _append_text(
                        context.log_file,
                        f"cascade: max_stages={context.max_stages} reached, stopping",
                    )
                    break

                try:
                    handled = stage.can_handle(finding, context)
                except Exception as exc:
                    _append_text(
                        context.log_file,
                        f"cascade: stage={stage.name} can_handle error={exc}",
                    )
                    telemetry.stages_skipped.append(stage.name)
                    continue
                if not handled:
                    telemetry.stages_skipped.append(stage.name)
                    continue

                if checkpoint is None:
                    try:
                        checkpoint, owned_checkpoint = self._ensure_checkpoint(
                            worktree, context
                        )
                    except CheckpointError as exc:
                        # Best-effort diagnostic only: a failing log write must
                        # never mask the CheckpointError — a masked error looks
                        # like an ordinary failure and lets dispatch fall back
                        # to the model.
                        try:
                            _append_text(
                                context.log_file,
                                f"cascade: checkpoint failed: {exc}",
                            )
                        except BaseException:
                            pass
                        raise

                _append_text(
                    context.log_file,
                    f"cascade: trying stage={stage.name} finding_id={finding.finding_id} rule={finding.rule}",
                )

                telemetry.stages_tried.append(stage.name)
                try:
                    allowed = self._stage_scope(stage, finding, worktree, context)
                    result = stage.attempt(finding, worktree, context)
                except Exception as exc:  # incl. subprocess.TimeoutExpired
                    result = CascadeResult(
                        success=False,
                        stage_name=stage.name,
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        error=f"{type(exc).__name__}: {exc}",
                    )

                if result.success and result.validation_passed:
                    try:
                        out_of_scope = self._out_of_scope_edits(worktree, allowed, checkpoint)
                    except Exception:
                        self._restore_or_stop(context, checkpoint, stage.name)
                        raise
                    if out_of_scope is not None:
                        _append_text(
                            context.log_file,
                            f"cascade: stage={stage.name} out_of_scope_edits={sorted(out_of_scope)} — restoring",
                        )
                        telemetry.stages_failed.append(
                            {"stage": stage.name, "reason": "out_of_scope_edits"}
                        )
                        self._restore_or_stop(context, checkpoint, stage.name)
                        attempts += 1
                        continue
                    telemetry.final_stage = stage.name
                    telemetry.success = True
                    telemetry.total_latency_ms = int((time.monotonic() - t0) * 1000)
                    result.telemetry = telemetry
                    result.authorized_scope = allowed
                    _append_text(
                        context.log_file, f"cascade: stage={stage.name} succeeded"
                    )
                    self._write_telemetry(context, telemetry)
                    # Ledger record deferred: the lifecycle tier must accept
                    # before success is persisted (result.telemetry carries it).
                    accepted = True
                    return result

                if result.success and not result.validation_passed:
                    telemetry.stages_failed.append(
                        {"stage": stage.name, "reason": "validation_failed"}
                    )
                    _append_text(
                        context.log_file,
                        f"cascade: stage={stage.name} validation_failed",
                    )
                else:
                    telemetry.stages_failed.append(
                        {
                            "stage": stage.name,
                            "reason": result.error or "unknown",
                        }
                    )
                    _append_text(
                        context.log_file,
                        f"cascade: stage={stage.name} failed error={result.error}",
                    )
                # Transactional rollback: restore exact pre-attempt state. The
                # old per-stage git-checkout rollback is NOT used here — it
                # would destroy pre-existing unstaged/staged worktree state.
                self._restore_or_stop(context, checkpoint, stage.name)
                attempts += 1

            telemetry.total_latency_ms = int((time.monotonic() - t0) * 1000)
            telemetry.final_stage = "cascade_exhausted"
            _append_text(
                context.log_file,
                f"cascade: all stages exhausted finding_id={finding.finding_id} attempts={attempts}",
            )
            self._write_telemetry(context, telemetry)

            return CascadeResult(
                success=False,
                stage_name="cascade_exhausted",
                changes_made=[],
                validation_passed=False,
                pattern_id=None,
                latency_ms=telemetry.total_latency_ms,
                error=f"cascade exhausted after {len(telemetry.stages_tried)} stages: " + "; ".join(f"{x['stage']}: {x['reason']}" for x in telemetry.stages_failed),
            )
        except BaseException as exc:
            pending = exc
            # Logging, scope checks, and telemetry can fail after a stage edits
            # files. Restore before releasing even a directly owned backup.
            # A failed restore retains its evidence and must not be retried here.
            if checkpoint is not None and not accepted and not checkpoint.retained:
                restore_checkpoint(checkpoint)
            raise
        finally:
            if checkpoint is not None and owned_checkpoint:
                # Self-created checkpoint: nothing outside execute() will tier-
                # validate, so the backup is no longer needed (worktree is
                # either restored or holds the accepted winning fix).
                try:
                    discard_checkpoint(checkpoint)
                except CheckpointError as exc:
                    if isinstance(pending, (KeyboardInterrupt, SystemExit, CheckpointRestoreError)):
                        pending.add_note(str(exc))
                    else:
                        raise

    def _ensure_checkpoint(
        self, worktree: Path, context: CascadeContext
    ) -> tuple:
        """Return (checkpoint, owned). Uses the caller's checkpoint when given."""
        if context.checkpoint is not None:
            return context.checkpoint, False
        exclude = [context.log_file]
        if context.ledger_path is not None:
            exclude.append(context.ledger_path)
        cp = capture_checkpoint(
            worktree,
            context.checkpoint_dir or default_checkpoint_dir(),
            exclude_paths=exclude,
        )
        return cp, True

    def _restore_or_stop(
        self,
        context: CascadeContext,
        checkpoint: Optional[WorktreeCheckpoint],
        stage_name: str,
    ) -> None:
        """Restore the pre-attempt worktree; a failed restore halts everything."""
        if checkpoint is None:
            return
        try:
            restore_checkpoint(checkpoint)
        except CheckpointRestoreError as exc:
            try:
                _append_text(
                    context.log_file,
                    f"cascade: RESTORE FAILED stage={stage_name} error={exc} — halting, backup retained",
                )
            except BaseException:
                pass  # Diagnostic failure must never replace the fail-stop error.
            raise

    def _stage_scope(self, stage, finding, worktree, context) -> Set[str]:
        """Resolve authorized targets before the stage can mutate the tree."""
        root = Path(worktree).resolve()
        def normalize(path):
            path = Path(path)
            if not path.is_absolute():
                path = root / path
            return path.resolve().relative_to(root).as_posix()

        allowed = {normalize(finding.path)}
        if isinstance(stage, CompositePatternCascadeStage):
            from bluei.engine.composite_pattern import CompositePatternApplier, CompositePatternStore
            store = CompositePatternStore(context.pattern_store.store_path.with_name("composite_patterns.jsonl"))
            pattern = store.lookup(finding.rule)
            if pattern is None:
                raise ValueError("composite pattern disappeared")
            applier = CompositePatternApplier()
            for step in pattern.steps:
                target = applier._resolve_target(step, finding.path, worktree)
                if target is None:
                    raise ValueError("composite target not found")
                allowed.add(normalize(target))
        return allowed

    def _out_of_scope_edits(self, worktree, allowed, checkpoint):
        if checkpoint is None:
            raise RuntimeError("missing checkpoint")
        changed = changed_paths(checkpoint)
        index = checkpoint.index_target
        if index is not None:
            if checkpoint.index_backup is None:
                if index.exists():
                    changed.add("<git-index>")
            elif not index.is_file() or index.read_bytes() != checkpoint.index_backup.read_bytes():
                changed.add("<git-index>")
        # Creating the declared target may also create its parent directories.
        for path in tuple(allowed):
            allowed.update(
                p.as_posix() for p in Path(path).parents
                if p.as_posix() != "." and p.as_posix() not in checkpoint.entries
            )
        return changed - allowed or None

    def _write_telemetry(
        self, context: CascadeContext, telemetry: CascadeTelemetry
    ) -> None:
        """Append a JSON telemetry line to the log file; silently ignores errors."""
        try:
            import json

            _append_text(
                context.log_file,
                f"cascade-telemetry: {json.dumps(telemetry.to_dict())}",
            )
        except Exception:
            _logger.debug("Failed to write cascade telemetry")

    def _emit_ledger_record(
        self,
        context: CascadeContext,
        telemetry: CascadeTelemetry,
        *,
        outcome: str,
        final_stage: Optional[str] = None,
        pattern_id: Optional[str] = None,
    ) -> None:
        """Emit one resolution record to both in-memory accumulator and durable JSONL sink."""
        if context.ledger_path is None and context.ledger_records is None:
            return
        record: Dict[str, Any] = {
            "cycle": context.cycle,
            "finding_id": telemetry.finding_id,
            "rule": telemetry.rule,
            "stages_tried": list(telemetry.stages_tried),
            "final_stage": final_stage or telemetry.final_stage,
            "outcome": outcome,
            "via": "cascade",
            "pattern_id": pattern_id,
            "latency_ms": telemetry.total_latency_ms,
            "timestamp": now_iso(),
            "run_id": context.run_id,
        }
        if context.ledger_records is not None:
            context.ledger_records.append(record)
        if context.ledger_path is not None:
            try:
                append_jsonl(context.ledger_path, record)
            except Exception:
                _logger.debug("Failed to write ledger record")


class LinterFixStage(CascadeStage):
    """Stage that invokes an external linter/auto-fixer binary on a single file."""

    def __init__(
        self,
        name: str,
        command: List[str],
        languages: List[str],
        applicable_rules: Optional[Set[str]] = None,
        tier: int = 1,
        unsafe_fixes: bool = False,
        rule_scoped: bool = False,
    ):
        """
        Args:
            name: Human-readable stage identifier.
            command: Shell command template; ``"{file}"`` is replaced with the
                target file path and, when ``rule_scoped``, ``"{rule}"`` with
                the finding's ruff selector.
            languages: Languages this stage applies to (case-insensitive).
            applicable_rules: Optional rule patterns (fnmatch-globbed) this
                stage handles. ``None`` means all rules.
            tier: Priority tier (lower = tried earlier).
            unsafe_fixes: Whether to keep ``--unsafe-fixes`` in the command.
            rule_scoped: Substitute the finding's validated ruff rule into
                ``"{rule}"``. Findings without a well-formed ruff selector are
                declined — there is no unscoped fallback.
        """
        self.name = name
        self.tier = tier
        self.estimated_cost = 0.0
        self.estimated_latency_ms = 5000
        self.command = command
        self.languages = [l.lower() for l in languages]
        self.applicable_rules = applicable_rules
        self.unsafe_fixes = unsafe_fixes
        self.rule_scoped = rule_scoped

    def can_handle(self, finding: Finding, context: CascadeContext) -> bool:
        """Check language match, rule applicability, and that the binary exists on PATH."""
        if context.language.lower() not in self.languages:
            return False
        if self.rule_scoped and (not finding.safe_to_autofix or ruff_rule_code(finding.rule) is None):
            return False
        if (
            self.applicable_rules is not None
            and finding.rule not in self.applicable_rules
        ):
            rule_matches = any(
                fnmatch(finding.rule, pattern) for pattern in self.applicable_rules
            )
            if not rule_matches:
                return False
        return shutil.which(self.command[0]) is not None

    def attempt(
        self, finding: Finding, worktree: Path, context: CascadeContext
    ) -> CascadeResult:
        """Run the linter fix command, then verify the finding is resolved."""
        t0 = time.monotonic()
        file_path = worktree / finding.path
        if not file_path.exists():
            return CascadeResult(
                success=False,
                stage_name=self.name,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error="file not found",
            )

        cmd = [str(file_path) if a == "{file}" else a for a in self.command]
        if self.rule_scoped:
            if not finding.safe_to_autofix:
                return CascadeResult(False, self.name, error="finding is not autofixable")
            rule_code = ruff_rule_code(finding.rule)
            if rule_code is None:
                # Malformed/unsupported rule selection: decline. Running the
                # fixer without --select would clean unrelated diagnostics.
                return CascadeResult(
                    success=False,
                    stage_name=self.name,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    error=f"unsupported rule selection: {finding.rule!r}",
                )
            cmd = [rule_code if a == "{rule}" else a for a in cmd]
        if not self.unsafe_fixes:
            cmd = [a for a in cmd if a != "--unsafe-fixes"]
        try:
            rc, output = run_capture(cmd, cwd=worktree, timeout=60)
        except subprocess.TimeoutExpired:
            return CascadeResult(
                success=False,
                stage_name=self.name,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error="timeout after 60s",
            )

        if rc != 0:
            return CascadeResult(
                success=False,
                stage_name=self.name,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=f"command exited {rc}: {output[:200]}",
            )

        from bluei.engine.verify import verify_fix_closed

        result = verify_fix_closed(worktree, finding, context.log_file)
        resolved = result.is_closed

        latency = int((time.monotonic() - t0) * 1000)
        return CascadeResult(
            success=resolved,
            stage_name=self.name,
            changes_made=[finding.path] if resolved else [],
            validation_passed=resolved,
            pattern_id=None,
            latency_ms=latency,
            error=output[:200] if not resolved else None,
        )

    def rollback(self, finding: Finding, worktree: Path) -> None:
        """Git-checkout the affected file to discard linter changes."""
        file_path = worktree / finding.path
        if file_path.exists():
            run_capture(
                ["git", "checkout", "--", str(file_path)], cwd=worktree, timeout=10
            )


def default_linter_stages() -> List[CascadeStage]:
    """Return the built-in linter fix stages for Python repos."""
    return [
        LinterFixStage(
            name="ruff-fix",
            command=[
                "ruff",
                "check",
                "--fix",
                "--isolated",
                "--no-cache",
                "--no-unsafe-fixes",
                "--select",
                "{rule}",
                "--",
                "{file}",
            ],
            languages=["python"],
            applicable_rules={"ruff-*"},
            unsafe_fixes=False,
            rule_scoped=True,
        ),
        LinterFixStage(
            name="ruff-format",
            command=["ruff", "format", "--no-cache", "--", "{file}"],
            languages=["python"],
            applicable_rules={"ruff-e501"},
        ),
        LinterFixStage(
            name="pyupgrade",
            command=["pyupgrade", "--py311-plus", "{file}"],
            languages=["python"],
            applicable_rules={"ruff-UP*"},
        ),
        LinterFixStage(
            name="autoflake",
            command=[
                "autoflake",
                "--in-place",
                "--remove-all-unused-imports",
                "{file}",
            ],
            languages=["python"],
            applicable_rules={"ruff-F401"},
        ),
        LinterFixStage(
            name="isort",
            command=["isort", "--profile", "black", "{file}"],
            languages=["python"],
            applicable_rules={"ruff-I001"},
        ),
    ]


class RecipeCascadeStage(CascadeStage):
    """Stage that applies a pre-built recipe fix matched by rule + language."""

    name = "recipe-engine"
    tier = 2
    estimated_cost = 0.0
    estimated_latency_ms = 500

    def can_handle(self, finding: Finding, context: CascadeContext) -> bool:
        """Return True if the recipe engine has a matching recipe for this finding."""
        if context.recipe_engine is None:
            return False
        try:
            file_path = context.worktree / finding.path
            file_text = (
                file_path.read_text(encoding="utf-8") if file_path.exists() else ""
            )
            recipe = context.recipe_engine.match(
                finding.rule, context.language, file_text=file_text
            )
            return recipe is not None and is_governance_active(
                format_asset_ref("recipe", recipe.id), context.governance_state
            )
        except Exception:
            return False

    def attempt(
        self, finding: Finding, worktree: Path, context: CascadeContext
    ) -> CascadeResult:
        """Look up and apply a matching recipe to the affected file."""
        t0 = time.monotonic()
        try:
            file_path = worktree / finding.path
            file_text = (
                file_path.read_text(encoding="utf-8") if file_path.exists() else ""
            )
            recipe = context.recipe_engine.match(
                finding.rule, context.language, file_text=file_text
            )
            if recipe is None:
                return CascadeResult(
                    success=False,
                    stage_name=self.name,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    error="no matching recipe",
                )
            result = context.recipe_engine.apply(
                recipe, file_path, worktree, finding=finding
            )
            latency = int((time.monotonic() - t0) * 1000)
            return CascadeResult(
                success=result.success,
                stage_name=f"recipe:{recipe.id}",
                changes_made=result.files_changed,
                validation_passed=result.validation_passed,
                pattern_id=None,
                latency_ms=latency,
                error=result.error if not result.success else None,
            )
        except Exception as exc:
            return CascadeResult(
                success=False,
                stage_name=self.name,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=str(exc),
            )

    def rollback(self, finding: Finding, worktree: Path) -> None:
        """Git-checkout the affected file to discard recipe changes."""
        file_path = worktree / finding.path
        if file_path.exists():
            run_capture(
                ["git", "checkout", "--", str(file_path)], cwd=worktree, timeout=10
            )


class PatternReplayCascadeStage(CascadeStage):
    """Stage that replays a previously-learned fix pattern from the pattern store."""

    name = "pattern-replay"
    tier = 2
    estimated_cost = 0.0
    estimated_latency_ms = 200

    def __init__(self, confidence_threshold: float = 0.85):
        """
        Args:
            confidence_threshold: Minimum pattern confidence to attempt replay.
                Per-rule overrides via ``context.replay_threshold_overrides``.
        """
        self.confidence_threshold = confidence_threshold

    def can_handle(self, finding: Finding, context: CascadeContext) -> bool:
        """Check pattern store for an exact, structural, or fuzzy match above threshold."""
        if context.pattern_store is None:
            return False
        try:
            from bluei.engine.pattern_store import normalize_snippet
            from bluei.engine.structural_hash import compute_structural_hash

            overrides = getattr(context, "replay_threshold_overrides", {})
            threshold = overrides.get(finding.rule, self.confidence_threshold)
            normalized = normalize_snippet(finding.snippet)
            language = getattr(finding, "language", None) or "python"
            target_path = finding.path
            pattern = context.pattern_store.lookup(
                finding.rule, normalized, target_path=target_path
            )
            if pattern is not None and pattern.confidence >= threshold:
                if is_governance_active(
                    format_asset_ref("pattern", pattern.pattern_id),
                    context.governance_state,
                ):
                    return True
            pattern = context.pattern_store.lookup_structural(
                finding.rule, normalized, language, target_path=target_path
            )
            if pattern is not None and pattern.confidence >= threshold:
                if is_governance_active(
                    format_asset_ref("pattern", pattern.pattern_id),
                    context.governance_state,
                ):
                    return True
            pattern = context.pattern_store.lookup_fuzzy(
                finding.rule, normalized, language, target_path=target_path
            )
            return (
                pattern is not None
                and pattern.confidence >= threshold
                and (
                    is_governance_active(
                        format_asset_ref("pattern", pattern.pattern_id),
                        context.governance_state,
                    )
                )
            )
        except Exception:
            return False

    def attempt(
        self, finding: Finding, worktree: Path, context: CascadeContext
    ) -> CascadeResult:
        """Replay the best-matching pattern against the affected file."""
        t0 = time.monotonic()
        try:
            from bluei.engine.pattern_replay import try_replay

            replayed, pattern_id = try_replay(
                worktree,
                finding,
                context.pattern_store,
                context.baseline_results,
                context.log_file,
                shared_library=getattr(context, "shared_library", None),
                exclude_pattern_ids=getattr(context, "attempted_pattern_ids", None),
            )
            if replayed and context.cost_tracker and pattern_id:
                saved = context.cost_tracker.estimate_invocation_cost(
                    "claude-sonnet-4", input_tokens=4000, output_tokens=2000
                )
                context.cost_tracker.record_pattern_replay_savings(
                    model="claude-sonnet-4",
                    saved_cost=saved,
                    pattern_id=pattern_id,
                    rule=finding.rule,
                )
            latency = int((time.monotonic() - t0) * 1000)
            return CascadeResult(
                success=replayed,
                stage_name=self.name,
                changes_made=[finding.path] if replayed else [],
                validation_passed=replayed,
                pattern_id=pattern_id,
                latency_ms=latency,
            )
        except Exception as exc:
            return CascadeResult(
                success=False,
                stage_name=self.name,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=str(exc),
            )

    def rollback(self, finding: Finding, worktree: Path) -> None:
        """Git-checkout the affected file to discard replay changes."""
        file_path = worktree / finding.path
        if file_path.exists():
            run_capture(
                ["git", "checkout", "--", str(file_path)], cwd=worktree, timeout=10
            )


class ASTTransformCascadeStage(CascadeStage):
    """Stage that applies AST-based code transforms for Python findings."""

    name = "ast-transform"
    tier = 2
    estimated_cost = 0.0
    estimated_latency_ms = 1000

    def __init__(self):
        self._transformer = None
        self._initialized = False

    def _ensure_init(self):
        """Lazily load the AST transformer; sets ``_transformer`` to None on failure."""
        if self._initialized:
            return
        self._initialized = True
        try:
            from bluei.engine.ast_engine.transformer import ASTTransformer
            from bluei.engine.ast_engine.transforms.python_transforms import (
                get_transforms,
            )

            self._transformer = ASTTransformer(get_transforms())
        except Exception:
            self._transformer = None

    def can_handle(self, finding: Finding, context: CascadeContext) -> bool:
        """Return True for Python findings that have a registered AST transform."""
        if context.language.lower() != "python":
            return False
        self._ensure_init()
        if self._transformer is None:
            return False
        transform = self._transformer.get_transform(finding.rule)
        return transform is not None and is_governance_active(
            format_asset_ref("transform", f"python-{finding.rule}"),
            context.governance_state,
        )

    def attempt(
        self, finding: Finding, worktree: Path, context: CascadeContext
    ) -> CascadeResult:
        """Apply the matching AST transform and verify the fix."""
        t0 = time.monotonic()
        self._ensure_init()
        if self._transformer is None:
            return CascadeResult(
                success=False,
                stage_name=self.name,
                latency_ms=0,
                error="transformer not available",
            )
        try:
            transform = self._transformer.get_transform(finding.rule)
            if transform is None:
                return CascadeResult(
                    success=False,
                    stage_name=self.name,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    error="no transform for rule",
                )
            file_path = worktree / finding.path
            if not file_path.exists():
                return CascadeResult(
                    success=False,
                    stage_name=self.name,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    error="file not found",
                )
            source = file_path.read_text(encoding="utf-8")
            from bluei.engine.ast_engine.models import ASTMatch, ASTPattern

            match = ASTMatch(
                pattern=ASTPattern(id=finding.rule, language="python", node_type=""),
                node=None,
                line=finding.line,
                col=0,
                source_text=finding.snippet,
            )
            result = self._transformer.apply(source, match, transform)
            if result.success:
                file_path.write_text(result.source, encoding="utf-8")
                from bluei.engine.verify import verify_fix_closed

                result = verify_fix_closed(worktree, finding, context.log_file)
                resolved = result.is_closed

                latency = int((time.monotonic() - t0) * 1000)
                return CascadeResult(
                    success=resolved,
                    stage_name=self.name,
                    changes_made=[finding.path] if resolved else [],
                    validation_passed=resolved,
                    pattern_id=transform.id,
                    latency_ms=latency,
                    error=None if resolved else "fix did not resolve finding",
                )
            latency = int((time.monotonic() - t0) * 1000)
            return CascadeResult(
                success=False,
                stage_name=self.name,
                latency_ms=latency,
                error=result.error,
            )
        except Exception as exc:
            return CascadeResult(
                success=False,
                stage_name=self.name,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=str(exc),
            )

    def rollback(self, finding: Finding, worktree: Path) -> None:
        """Git-checkout the affected file to discard AST transform changes."""
        file_path = worktree / finding.path
        if file_path.exists():
            run_capture(
                ["git", "checkout", "--", str(file_path)], cwd=worktree, timeout=10
            )


class CompositePatternCascadeStage(CascadeStage):
    """Stage that applies a multi-file composite fix pattern."""

    name = "composite-pattern"
    tier = 2
    estimated_cost = 0.0
    estimated_latency_ms = 300

    def __init__(self, confidence_threshold: float = 0.7):
        """
        Args:
            confidence_threshold: Minimum pattern confidence to attempt the fix.
        """
        self.confidence_threshold = confidence_threshold

    def can_handle(self, finding: Finding, context: CascadeContext) -> bool:
        """Check composite pattern store for a high-confidence match on this rule."""
        if context.pattern_store is None:
            return False
        try:
            from bluei.engine.composite_pattern import CompositePatternStore

            store_path = (
                context.pattern_store.store_path
                if hasattr(context.pattern_store, "store_path")
                else None
            )
            if store_path is None:
                return False
            comp_store = CompositePatternStore(
                store_path.with_name("composite_patterns.jsonl")
            )
            pattern = comp_store.lookup(finding.rule)
            return (
                pattern is not None and pattern.confidence >= self.confidence_threshold
            )
        except Exception:
            return False

    def attempt(
        self, finding: Finding, worktree: Path, context: CascadeContext
    ) -> CascadeResult:
        """Apply the matched composite pattern and verify the fix."""
        t0 = time.monotonic()
        try:
            from bluei.engine.composite_pattern import (
                CompositePatternApplier,
                CompositePatternStore,
            )

            store_path = (
                context.pattern_store.store_path
                if hasattr(context.pattern_store, "store_path")
                else None
            )
            if store_path is None:
                return CascadeResult(
                    success=False,
                    stage_name=self.name,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    error="no pattern store path",
                )
            comp_store = CompositePatternStore(
                store_path.with_name("composite_patterns.jsonl")
            )
            pattern = comp_store.lookup(finding.rule)
            if pattern is None:
                return CascadeResult(
                    success=False,
                    stage_name=self.name,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    error="no composite pattern found",
                )
            applier = CompositePatternApplier()
            success, changes, error = applier.apply(
                pattern,
                finding.path,
                worktree,
                context.log_file,
            )
            latency = int((time.monotonic() - t0) * 1000)
            if success:
                from bluei.engine.validation import verify_fix_closed

                resolved = verify_fix_closed(worktree, finding, context.log_file)
                comp_store.record_result(pattern.pattern_id, resolved)
                if resolved and context.cost_tracker:
                    saved = context.cost_tracker.estimate_invocation_cost(
                        "claude-sonnet-4", input_tokens=4000, output_tokens=2000
                    )
                    context.cost_tracker.record_pattern_replay_savings(
                        model="claude-sonnet-4",
                        saved_cost=saved,
                        pattern_id=pattern.pattern_id,
                        rule=finding.rule,
                    )
                return CascadeResult(
                    success=resolved,
                    stage_name=self.name,
                    changes_made=changes if resolved else [],
                    validation_passed=resolved,
                    pattern_id=pattern.pattern_id,
                    latency_ms=latency,
                    error=None if resolved else "fix did not resolve finding",
                )
            comp_store.record_result(pattern.pattern_id, False)
            return CascadeResult(
                success=False,
                stage_name=self.name,
                latency_ms=latency,
                error=error or "composite apply failed",
            )
        except Exception as exc:
            return CascadeResult(
                success=False,
                stage_name=self.name,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=str(exc),
            )

    def rollback(self, finding: Finding, worktree: Path) -> None:
        """Git-checkout the affected file to discard composite pattern changes."""
        file_path = worktree / finding.path
        if file_path.exists():
            run_capture(
                ["git", "checkout", "--", str(file_path)], cwd=worktree, timeout=10
            )


def default_cascade_stages() -> List[CascadeStage]:
    """Build the standard stage list: linters → recipes → pattern replay → composite → AST."""
    stages: List[CascadeStage] = []
    stages.extend(default_linter_stages())
    stages.append(RecipeCascadeStage())
    stages.append(PatternReplayCascadeStage())
    stages.append(CompositePatternCascadeStage())
    stages.append(ASTTransformCascadeStage())
    return stages
