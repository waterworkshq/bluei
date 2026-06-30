"""Campaign execution engine with phase-by-phase finding resolution.

Runs campaigns end-to-end with support for dry-run mode, file rollback on
failure, validation gates between phases, and pause/resume semantics via
a filesystem lock.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List

_logger = logging.getLogger(__name__)

from .types import CampaignStatus, PhaseStatus, LearningObjective, ObjectiveTarget
from .state import CampaignStateManager
from bluei.engine.models import Finding, now_iso
from bluei.engine.lifecycle import apply_autofix
from bluei.engine.pattern_store import FixPatternStore
from bluei.engine.dry_replay import run_dry_replay
from bluei.engine.rule_family import derive_rule_family


def autofix_fix_runner(
    *,
    campaign: Any,
    phase: Any,
    finding_id: str,
    finding: Dict[str, Any],
    worktree_path: Path,
    pattern_store_path: Path | None = None,
) -> Dict[str, Any]:
    """Apply a single finding fix via pattern replay (high-confidence) or deterministic autofix.

    Tries FixPatternStore lookup first when a pattern_store_path is provided;
    falls back to the sandbox autofix pipeline.

    Args:
        campaign: Parent Campaign object (passed through to fix_runner).
        phase: Parent CampaignPhase object (passed through to fix_runner).
        finding_id: Unique identifier of the finding to fix.
        finding: Finding snapshot dict from campaign.finding_snapshots.
        worktree_path: Path to the git worktree where fixes are applied.
        pattern_store_path: Optional path to a FixPatternStore file.

    Returns:
        Dict with "status" ("success"/"failed") and "method" or "error" keys.
        Additive keys (Campaign Lab, alpha.5): "deterministic" (always True —
        pattern_replay + apply_autofix never invoke the LLM), "matched_asset"
        (pattern_id when method=="pattern_replay", else None), "cascade_stage"
        (None in alpha.5 — apply_autofix returns bool). Existing readers that
        only consult "status"/"method"/"error" are unaffected.
    """
    try:
        snapshot = {**finding, "finding_id": finding.get("finding_id") or finding_id}
        sandbox_finding = Finding.from_dict(snapshot)
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "status": "failed",
            "error": f"missing finding snapshot field: {exc}",
            "deterministic": True,
            "matched_asset": None,
            "cascade_stage": None,
        }

    if pattern_store_path is not None and pattern_store_path.exists():
        try:
            store = FixPatternStore(pattern_store_path)
            pattern = store.lookup(
                finding.get("rule", ""),
                finding.get("snippet", ""),
            )
            if pattern is not None and pattern.confidence >= 0.9:
                target_file = Path(worktree_path) / finding.get("path", "")
                if target_file.exists() and pattern.before_snippet:
                    content = target_file.read_text()
                    if pattern.before_snippet in content:
                        new_content = content.replace(
                            pattern.before_snippet, pattern.after_snippet, 1
                        )
                        target_file.write_text(new_content)
                        return {
                            "status": "success",
                            "method": "pattern_replay",
                            "deterministic": True,
                            "matched_asset": pattern.pattern_id,
                            "cascade_stage": None,
                        }
        except Exception:
            _logger.debug("Failed to replay campaign fix pattern")

    log_file = Path(worktree_path) / ".bluei" / "campaign-autofix.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        applied = apply_autofix(Path(worktree_path), sandbox_finding, log_file)
    except Exception as exc:
        return {
            "status": "failed",
            "error": f"autofix exception: {type(exc).__name__}",
            "deterministic": True,
            "matched_asset": None,
            "cascade_stage": None,
        }
    if not applied:
        return {
            "status": "failed",
            "error": "autofix returned false",
            "method": "autofix",
            "deterministic": True,
            "matched_asset": None,
            "cascade_stage": None,
        }
    return {
        "status": "success",
        "method": "autofix",
        "deterministic": True,
        "matched_asset": None,
        "cascade_stage": None,
    }


class CampaignExecutor:
    """Drive a planned campaign through its phases with validation, rollback, and pause support."""

    def __init__(
        self,
        campaign_store: CampaignStateManager,
        fix_runner: Callable[..., Dict[str, Any]] | None = None,
        pattern_store_path: Path | None = None,
        *,
        emergent_evidence_consumer: Callable[..., Dict[str, Any]] | None = None,
        dry_replay_path: Path | None = None,
        baseline_checks: Dict[str, Any] | None = None,
        run_id: str = "",
    ):
        """
        Args:
            campaign_store: State manager for loading/saving campaign data.
            fix_runner: Callable that applies a single fix; required when dry_run=False.
            pattern_store_path: Optional path to a FixPatternStore file for pattern replay.
            emergent_evidence_consumer: Optional app-layer callable (defined in
                ``bluei/app/campaign_evidence.py``, wired at
                ``bin/cmd_campaign.py``) that advances the target emergent
                rule + proposes new rules from successful fix patterns.
                Required for emergent-rule learning objectives. Injected (not
                imported) so the campaigns layer never depends on
                ``bluei.app.*``. When ``None`` (default), emergent-rule
                objectives are a no-op.
            dry_replay_path: Optional path to a dry_replay.jsonl file. Required
                for pattern-family learning objectives (evidence store).
            baseline_checks: Validation command map consumed by Dry Replay.
            run_id: Identifier stamped on dry-replay records / proposed rules.

        The learning-objective kwargs are additive and default to no-ops;
        a campaign without a ``learning_objective`` behaves exactly as before.
        """
        self.campaign_store = campaign_store
        self.fix_runner = fix_runner
        self.pattern_store_path = pattern_store_path
        self.emergent_evidence_consumer = emergent_evidence_consumer
        self.dry_replay_path = dry_replay_path
        self.baseline_checks: Dict[str, Any] = baseline_checks or {}
        self.run_id = run_id

    def run(
        self,
        campaign_id: str,
        *,
        dry_run: bool = False,
        worktree_path: Path | None = None,
        require_clean_worktree: bool = False,
    ) -> Dict[str, Any]:
        """Execute all pending phases of a campaign sequentially.

        In dry_run mode, findings are skipped with no file mutations. In live
        mode, each finding is applied via fix_runner with pre/post validation
        gates and automatic rollback on failure.

        Args:
            campaign_id: Campaign to execute.
            dry_run: If True, skip all fixes and count findings as skipped.
            worktree_path: Root of the git worktree for file operations.
            require_clean_worktree: If True, verify a clean git state before running.

        Returns:
            Dict with campaign_id, status, phases_walked, and finding counts.

        Raises:
            ValueError: If the campaign is aborted or worktree is dirty.
            RuntimeError: If another run is already in progress (lock conflict).
        """
        if not dry_run and self.fix_runner is None:
            raise ValueError(
                "campaign execution requires a fix_runner when dry_run=False"
            )

        lock_path = self._lock_path(campaign_id)
        lock_fd = self._acquire_lock(lock_path)
        campaign = self.campaign_store.load(campaign_id)
        try:
            if campaign.status == CampaignStatus.aborted:
                raise ValueError("campaign is aborted")
            cwd = Path(worktree_path or ".")
            if not dry_run and require_clean_worktree:
                try:
                    self._require_clean_git_worktree(cwd)
                except ValueError as exc:
                    self.campaign_store.append_event(
                        campaign_id,
                        "campaign.preflight.failed",
                        {
                            "dry_run": False,
                            "reason": "clean_worktree_required",
                            "error": str(exc),
                        },
                    )
                    raise

            campaign.status = CampaignStatus.active
            campaign.updated_at = now_iso()
            self.campaign_store.save(campaign)
            self.campaign_store.append_event(
                campaign_id,
                "campaign.started",
                {"dry_run": dry_run, "phases": len(campaign.phases)},
            )

            phases_walked = 0
            findings_skipped = 0
            objective = campaign.learning_objective
            method_breakdown: Dict[str, int] = {}
            assets_proven: List[str] = []
            proposed_rule_ids: List[str] = []
            dry_replay_candidates: List[str] = []
            for phase in campaign.phases:
                if phase.status == PhaseStatus.completed:
                    continue
                phase_snapshot = (
                    self._snapshot_phase_files(campaign, phase, cwd)
                    if not dry_run and require_clean_worktree
                    else {}
                )
                phase_fixed_this_run = 0
                campaign.current_phase_index = phase.index
                phase.status = PhaseStatus.active
                phase.started_at = now_iso()
                campaign.updated_at = phase.started_at
                self.campaign_store.save(campaign)
                self.campaign_store.append_event(
                    campaign_id,
                    "phase.started",
                    {
                        "dry_run": dry_run,
                        "phase_id": phase.phase_id,
                        "finding_count": len(phase.finding_ids),
                    },
                )

                pre_result = self._run_validation_checks(
                    campaign_id,
                    phase.phase_id,
                    "pre",
                    phase.pre_phase_checks,
                    cwd,
                    dry_run,
                )
                if pre_result is not None:
                    return self._pause_for_validation_failure(
                        campaign,
                        phase,
                        pre_result,
                        phases_walked,
                        findings_skipped,
                        dry_run,
                    )

                for finding_id in phase.finding_ids:
                    if dry_run:
                        campaign.findings_skipped += 1
                        phase.findings_skipped += 1
                        findings_skipped += 1
                        self.campaign_store.save(campaign)
                        self.campaign_store.append_event(
                            campaign_id,
                            "finding.skipped",
                            {
                                "dry_run": True,
                                "phase_id": phase.phase_id,
                                "finding_id": finding_id,
                                "reason": "dry_run",
                            },
                        )
                        continue

                    finding_snapshot = campaign.finding_snapshots.get(finding_id, {})
                    # For pattern-family objectives, snapshot the target file
                    # before the fix so Dry Replay can replay candidates against
                    # the original content (per DESIGN §2.5).
                    original_content: str | None = None
                    replay_path: Path | None = None
                    replay_target = (
                        not dry_run
                        and objective is not None
                        and objective.target_type == ObjectiveTarget.pattern_family
                        and self.dry_replay_path is not None
                        and derive_rule_family(finding_snapshot.get("rule", ""))
                        == objective.target_ref
                    )
                    if replay_target:
                        rel = finding_snapshot.get("path", "")
                        if rel:
                            candidate_path = cwd / rel
                            if candidate_path.exists():
                                try:
                                    snapshot_text = candidate_path.read_text(
                                        encoding="utf-8"
                                    )
                                except Exception:
                                    snapshot_text = None
                                if snapshot_text is not None:
                                    replay_path = candidate_path
                                    original_content = snapshot_text

                    fix_result = self._apply_finding(
                        campaign=campaign,
                        phase=phase,
                        finding_id=finding_id,
                        finding=finding_snapshot,
                        worktree_path=cwd,
                    )
                    if str(fix_result.get("status")) != "success":
                        self._rollback_phase_files(
                            campaign.campaign_id,
                            phase.phase_id,
                            cwd,
                            phase_snapshot,
                            reason="finding_failed",
                        )
                        campaign.findings_fixed -= phase_fixed_this_run
                        phase.findings_fixed -= phase_fixed_this_run
                        return self._pause_for_finding_failure(
                            campaign,
                            phase,
                            finding_id,
                            fix_result,
                            phases_walked,
                            findings_skipped,
                        )
                    campaign.findings_fixed += 1
                    phase.findings_fixed += 1
                    phase_fixed_this_run += 1

                    # Accumulate the fix outcome for the learning report.
                    if objective is not None:
                        method_key = fix_result.get("method") or "fix_runner"
                        method_breakdown[method_key] = (
                            method_breakdown.get(method_key, 0) + 1
                        )
                        matched = fix_result.get("matched_asset")
                        if matched:
                            if matched not in assets_proven:
                                assets_proven.append(matched)

                    # Pattern-family evidence routing: Dry Replay writes
                    # would_have_outcome records to dry_replay.jsonl. This is
                    # the EVIDENCE store only — never the governance trail
                    # (run_sprt_check is deliberately NOT called here; SPRT
                    # evaluates the gathered evidence in the pr-cycle).
                    if (
                        replay_target
                        and original_content is not None
                        and replay_path is not None
                    ):
                        try:
                            winner_content = replay_path.read_text(encoding="utf-8")
                            self._run_pattern_family_replay(
                                finding_snapshot,
                                cwd,
                                original_content,
                                winner_content,
                                dry_replay_candidates,
                            )
                        except Exception:
                            _logger.debug(
                                "campaign dry-replay routing failed for %s",
                                finding_id,
                            )

                    self.campaign_store.save(campaign)
                    self.campaign_store.append_event(
                        campaign_id,
                        "finding.fixed",
                        {
                            "dry_run": False,
                            "phase_id": phase.phase_id,
                            "finding_id": finding_id,
                            "method": fix_result.get("method", "fix_runner"),
                        },
                    )

                # Emergent-rule evidence routing: delegate to the injected
                # app-layer consumer (bluei/app/campaign_evidence.py). The
                # consumer advances the named target rule via shadow validation
                # AND proposes new rules from successful fix patterns. Both
                # touch the emergent store JSON only — zero governance
                # ApprovalRecord writes (AC-P2-4). No consumer wired → no-op.
                if (
                    not dry_run
                    and objective is not None
                    and objective.target_type == ObjectiveTarget.emergent_rule
                    and self.emergent_evidence_consumer is not None
                ):
                    self._advance_emergent_objective(
                        objective,
                        cwd,
                        assets_proven,
                        proposed_rule_ids,
                    )

                post_result = self._run_validation_checks(
                    campaign_id,
                    phase.phase_id,
                    "post",
                    phase.post_phase_checks,
                    cwd,
                    dry_run,
                )
                if post_result is not None:
                    self._rollback_phase_files(
                        campaign.campaign_id,
                        phase.phase_id,
                        cwd,
                        phase_snapshot,
                        reason="validation_failed",
                    )
                    campaign.findings_fixed -= phase_fixed_this_run
                    phase.findings_fixed -= phase_fixed_this_run
                    return self._pause_for_validation_failure(
                        campaign,
                        phase,
                        post_result,
                        phases_walked,
                        findings_skipped,
                        dry_run,
                    )

                phase.status = PhaseStatus.completed
                phase.completed_at = now_iso()
                campaign.updated_at = phase.completed_at
                phases_walked += 1
                self.campaign_store.save(campaign)
                self.campaign_store.append_event(
                    campaign_id,
                    "phase.completed",
                    {
                        "dry_run": dry_run,
                        "phase_id": phase.phase_id,
                        "finding_count": len(phase.finding_ids),
                    },
                )

            campaign.status = CampaignStatus.completed
            self._clear_pause_metadata(campaign)
            campaign.updated_at = now_iso()
            self.campaign_store.save(campaign)
            self.campaign_store.append_event(
                campaign_id,
                "campaign.completed",
                {"dry_run": dry_run, "phases": phases_walked},
            )

            return {
                "campaign_id": campaign_id,
                "status": campaign.status,
                "dry_run": dry_run,
                "phases_walked": phases_walked,
                "findings_skipped": findings_skipped,
                "findings_fixed": campaign.findings_fixed,
                "findings_failed": campaign.findings_failed,
                **(
                    {
                        "learning_report": {
                            "objective": objective.to_dict(),
                            "method_breakdown": dict(method_breakdown),
                            "assets_proven": list(assets_proven),
                            "proposed_rules": list(proposed_rule_ids),
                            "candidates": list(dry_replay_candidates),
                        }
                    }
                    if objective is not None
                    else {}
                ),
            }
        finally:
            os.close(lock_fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                _logger.debug("Failed to unlink lock file")

    def _pause_for_validation_failure(
        self,
        campaign,
        phase,
        failure: Dict[str, Any],
        phases_walked: int,
        findings_skipped: int,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Transition campaign and phase to paused state after a validation check fails.

        Args:
            campaign: Campaign object to update in-place.
            phase: Phase object that failed validation.
            failure: Dict with "stage" and "returncode" from the failed check.
            phases_walked: Number of phases completed before this failure.
            findings_skipped: Running count of skipped findings.
            dry_run: Whether the campaign was in dry_run mode.

        Returns:
            Summary dict with campaign status and counts.
        """
        phase.status = PhaseStatus.paused
        campaign.status = CampaignStatus.paused
        self._set_pause_metadata(
            campaign,
            reason="validation_failed",
            phase_id=phase.phase_id,
            finding_id=None,
        )
        campaign.updated_at = now_iso()
        self.campaign_store.save(campaign)
        self.campaign_store.append_event(
            campaign.campaign_id,
            "campaign.paused",
            {
                "dry_run": dry_run,
                "phase_id": phase.phase_id,
                "reason": "validation_failed",
                "stage": failure["stage"],
                "returncode": failure["returncode"],
            },
        )
        return {
            "campaign_id": campaign.campaign_id,
            "status": campaign.status,
            "dry_run": dry_run,
            "phases_walked": phases_walked,
            "findings_skipped": findings_skipped,
            "findings_fixed": campaign.findings_fixed,
            "findings_failed": campaign.findings_failed,
        }

    def _pause_for_finding_failure(
        self,
        campaign,
        phase,
        finding_id: str,
        fix_result: Dict[str, Any],
        phases_walked: int,
        findings_skipped: int,
    ) -> Dict[str, Any]:
        """Transition campaign and phase to paused state after a finding fix fails.

        Args:
            campaign: Campaign object to update in-place.
            phase: Phase object containing the failed finding.
            finding_id: ID of the finding that could not be fixed.
            fix_result: Dict with "error" from the failed fix attempt.
            phases_walked: Number of phases completed before this failure.
            findings_skipped: Running count of skipped findings.

        Returns:
            Summary dict with campaign status and counts.
        """
        campaign.findings_failed += 1
        phase.findings_failed += 1
        phase.status = PhaseStatus.paused
        campaign.status = CampaignStatus.paused
        self._set_pause_metadata(
            campaign,
            reason="finding_failed",
            phase_id=phase.phase_id,
            finding_id=finding_id,
        )
        campaign.updated_at = now_iso()
        self.campaign_store.save(campaign)
        self.campaign_store.append_event(
            campaign.campaign_id,
            "finding.failed",
            {
                "dry_run": False,
                "phase_id": phase.phase_id,
                "finding_id": finding_id,
                "error": fix_result.get("error", "fix_runner_failed"),
            },
        )
        self.campaign_store.append_event(
            campaign.campaign_id,
            "campaign.paused",
            {
                "dry_run": False,
                "phase_id": phase.phase_id,
                "reason": "finding_failed",
                "finding_id": finding_id,
            },
        )
        return {
            "campaign_id": campaign.campaign_id,
            "status": campaign.status,
            "dry_run": False,
            "phases_walked": phases_walked,
            "findings_skipped": findings_skipped,
            "findings_fixed": campaign.findings_fixed,
            "findings_failed": campaign.findings_failed,
        }

    def _apply_finding(
        self,
        *,
        campaign,
        phase,
        finding_id: str,
        finding: Dict[str, Any],
        worktree_path: Path,
    ) -> Dict[str, Any]:
        if self.fix_runner is None:
            raise ValueError(
                "campaign execution requires a fix_runner when dry_run=False"
            )
        return self.fix_runner(
            campaign=campaign,
            phase=phase,
            finding_id=finding_id,
            finding=finding,
            worktree_path=worktree_path,
            pattern_store_path=self.pattern_store_path,
        )

    def _run_pattern_family_replay(
        self,
        finding_snapshot: Dict[str, Any],
        cwd: Path,
        original_content: str,
        winner_content: str,
        candidates_log: List[str],
    ) -> None:
        """Gather Dry Replay evidence for a pattern-family finding.

        Loads candidate patterns for the finding's rule from the configured
        pattern store and runs Dry Replay (writes ``would_have_outcome``
        records to ``dry_replay.jsonl``). The worktree is restored to the
        winning fix on exit. No governance writes (AC-P2-4).
        """
        if self.pattern_store_path is None or self.dry_replay_path is None:
            return
        try:
            snapshot = {
                **finding_snapshot,
                "finding_id": finding_snapshot.get("finding_id") or "campaign-lo",
            }
            finding_obj = Finding.from_dict(snapshot)
        except (KeyError, TypeError, ValueError):
            return
        try:
            store = FixPatternStore(self.pattern_store_path)
        except Exception:
            _logger.debug("campaign pattern store unavailable for dry replay")
            return
        candidates = store.load_active(rule=finding_obj.rule)
        if not candidates:
            return
        log_file = cwd / ".bluei" / "campaign-dry-replay.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        performed = run_dry_replay(
            finding_obj,
            candidates,
            cwd,
            original_content,
            winner_content,
            self.baseline_checks,
            log_file,
            self.run_id or "campaign",
            self.dry_replay_path,
        )
        if performed:
            for pattern in candidates[:performed]:
                if pattern.pattern_id and pattern.pattern_id not in candidates_log:
                    candidates_log.append(pattern.pattern_id)

    def _advance_emergent_objective(
        self,
        objective: LearningObjective,
        cwd: Path,
        assets_proven: List[str],
        proposed_rule_ids: List[str],
    ) -> None:
        """Delegate emergent-rule evidence gathering to the injected consumer.

        The actual advancement (scan_shadow_rules + record_shadow_run) and
        new-rule proposal (propose_rules_from_fix_patterns) live in the
        app-layer consumer injected at construction (``bluei/app/
        campaign_evidence.py``). Campaigns stays free of ``bluei.app.*``
        imports; this method is a thin callback that merges the returned
        summary into the learning report accumulators. When no consumer is
        wired (default), the call site guards this — but defend anyway.
        """
        if self.emergent_evidence_consumer is None:
            return  # no consumer wired → no emergent evidence (tests/default)
        try:
            summary = self.emergent_evidence_consumer(
                target_ref=objective.target_ref,
                worktree=cwd,
                run_id=self.run_id,
                pattern_store_path=self.pattern_store_path,
            )
        except Exception:
            _logger.debug("emergent_evidence_consumer failed")
            return
        for proven in summary.get("assets_proven", []):
            if proven not in assets_proven:
                assets_proven.append(proven)
        for proposed in summary.get("proposed_rules", []):
            if proposed not in proposed_rule_ids:
                proposed_rule_ids.append(proposed)

    def _run_validation_checks(
        self,
        campaign_id: str,
        phase_id: str,
        stage: str,
        checks: list[list[str]],
        cwd: Path,
        dry_run: bool,
    ) -> Dict[str, Any] | None:
        """Run a list of shell-command validation checks; return the first failure or None.

        Args:
            campaign_id: Campaign ID for event logging.
            phase_id: Phase ID for event logging.
            stage: Check stage label ("pre" or "post").
            checks: List of command-arg lists to execute.
            cwd: Working directory for subprocess execution.
            dry_run: Whether the campaign is in dry_run mode.

        Returns:
            Dict with failure details, or None if all checks passed.
        """
        for check in checks:
            result = subprocess.run(
                check,
                cwd=str(cwd),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            payload = {
                "dry_run": dry_run,
                "phase_id": phase_id,
                "stage": stage,
                "command": check,
                "returncode": result.returncode,
            }
            if result.stdout:
                payload["output"] = result.stdout.strip()[:500]
            if result.returncode != 0:
                self.campaign_store.append_event(
                    campaign_id,
                    "phase.validation.failed",
                    payload,
                )
                return payload
            self.campaign_store.append_event(
                campaign_id,
                "phase.validation.passed",
                payload,
            )
        return None

    def _snapshot_phase_files(
        self, campaign, phase, worktree_path: Path
    ) -> Dict[str, bytes | None]:
        """Read and cache file contents for all paths referenced by a phase's findings.

        Args:
            campaign: Campaign object containing finding_snapshots.
            phase: Phase whose finding paths should be snapshotted.
            worktree_path: Root of the worktree to read files from.

        Returns:
            Dict mapping relative paths to their byte content (or None if missing).
        """
        snapshots: Dict[str, bytes | None] = {}
        for finding_id in phase.finding_ids:
            relative_path = campaign.finding_snapshots.get(finding_id, {}).get("path")
            if not relative_path or relative_path in snapshots:
                continue
            target = worktree_path / str(relative_path)
            snapshots[str(relative_path)] = (
                target.read_bytes() if target.is_file() else None
            )
        return snapshots

    def _rollback_phase_files(
        self,
        campaign_id: str,
        phase_id: str,
        worktree_path: Path,
        snapshots: Dict[str, bytes | None],
        *,
        reason: str,
    ) -> None:
        """Restore files to their snapshotted state after a phase failure.

        Args:
            campaign_id: Campaign ID for event logging.
            phase_id: Phase ID for event logging.
            worktree_path: Root of the worktree to restore files into.
            snapshots: Dict from _snapshot_phase_files mapping paths to byte content.
            reason: Human-readable rollback reason for the event log.

        Returns:
            None
        """
        if not snapshots:
            return
        for relative_path, content in snapshots.items():
            target = worktree_path / relative_path
            if content is None:
                if target.is_file():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.campaign_store.append_event(
            campaign_id,
            "phase.rollback.applied",
            {
                "dry_run": False,
                "phase_id": phase_id,
                "reason": reason,
                "paths": sorted(snapshots),
            },
        )

    def _set_pause_metadata(
        self,
        campaign,
        *,
        reason: str,
        phase_id: str,
        finding_id: str | None,
    ) -> None:
        campaign.pause_reason = reason
        campaign.pause_phase_id = phase_id
        campaign.pause_finding_id = finding_id
        campaign.paused_at = now_iso()

    def _clear_pause_metadata(self, campaign) -> None:
        campaign.pause_reason = None
        campaign.pause_phase_id = None
        campaign.pause_finding_id = None
        campaign.paused_at = None

    def _lock_path(self, campaign_id: str) -> Path:
        return self.campaign_store.campaigns_dir / campaign_id / "campaign.lock"

    def _require_clean_git_worktree(self, worktree_path: Path) -> None:
        """Verify that worktree_path is inside a git repo with no uncommitted changes.

        Args:
            worktree_path: Directory to check.

        Raises:
            ValueError: If the path is not a git worktree or has uncommitted changes.
        """
        if not worktree_path.exists() or not worktree_path.is_dir():
            raise ValueError("campaign mutation requires a clean git worktree")
        rev_parse = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "--is-inside-work-tree"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if rev_parse.returncode != 0 or rev_parse.stdout.strip() != "true":
            raise ValueError("campaign mutation requires a clean git worktree")
        status = subprocess.run(
            ["git", "-C", str(worktree_path), "status", "--porcelain"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if status.returncode != 0 or status.stdout.strip():
            raise ValueError("campaign mutation requires a clean git worktree")

    def _acquire_lock(self, lock_path: Path) -> int:
        """Create an exclusive lock file to prevent concurrent campaign runs.

        Args:
            lock_path: Path to the lock file.

        Returns:
            Open file descriptor for the lock.

        Raises:
            RuntimeError: If the lock already exists (campaign already running).
        """
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError("campaign is already running") from exc
