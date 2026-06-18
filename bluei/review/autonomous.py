"""Autonomous — extracted from review.cycle for single-responsibility.

Autonomous review cycle: full pipeline orchestration with guarded live publication.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

from bluei.common.models import (
    LiveRolloutMode,
    ReviewMode,
    now_iso,
    generate_id,
)
from bluei.review.models import PublishStatus, MonitoredSafetyState, CompressionMode
from bluei.review.types import ReviewCycleResult, CandidateValidationError
from bluei.review.normalization import (
    normalize_candidate,
    assign_finding_identity,
    dedupe_findings,
)
from bluei.review.eligibility import is_remediation_eligible
from bluei.review.publisher import (
    _build_pass_filter_result,
    reconcile_publish_state,
    build_publish_entry,
    compute_run_publish_status,
    build_run_publish_entry,
    build_review_summary_comment,
)
from bluei.review.rules import (
    _get_learned_rules_state,
    _save_learned_rules_state,
    _build_learned_rules_payload,
    _process_learned_rules_for_run,
)
from bluei.review.feedback import _flush_injected_feedback


def _format_run_completion_reason(
    lifecycle_phase: str,
    pr_number: Optional[int],
    candidate_source: str,
    guard_enabled: bool,
    live_actions: bool,
    publish_filter_result: Any,
    safety_reason: str,
    current_safety_state: MonitoredSafetyState,
    prior_publish: Dict[str, Any],
    run_id: str,
) -> str:
    """Format the human-readable run_completion_reason for the ReviewRun artifact.

    Pure: no state I/O, no side effects. Extracted from _build_run_artifact
    (rec-18 followup) to make each branch independently unit-testable.
    """
    if lifecycle_phase == "shadow-published":
        return (
            f"shadow: summary would have been posted to PR #{pr_number} "
            f"(live_rollout_mode=shadow; no actual GitHub API call made); "
            f"candidates={candidate_source}"
        )
    elif lifecycle_phase == "guard-disabled":
        return (
            f"guard-disabled: local-only run; "
            f"guarded_live_review={guard_enabled}, live_actions={live_actions}"
        )
    elif lifecycle_phase == "guarded-live-published":
        return (
            f"published: summary comment posted to PR #{pr_number} via guarded path; "
            f"candidates={candidate_source}"
        )
    elif lifecycle_phase == "filter-blocked":
        return (
            f"filter-blocked: publication blocked by limited-publish filters; "
            f"filters_decision={publish_filter_result.decision} "
            f"reason={publish_filter_result.failed_reason!r}; "
            f"candidates={candidate_source}"
        )
    elif lifecycle_phase == "guarded-live-refused":
        runs_entries = prior_publish.get("runs", {})
        run_entry = runs_entries.get(run_id, {})
        refusal_reason = run_entry.get("error", "unknown")
        return (
            f"refused: publication blocked ({refusal_reason}); "
            f"candidates={candidate_source}"
        )
    elif lifecycle_phase == "guarded-live-failed":
        runs_entries = prior_publish.get("runs", {})
        run_entry = runs_entries.get(run_id, {})
        error_reason = run_entry.get("error", "unknown")
        return (
            f"failed: publication error ({error_reason}); candidates={candidate_source}"
        )
    elif lifecycle_phase == "safety-blocked":
        return (
            f"safety-blocked: circuit-breaker blocked live publication; "
            f"safety_reason={safety_reason}; "
            f"circuit_open={current_safety_state.circuit_open}; "
            f"failure_count={current_safety_state.failure_count}; "
            f"candidates={candidate_source}"
        )
    else:
        return (
            f"completed: lifecycle_phase={lifecycle_phase}, "
            f"candidates={candidate_source}"
        )


class AutonomousMixin:
    def _build_run_artifact(
        self,
        result: ReviewCycleResult,
        dry_run: bool,
        prior_publish: Dict[str, Any],
        run_id: str,
        pr_number: Optional[int],
        guard_enabled: bool,
        guard_reason: str,
        live_rollout_mode: LiveRolloutMode,
        rollout_reason: str,
        live_actions: bool,
        candidate_source: str,
        deduped: List[Dict[str, Any]],
        eligible_findings: List[Dict[str, Any]],
        ineligible_findings: List[Dict[str, Any]],
        reconciliation: Any,
        findings_total: int,
        findings_published: int,
        findings_failed: int,
        findings_skipped_count: int,
        rollup_status: PublishStatus,
        summary_text: str,
        comment_url: Optional[str],
        resolved_target_pr: Optional[int],
        publish_filter_result: Any,
        rollout_eligible: bool,
        attention_recommended: bool,
        safety_reason: str,
        current_safety_state: MonitoredSafetyState,
        lifecycle_phase: str,
        operator_action_required: bool,
        operator_action_summary: Optional[str],
        suggested_review_care_patch: Optional[Dict[str, Any]],
    ) -> None:
        """G7 final: build run publish entry, update counters, persist + emit events.

        Constructs the run publish entry (when not dry_run) and the ReviewRun
        artifact, updates result counters, persists run/finding/summary/publish
        state, appends the autonomous-review-completed event, and flushes any
        injected feedback events. Mutates result and prior_publish in place.
        """
        # --- 7. Build run publish entry ---
        # The run entry was already updated in-place by _post_summary_to_github
        # (inside prior_publish["runs"][run_id]) with the gh-result status and error.
        # We read that entry back so the persisted entry reflects the actual
        # publication outcome, not just the local rollup status.
        if not dry_run:
            prior_publish.setdefault("runs", {})
            existing_run_entry = prior_publish["runs"].get(run_id, {})
            gh_status_str = existing_run_entry.get("status", "")
            if gh_status_str in {_s.value for _s in PublishStatus}:
                final_status = PublishStatus(gh_status_str)
            else:
                final_status = rollup_status
            # Preserve the error message set by Phase J's _post_summary_to_github
            gh_error = existing_run_entry.get("error")
            run_publish_entry = build_run_publish_entry(
                status=final_status,
                run_id=run_id,
                findings_total=findings_total,
                findings_published=findings_published,
                findings_failed=findings_failed,
                targeted_pr_number=pr_number,
                lifecycle_phase=lifecycle_phase,
                error=gh_error,
                comment_url=comment_url,
                publish_filter_decision=publish_filter_result.decision,
                publish_filter_reason=publish_filter_result.failed_reason,
                rollout_eligible=rollout_eligible,
                attention_recommended=attention_recommended,
                safety_circuit_open=current_safety_state.circuit_open,
                safety_failure_count=current_safety_state.failure_count,
                safety_cooldown_until=current_safety_state.cooldown_until,
                auto_rollback_active=current_safety_state.auto_rollback_active,
                auto_rollback_reason=current_safety_state.auto_rollback_reason,
                auto_rollback_triggered_at=current_safety_state.auto_rollback_triggered_at,
                operator_action_required=operator_action_required,
                operator_action_summary=operator_action_summary,
                suggested_review_care_patch=suggested_review_care_patch,
            )
            prior_publish["runs"][run_id] = run_publish_entry
            # Preserve shadow flag from _post_summary_to_github's shadow entry
            # (build_run_publish_entry doesn't know about shadow, so we carry it over)
            if existing_run_entry.get("shadow"):
                prior_publish["runs"][run_id]["shadow"] = True
                prior_publish["runs"][run_id]["shadow_summary_text"] = (
                    existing_run_entry.get("shadow_summary_text")
                )
                prior_publish["runs"][run_id]["rollout_mode"] = existing_run_entry.get(
                    "rollout_mode"
                )
        else:
            run_publish_entry = build_run_publish_entry(
                status=rollup_status,
                run_id=run_id,
                findings_total=findings_total,
                findings_published=findings_published,
                findings_failed=findings_failed,
                targeted_pr_number=pr_number,
                lifecycle_phase=lifecycle_phase,
                comment_url=comment_url,
                auto_rollback_active=current_safety_state.auto_rollback_active,
                auto_rollback_reason=current_safety_state.auto_rollback_reason,
                auto_rollback_triggered_at=current_safety_state.auto_rollback_triggered_at,
                operator_action_required=operator_action_required,
                operator_action_summary=operator_action_summary,
                suggested_review_care_patch=suggested_review_care_patch,
            )

        # --- 8. Update result counters ---
        result.findings_detected = len(deduped)
        result.findings_published = findings_published
        result.findings_failed = findings_failed
        result.findings_skipped = findings_skipped_count
        result.findings_absent = len(reconciliation.absent_findings)

        # --- 9. Persist ReviewRun artifact ---
        now = now_iso()

        # Compute run_completion_reason: human-readable summary of why the run ended
        run_completion_reason = _format_run_completion_reason(
            lifecycle_phase=lifecycle_phase,
            pr_number=pr_number,
            candidate_source=candidate_source,
            guard_enabled=guard_enabled,
            live_actions=live_actions,
            publish_filter_result=publish_filter_result,
            safety_reason=safety_reason,
            current_safety_state=current_safety_state,
            prior_publish=prior_publish,
            run_id=run_id,
        )

        review_run_data = {
            "id": run_id,
            "run_id": run_id,
            "repo": self.repo.config.name,
            "pr_number": pr_number,
            "status": "completed",
            "mode": "autonomous-review",
            "compression_mode": CompressionMode.FULL_DIFF.value,
            "token_budget": 0,
            "started_at": now,
            "ended_at": now,
            "findings_total": findings_total,
            "findings_eligible": len(eligible_findings),
            "findings_ineligible": len(ineligible_findings),
            "findings_published": findings_published,
            "findings_failed": findings_failed,
            "findings_skipped": findings_skipped_count,
            "run_publish_status": rollup_status.value,
            "guard_enabled": guard_enabled,
            "guard_reason": guard_reason,
            "live_rollout_mode": live_rollout_mode.value,
            "rollout_reason": rollout_reason,
            "candidate_source": candidate_source,
            "lifecycle_phase": lifecycle_phase,
            "run_completion_reason": run_completion_reason,
            "comment_url": comment_url,
            # Phase G6: publish filter monitoring signals
            "publish_filter_decision": publish_filter_result.decision,
            "publish_filter_reason": publish_filter_result.failed_reason,
            "rollout_eligible": rollout_eligible,
            "attention_recommended": attention_recommended,
            # Phase G7: monitored-rollout safety signals
            "safety_circuit_open": current_safety_state.circuit_open,
            "safety_failure_count": current_safety_state.failure_count,
            "safety_cooldown_until": current_safety_state.cooldown_until,
            "safety_last_failure_reason": current_safety_state.last_failure_reason,
            "auto_rollback_active": current_safety_state.auto_rollback_active,
            "auto_rollback_reason": current_safety_state.auto_rollback_reason,
            "auto_rollback_triggered_at": current_safety_state.auto_rollback_triggered_at,
            "operator_action_required": operator_action_required,
            "operator_action_summary": operator_action_summary,
            "suggested_review_care_patch": suggested_review_care_patch,
            "reconciliation": {
                "new": reconciliation.new_findings,
                "already_published": reconciliation.already_published,
                "absent": reconciliation.absent_findings,
                "superseded": reconciliation.superseded_findings,
                "pending": reconciliation.pending_findings,
            },
            "error": None,
        }

        summary_prompt_path: Optional[Path] = None
        if not dry_run:
            self.state.save_review_run(self.repo.config.name, review_run_data)
            # Persist findings to both JSONL index and individual JSON files
            if deduped:
                self.state.append_review_findings(
                    self.repo.config.name,
                    [{**f, "run_id": run_id} for f in deduped],
                )
            for finding in deduped:
                self.state.save_review_finding(
                    self.repo.config.name,
                    finding["finding_id"],
                    {**finding, "run_id": run_id},
                )
            # Persist summary as local artifact regardless of live publish outcome.
            # The summary is a local-only artifact (not a live GitHub action).
            summary_prompt_path = (
                self.state.get_review_prompts_dir(self.repo.config.name)
                / f"autonomous-run-{run_id}.md"
            )
            summary_prompt_path.parent.mkdir(parents=True, exist_ok=True)
            summary_prompt_path.write_text(summary_text, encoding="utf-8")
            # Persist full publish state AFTER the cycle so reconciliation
            # results and run entries are written through to storage.
            self.state.save_review_publish_state(self.repo.config.name, prior_publish)

        # --- 11. Append review event ---
        if not dry_run:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "autonomous-review-completed",
                    "run_id": run_id,
                    "provider": "local-stub",
                    "details": {
                        "mode": ReviewMode.AUTONOMOUS_REVIEW.value,
                        "findings_total": findings_total,
                        "findings_published": findings_published,
                        "findings_skipped": findings_skipped_count,
                        "findings_absent": result.findings_absent,
                        "rollup_status": rollup_status.value,
                        "lifecycle_phase": lifecycle_phase,
                        "live_rollout_mode": live_rollout_mode.value,
                        "rollout_reason": rollout_reason,
                        "guard_enabled": guard_enabled,
                        "guard_reason": guard_reason,
                        "candidate_source": candidate_source,
                        "resolved_target_pr": resolved_target_pr,
                        "comment_url": comment_url,
                        "summary_file": str(summary_prompt_path)
                        if summary_prompt_path
                        else None,
                        "run_file": f"review_runs/{run_id}.json",
                        # Phase G6: publish filter monitoring signals
                        "publish_filter_decision": publish_filter_result.decision,
                        "publish_filter_reason": publish_filter_result.failed_reason,
                        "rollout_eligible": rollout_eligible,
                        "attention_recommended": attention_recommended,
                        # Phase G7: monitored-rollout safety signals
                        "safety_circuit_open": current_safety_state.circuit_open,
                        "safety_failure_count": current_safety_state.failure_count,
                        "safety_cooldown_until": current_safety_state.cooldown_until,
                        "safety_last_failure_reason": current_safety_state.last_failure_reason,
                        "auto_rollback_active": current_safety_state.auto_rollback_active,
                        "auto_rollback_reason": current_safety_state.auto_rollback_reason,
                        "auto_rollback_triggered_at": current_safety_state.auto_rollback_triggered_at,
                        "operator_action_required": operator_action_required,
                        "operator_action_summary": operator_action_summary,
                        "suggested_review_care_patch": suggested_review_care_patch,
                        "safety_reason": safety_reason,
                    },
                },
            )

            # --- 12. Persist any injected feedback events ---
            flushed = _flush_injected_feedback(
                engine=self,
                repo_name=self.repo.config.name,
                state=self.state,
            )
            if flushed > 0:
                self.state.append_review_event(
                    self.repo.config.name,
                    {
                        "event": "feedback-events-recorded",
                        "count": flushed,
                        "provider": "local-stub",
                        "details": {
                            "injected_via": "inject_feedback_for_autonomous_review"
                        },
                    },
                )

    def _record_safety_outcome(
        self,
        dry_run: bool,
        live_rollout_mode: LiveRolloutMode,
        guard_enabled: bool,
        run_id: str,
        prior_publish: Dict[str, Any],
        comment_url: Optional[str],
        current_safety_state: MonitoredSafetyState,
        safety_blocked_live: bool,
        filter_blocked_live: bool,
    ) -> Dict[str, Any]:
        """G7 second half: record publish success/failure + compute lifecycle.

        Records the publish outcome to the safety state (only on limited+guarded
        non-dry-run), then computes the run's lifecycle_phase and operator-action
        recommendation.

        Returns dict with keys:
            current_safety_state, lifecycle_phase,
            operator_action_required, operator_action_summary,
            suggested_review_care_patch
        """
        # --- Record safety outcome after publish attempt ---
        # Only for limited+guarded path (shadow/local_only skip this)
        if not dry_run:
            if live_rollout_mode == LiveRolloutMode.LIMITED and guard_enabled:
                run_entry_after = prior_publish.get("runs", {}).get(run_id, {})
                gh_status = run_entry_after.get("status", "")
                gh_error = run_entry_after.get("error", "")
                is_success = (
                    gh_status == PublishStatus.PUBLISHED.value
                    and comment_url is not None
                )
                is_failure = gh_status == PublishStatus.FAILED.value or (
                    run_entry_after.get("error")
                    and "target-refused" in run_entry_after.get("error", "")
                )
                if is_success:
                    updated_safety = self._record_publish_success_for_safety(run_id)
                elif is_failure:
                    updated_safety = self._record_publish_failure_for_safety(
                        run_id,
                        failure_reason=gh_error or "unknown-gh-error",
                    )
                else:
                    # Neither success nor failure (stayed local, refused, etc.)
                    updated_safety = current_safety_state
                # Carry updated safety state for artifact fields
                current_safety_state = updated_safety

        # --- Compute lifecycle_phase for run artifact and event ---
        # This makes the run's lifecycle explicit:
        # local-only | shadow | guarded-live-published | guarded-live-refused | guarded-live-failed
        # | safety-blocked
        runs_entries = prior_publish.get("runs", {})
        run_entry = runs_entries.get(run_id, {})
        is_shadow = run_entry.get("shadow", False)

        if is_shadow:
            lifecycle_phase = "shadow-published"
        elif not guard_enabled:
            lifecycle_phase = "guard-disabled"
        elif safety_blocked_live:
            # Circuit breaker blocked live publication; stayed local
            lifecycle_phase = "safety-blocked"
        elif filter_blocked_live:
            # Filters blocked live publication; stayed local/shadow
            lifecycle_phase = "filter-blocked"
        elif comment_url:
            lifecycle_phase = "guarded-live-published"
        elif run_entry.get("status") == PublishStatus.FAILED.value:
            lifecycle_phase = "guarded-live-failed"
        else:
            # Refused (target ambiguity, no open PR, etc.) or stayed local
            lifecycle_phase = "guarded-live-refused"

        operator_action_required = bool(current_safety_state.auto_rollback_active)
        operator_action_summary = None
        suggested_review_care_patch = None
        if current_safety_state.auto_rollback_active:
            operator_action_summary = (
                "disable-guarded-live-review-and-fallback-to-shadow"
            )
            suggested_review_care_patch = {
                "guarded_live_review": False,
                "live_rollout_mode": LiveRolloutMode.SHADOW.value,
            }

        return {
            "current_safety_state": current_safety_state,
            "lifecycle_phase": lifecycle_phase,
            "operator_action_required": operator_action_required,
            "operator_action_summary": operator_action_summary,
            "suggested_review_care_patch": suggested_review_care_patch,
        }

    def _attempt_live_publication(
        self,
        prior_publish: Dict[str, Any],
        live_rollout_mode: LiveRolloutMode,
        guard_enabled: bool,
        findings_total: int,
        deduped: List[Dict[str, Any]],
        pr_context: Optional[Dict[str, Any]],
        pr_number: Optional[int],
        run_id: str,
        summary_text: str,
        dry_run: bool,
    ) -> Dict[str, Any]:
        """G7 first half: limited publish filters + safety check + live publication.

        Runs the publish filter (limited/shadow only), evaluates the circuit
        breaker (limited+guarded only), resolves the target PR (nullified when
        filters or safety block live), and posts the summary to GitHub when
        not dry_run.

        Mutates prior_publish (setdefault "runs"). Returns dict with keys:
            publish_filter_result, rollout_eligible, attention_recommended,
            filter_blocked_live, safety_blocked_live, safety_reason,
            current_safety_state, comment_url, resolved_target_pr
        """
        # Ensure runs dict exists before Phase J so that _post_summary_to_github
        # can safely update run entries in-place via prior_publish["runs"][run_id].
        prior_publish.setdefault("runs", {})

        # --- Phase G6: Limited publish filter check ---
        # Only apply filters when in limited mode AND guarded path is active.
        # Shadow mode always bypasses filters (it never publishes anyway).
        # local_only stays local regardless.
        publish_filter_result = _build_pass_filter_result()
        rollout_eligible = False
        attention_recommended = False
        filter_blocked_live = False

        if live_rollout_mode == LiveRolloutMode.LIMITED and guard_enabled:
            publish_filter_result = self._check_limited_publish_filters(
                findings_total=findings_total,
                findings_list=deduped,
                pr_context=pr_context,
            )
            rollout_eligible = publish_filter_result.passed
            attention_recommended = (
                not publish_filter_result.passed
                and publish_filter_result.failed_reason != ""
            )
            filter_blocked_live = not publish_filter_result.passed
        elif live_rollout_mode == LiveRolloutMode.SHADOW:
            # Shadow mode: still run filter check for observability but don't block
            publish_filter_result = self._check_limited_publish_filters(
                findings_total=findings_total,
                findings_list=deduped,
                pr_context=pr_context,
            )
            rollout_eligible = False  # shadow never goes live
            attention_recommended = (
                not publish_filter_result.passed
                and publish_filter_result.failed_reason != ""
            )

        # If filters failed in limited+guarded mode, block live publication
        # by nullifying the target PR (causes _post_summary_to_github to refuse)
        resolved_target_pr: Optional[int] = pr_number
        if filter_blocked_live:
            resolved_target_pr = None

        # --- Phase G7: Monitored-rollout safety check ---
        # Check circuit-breaker state before attempting live publication.
        # Only applies to limited+guarded path (shadow/local_only unaffected).
        circuit_allows_live = True
        safety_blocked_live = False
        safety_reason = "circuit-closed"
        safety_state_data = self.state.load_monitored_safety_state(
            self.repo.config.name
        )
        current_safety_state = MonitoredSafetyState.from_dict(safety_state_data)

        if live_rollout_mode == LiveRolloutMode.LIMITED and guard_enabled:
            current_safety_state = self._evaluate_feedback_auto_rollback(run_id)
            circuit_allows_live, safety_reason, current_safety_state = (
                self._check_monitored_safety()
            )
            safety_blocked_live = not circuit_allows_live
            if safety_blocked_live:
                resolved_target_pr = None
            # Recommend operator attention when monitored safety is carrying risk
            if (
                current_safety_state.circuit_open
                or current_safety_state.failure_count > 0
                or current_safety_state.auto_rollback_active
            ):
                attention_recommended = True

        # Guarded live publication: _post_summary_to_github is always called (to
        # record PR resolution errors in the run entry), but the gh API call inside
        # it is gated by the guard. When guard is not enabled the run stays
        # local-only; the guard event already documents the reason.
        comment_url: Optional[str] = None
        if not dry_run:
            comment_url = self._post_summary_to_github(
                summary_text=summary_text,
                run_id=run_id,
                prior_publish=prior_publish,
                target_pr_number=resolved_target_pr,
            )

        return {
            "publish_filter_result": publish_filter_result,
            "rollout_eligible": rollout_eligible,
            "attention_recommended": attention_recommended,
            "filter_blocked_live": filter_blocked_live,
            "safety_blocked_live": safety_blocked_live,
            "safety_reason": safety_reason,
            "current_safety_state": current_safety_state,
            "comment_url": comment_url,
            "resolved_target_pr": resolved_target_pr,
        }

    def _reconcile_publish_state(
        self,
        deduped: List[Dict[str, Any]],
        prior_publish: Dict[str, Any],
        skipped_findings: List[Dict[str, Any]],
        run_id: str,
        pr_number: Optional[int],
        dry_run: bool,
    ) -> Dict[str, Any]:
        """G6: Reconcile against prior publish state, build per-finding entries.

        Computes reconciliation, builds per-finding publish statuses, persists
        finding entries into prior_publish (when not dry_run), computes count
        rollups + rollup_status, and builds the summary comment text.

        Mutates prior_publish in place. Returns dict with keys:
            reconciliation, finding_statuses, findings_total,
            findings_published, findings_failed, findings_skipped_count,
            rollup_status, summary_text
        """
        # --- 5. Reconcile against prior publish state ---
        reconciliation = reconcile_publish_state(deduped, prior_publish)

        # --- 6. Build per-finding publish entries (local only — no GitHub) ---
        finding_statuses: List[PublishStatus] = []

        for finding in deduped:
            fid = finding["finding_id"]
            if fid in reconciliation.already_published:
                status = PublishStatus.PUBLISHED
            elif fid in reconciliation.new_findings:
                # Stub: mark as published locally (no live GitHub call)
                status = PublishStatus.PUBLISHED
            elif fid in reconciliation.superseded_findings:
                status = PublishStatus.SUPERSEDED
            elif fid in reconciliation.absent_findings:
                status = PublishStatus.ABSENT
            else:
                status = PublishStatus.SKIPPED

            finding_statuses.append(status)

            if not dry_run:
                entry = build_publish_entry(
                    finding_id=fid,
                    status=status,
                    run_id=run_id,
                    finding_fingerprint=finding.get("finding_fingerprint"),
                )
                # Persist per-finding publish state
                prior_publish.setdefault("findings", {})
                prior_publish["findings"][fid] = entry

        # --- 7. Compute findings counts and rollup status (needed before Phase J) ---
        findings_total = len(deduped)
        findings_published = sum(
            1 for s in finding_statuses if s == PublishStatus.PUBLISHED
        )
        findings_failed = sum(1 for s in finding_statuses if s == PublishStatus.FAILED)
        findings_skipped_count = len(skipped_findings) + sum(
            1
            for s in finding_statuses
            if s in {PublishStatus.SKIPPED, PublishStatus.SUPERSEDED}
        )
        rollup_status = compute_run_publish_status(finding_statuses)

        # --- Phase J: Live publication bridge ---
        # Build summary text first so it can be published to GitHub.
        summary_text = build_review_summary_comment(
            repo=self.repo.config.name,
            run_id=run_id,
            reconciliation=reconciliation,
            run_status=rollup_status.value,
            run_error=None,
            pr_number=pr_number,
        )

        return {
            "reconciliation": reconciliation,
            "finding_statuses": finding_statuses,
            "findings_total": findings_total,
            "findings_published": findings_published,
            "findings_failed": findings_failed,
            "findings_skipped_count": findings_skipped_count,
            "rollup_status": rollup_status,
            "summary_text": summary_text,
        }

    def _check_remediation_eligibility(
        self,
        deduped: List[Dict[str, Any]],
    ) -> tuple:
        """G5: Split findings into eligible/ineligible buckets.

        Returns (eligible_findings, ineligible_findings).
        """
        eligible_findings: List[Dict[str, Any]] = []
        ineligible_findings: List[Dict[str, Any]] = []
        for finding in deduped:
            eligibility = is_remediation_eligible(finding, repo_config=self.repo.config)
            if eligibility.eligible:
                eligible_findings.append(finding)
            else:
                ineligible_findings.append(finding)
        return eligible_findings, ineligible_findings

    def _apply_learned_and_emergent_rules(
        self,
        deduped: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """G3+G4: Apply learned-rule suppression and observe emergent rules.

        Loads existing learned rules, applies suppression/activation, persists
        updated rules state, emits learned-rule-log events, then observes the
        (post-rule) findings with the EmergentRuleStore and extends the list
        with any newly-active emergent findings.

        Returns the filtered/extended findings list (caller must reassign).
        """
        # --- Phase J: Process learned rules (conservative pattern learning) ---
        # Load existing rules and apply learned-rule suppression/activation.
        # This runs BEFORE remediation eligibility to allow learned rules
        # to suppress low-risk repeated findings before they enter the
        # publish pipeline.  Reaction-only signals are NOT consulted here.
        rules_state = _get_learned_rules_state(self.state, self.repo.config.name)
        _run_id_for_rules = generate_id("arun")
        _patterns_file = (
            self.state._get_state_dir(self.repo.config.name) / "fix_patterns.json"
        )
        (
            deduped_after_rules,
            updated_rules,
            rules_log,
        ) = _process_learned_rules_for_run(
            deduped, rules_state, _run_id_for_rules, patterns_file=_patterns_file
        )

        suppressed_by_rules = len(deduped) - len(deduped_after_rules)
        if suppressed_by_rules > 0 or rules_log:
            for log_line in rules_log:
                # Emit as structured events for traceability
                self.state.append_review_event(
                    self.repo.config.name,
                    {
                        "event": "learned-rule-log",
                        "run_id": _run_id_for_rules,
                        "provider": "local-stub",
                        "details": {"log": log_line},
                    },
                )

        # Persist updated rules state
        _save_learned_rules_state(
            self.state,
            self.repo.config.name,
            _build_learned_rules_payload(updated_rules),
        )
        # Use the filtered findings for the rest of the pipeline
        deduped = deduped_after_rules

        # Inverted dependency: invoke the emergent-rule observer callback
        # (wired by app/runner.py) instead of importing EmergentRuleStore
        # directly. When no callback is wired, this whole block is skipped.
        # getattr() defensively tolerates ReviewCycleEngine instances built
        # via __new__ that bypass __init__ (common in tests).
        _observer = getattr(self, "emergent_rule_observer", None)
        if _observer is not None:
            try:
                _er_path = (
                    self.state._get_state_dir(self.repo.config.name)
                    / "emergent_rules.json"
                )
                _er_new = _observer(_er_path, deduped, _run_id_for_rules)
                if _er_new:
                    deduped.extend(_er_new)
            except Exception:
                pass

        return deduped

    def _gather_normalized_candidates(
        self,
        run_id: str,
        pr_context: Optional[Dict[str, Any]],
        guard_enabled: bool,
    ) -> Dict[str, Any]:
        """G1+G2: Generate candidates, normalize, dedupe, assign identity.

        Resolves live-rollout mode + live_actions gating, generates backend or
        local-stub candidates, normalizes, assigns identity, and deduplicates
        with mnemo candidate signals.

        Returns dict with keys:
            deduped, skipped_findings, candidate_source,
            live_rollout_mode, rollout_reason, live_actions
        """
        # --- 2. Generate candidates (backend preferred; local stub fallback) ---
        # Backend generation is gated by live_rollout_mode (Phase G5):
        # - shadow mode: backend NOT blocked (shadow allows full backend + targeting,
        #   just never actually publishes to GitHub)
        # - limited mode: backend allowed only when guarded_live_review is enabled
        #   (standard guarded progression)
        # - local_only: backend blocked when live_actions=True (stays local-only)
        # - live_actions=False: backend always allowed for local analysis
        live_rollout_mode, rollout_reason = self._get_live_rollout_mode()
        live_actions = bool(self.repo.config.github.get("live_actions", False))
        if live_rollout_mode == LiveRolloutMode.SHADOW:
            backend_blocked = False
        elif live_rollout_mode == LiveRolloutMode.LIMITED:
            backend_blocked = live_actions and not guard_enabled
        else:
            # local_only (or fallback)
            backend_blocked = live_actions and not guard_enabled
        backend_raw_candidates: List[Dict[str, Any]] = []
        candidate_source: str
        if not backend_blocked:
            backend_raw_candidates = self._generate_from_backend(
                run_id, pr_context=pr_context
            )
            if backend_raw_candidates:
                raw_candidates = backend_raw_candidates
                candidate_source = "backend"
            else:
                # Backend returned no valid candidates; fall back to safe local stub.
                raw_candidates = self._generate_local_candidates()
                candidate_source = "local-stub-fallback"
        else:
            # Guard blocks backend — use local stub directly
            raw_candidates = self._generate_local_candidates()
            candidate_source = "local-stub"

        # --- 3. Normalize → deduplicate → assign identity ---
        validated_findings: List[Dict[str, Any]] = []
        skipped_findings: List[Dict[str, Any]] = []
        for raw in raw_candidates:
            try:
                normalized = normalize_candidate(raw)
            except CandidateValidationError:
                skipped_findings.append(raw)
                continue
            with_identity = assign_finding_identity(normalized)
            validated_findings.append(with_identity)

        deduped = dedupe_findings(validated_findings)
        deduped = self._apply_mnemo_candidate_signals(deduped)

        return {
            "deduped": deduped,
            "skipped_findings": skipped_findings,
            "candidate_source": candidate_source,
            "live_rollout_mode": live_rollout_mode,
            "rollout_reason": rollout_reason,
            "live_actions": live_actions,
        }

    def _run_autonomous_review_cycle(
        self, dry_run: bool = True, allow_review_push: bool = False
    ) -> ReviewCycleResult:
        """Execute a local autonomous-review cycle with guarded live publication.

        Generates candidates (backend or local stub), normalizes, deduplicates,
        checks remediation eligibility, reconciles against prior state, applies
        learned rules, and optionally publishes a summary comment to GitHub.

        Args:
            dry_run: If True, return immediately without processing.
            allow_review_push: If True, allow pushing remediation commits.

        Returns:
            ReviewCycleResult with finding counters.
        """
        result = ReviewCycleResult()

        # --- Guard: dry-run returns immediately without any processing ---
        if dry_run:
            return result

        # --- 1. Load prior publish state (for reconciliation) ---
        prior_publish = self.state.load_review_publish_state(self.repo.config.name)

        # --- 1b. Resolve PR context (explicit-first; used throughout the run) ---
        # Carries PR number through run creation, prompt artifact, publish state,
        # summary generation, and events when a PR is known.
        pr_number, pr_resolution_reason = self._resolve_pr_context_for_autonomous_run(
            prior_publish,
        )
        pr_context: Optional[Dict[str, Any]] = (
            {"pr_number": pr_number, "pr_url": None, "resolution": pr_resolution_reason}
            if pr_number is not None
            else None
        )

        # --- Guard evaluation (Phase G4) ---
        # Determine whether the guarded live-review path is enabled.
        # This gates backend generation and live GitHub publication.
        # The full local pipeline (normalization, state, summary artifact)
        # always runs regardless of guard status.
        guard_enabled, guard_reason = self._is_guarded_live_review_enabled()
        run_id = generate_id("arun")
        self._emit_guard_event(
            run_id=run_id,
            pr_number=pr_number,
            guard_enabled=guard_enabled,
            guard_reason=guard_reason,
        )

        # --- 2/3. Generate candidates (G1) + normalize/dedupe (G2) ---
        g1g2 = self._gather_normalized_candidates(
            run_id=run_id,
            pr_context=pr_context,
            guard_enabled=guard_enabled,
        )
        deduped = g1g2["deduped"]
        skipped_findings: List[Dict[str, Any]] = g1g2["skipped_findings"]
        candidate_source: str = g1g2["candidate_source"]
        live_rollout_mode = g1g2["live_rollout_mode"]
        rollout_reason = g1g2["rollout_reason"]
        live_actions = g1g2["live_actions"]

        # --- G3+G4: Apply learned rules + observe emergent rules ---
        deduped = self._apply_learned_and_emergent_rules(deduped)

        # --- 4. Check remediation eligibility (G5) ---
        eligible_findings, ineligible_findings = self._check_remediation_eligibility(
            deduped
        )

        # --- G6: Reconcile publish state + per-finding entries + summary ---
        g6 = self._reconcile_publish_state(
            deduped=deduped,
            prior_publish=prior_publish,
            skipped_findings=skipped_findings,
            run_id=run_id,
            pr_number=pr_number,
            dry_run=dry_run,
        )
        reconciliation = g6["reconciliation"]
        findings_total = g6["findings_total"]
        findings_published = g6["findings_published"]
        findings_failed = g6["findings_failed"]
        findings_skipped_count = g6["findings_skipped_count"]
        rollup_status = g6["rollup_status"]
        summary_text = g6["summary_text"]

        # --- G7 first half: publish filters + safety check + live publication ---
        g7a = self._attempt_live_publication(
            prior_publish=prior_publish,
            live_rollout_mode=live_rollout_mode,
            guard_enabled=guard_enabled,
            findings_total=findings_total,
            deduped=deduped,
            pr_context=pr_context,
            pr_number=pr_number,
            run_id=run_id,
            summary_text=summary_text,
            dry_run=dry_run,
        )
        publish_filter_result = g7a["publish_filter_result"]
        rollout_eligible = g7a["rollout_eligible"]
        attention_recommended = g7a["attention_recommended"]
        filter_blocked_live = g7a["filter_blocked_live"]
        safety_blocked_live = g7a["safety_blocked_live"]
        safety_reason = g7a["safety_reason"]
        current_safety_state = g7a["current_safety_state"]
        comment_url = g7a["comment_url"]
        resolved_target_pr = g7a["resolved_target_pr"]

        # --- G7 second half: record safety outcome + compute lifecycle ---
        g7b = self._record_safety_outcome(
            dry_run=dry_run,
            live_rollout_mode=live_rollout_mode,
            guard_enabled=guard_enabled,
            run_id=run_id,
            prior_publish=prior_publish,
            comment_url=comment_url,
            current_safety_state=current_safety_state,
            safety_blocked_live=safety_blocked_live,
            filter_blocked_live=filter_blocked_live,
        )
        current_safety_state = g7b["current_safety_state"]
        lifecycle_phase = g7b["lifecycle_phase"]
        operator_action_required = g7b["operator_action_required"]
        operator_action_summary = g7b["operator_action_summary"]
        suggested_review_care_patch = g7b["suggested_review_care_patch"]

        # --- G7 final: build run publish entry + persist + emit events ---
        self._build_run_artifact(
            result=result,
            dry_run=dry_run,
            prior_publish=prior_publish,
            run_id=run_id,
            pr_number=pr_number,
            guard_enabled=guard_enabled,
            guard_reason=guard_reason,
            live_rollout_mode=live_rollout_mode,
            rollout_reason=rollout_reason,
            live_actions=live_actions,
            candidate_source=candidate_source,
            deduped=deduped,
            eligible_findings=eligible_findings,
            ineligible_findings=ineligible_findings,
            reconciliation=reconciliation,
            findings_total=findings_total,
            findings_published=findings_published,
            findings_failed=findings_failed,
            findings_skipped_count=findings_skipped_count,
            rollup_status=rollup_status,
            summary_text=summary_text,
            comment_url=comment_url,
            resolved_target_pr=resolved_target_pr,
            publish_filter_result=publish_filter_result,
            rollout_eligible=rollout_eligible,
            attention_recommended=attention_recommended,
            safety_reason=safety_reason,
            current_safety_state=current_safety_state,
            lifecycle_phase=lifecycle_phase,
            operator_action_required=operator_action_required,
            operator_action_summary=operator_action_summary,
            suggested_review_care_patch=suggested_review_care_patch,
        )

        return result
