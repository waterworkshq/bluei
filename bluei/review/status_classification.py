"""Status classification — extracted from review.observation.ObservationMixin.

PR snapshot classification into a review status (with retry / loop / pending-push
tracking) and execution of prepared remediation plans. Both methods determine
what state a PR is in and how to act on it."""

from __future__ import annotations

from typing import Any, Dict, Optional

from bluei.review.observation import _MAX_PENDING_PUSH_CYCLES
from bluei.review.types import ReviewCycleResult


class StatusClassificationMixin:
    def _classify_pr_status(
        self,
        *,
        snapshot: Dict[str, Any],
        existing_review: Dict[str, Any],
        dry_run: bool,
        lock_handle: Any,
        result: ReviewCycleResult,
    ) -> Dict[str, Any]:
        """Classify a PR snapshot into a review status and remediation plan.

        Computes loop-count tracking, retry eligibility, status transitions
        across the seven review states, and any remediation plan produced by
        ``_plan_remediation``. Mutates ``result`` counters as a side effect.

        Returns a dict with keys: ``loop_count``, ``stale_pause``, ``status``,
        ``merge_state``, ``merge_reason``, ``paused``, ``remediation_plan``,
        ``execution_result``, ``pending_push_cycles``.
        """
        existing_fingerprint = existing_review.get("last_snapshot_fingerprint")
        previous_action = str(existing_review.get("last_action") or "")
        loop_count = int(existing_review.get("loop_count", 0))
        # Default to 0; only set non-zero while staying in retry_pending_push.
        # Any transition out (fingerprint change, escalation, status change)
        # implicitly resets because we never increment in those branches.
        pending_push_cycles = 0
        previous_attempted_remediation = previous_action in {
            "retry_executed",
            "retry_pushed",
            "retry_failed",
            "retry_failed_validation",
            "retry_no_changes",
            "retry_failed_timeout",
            "retry_failed_push",
        }
        stale_pause = (not previous_attempted_remediation) and int(
            existing_review.get("attempts_used", 0)
        ) == 0
        # Stable-signal counts — used to detect reviewer rephrase vs. genuine
        # new review content when the fingerprint changes. The fingerprint is
        # a hash over comment bodies, so any rewording flips it even when the
        # underlying concern (and number of commenters / comments) is static.
        current_change_requester_count = len(snapshot["active_change_requesters"])
        current_actionable_comment_count = len(snapshot["actionable_comments"])
        prev_change_requester_count = existing_review.get("change_requester_count")
        prev_actionable_comment_count = existing_review.get("actionable_comment_count")
        if (
            existing_fingerprint == snapshot["fingerprint"]
            and snapshot["actionable_comments"]
        ):
            if previous_attempted_remediation:
                loop_count += 1
            else:
                loop_count = 0
        elif existing_fingerprint and existing_fingerprint != snapshot["fingerprint"]:
            # H6: semantic-level loop guard. A fingerprint change normally means
            # the reviewer left genuinely new content, so we reset the counter.
            # But if the *stable signal* (number of change-requesters and
            # actionable comments) is unchanged AND we attempted remediation
            # last cycle, the reviewer most likely rephrased the same concern.
            # In that case preserve loop_count rather than resetting to 0 —
            # otherwise a reviewer can keep the bot in an infinite remediation
            # loop simply by rewording each time. We only PRESERVE (never
            # increment here) to avoid false-positive loop detection; when in
            # doubt (missing prior counts), we reset (backward compatible).
            counts_unchanged = (
                prev_change_requester_count is not None
                and prev_actionable_comment_count is not None
                and int(prev_change_requester_count) == current_change_requester_count
                and int(prev_actionable_comment_count)
                == current_actionable_comment_count
            )
            if counts_unchanged and previous_attempted_remediation:
                # Rephrase of the same concern — preserve loop_count.
                pass
            else:
                loop_count = 0

        retry_eligible = bool(
            snapshot["actionable_comments"] or snapshot["active_change_requesters"]
        )
        merge_state_status = str(snapshot.get("merge_state_status") or "UNKNOWN")
        prior_comment_key = str(existing_review.get("last_review_comment_key") or "")
        current_snapshot_prefix = f"{snapshot['fingerprint']}:"
        current_merge_ready_key = f"{snapshot['fingerprint']}:merge_ready"
        review_artifact_exists_for_snapshot = prior_comment_key.startswith(
            current_snapshot_prefix
        )
        merge_ready_artifact_exists_for_snapshot = (
            prior_comment_key == current_merge_ready_key
        )
        merge_state = "not_merge_ready"
        merge_reason = "Awaiting review evaluation"
        status = "pending_review"
        paused = False
        remediation_plan: Optional[Dict[str, Any]] = None
        execution_result: Optional[Dict[str, Any]] = None
        if retry_eligible:
            status = "review_feedback_detected"
            merge_state = "blocked_by_review"
            merge_reason = (
                f"{len(snapshot['actionable_comments'])} actionable review comments"
            )
            result.blocked_prs += 1
            attempts_used = int(existing_review.get("attempts_used", 0))
            max_attempts = int(self.repo.config.review_care.get("max_attempts", 3))
            if (
                previous_action == "retry_pending_push"
                and existing_fingerprint == snapshot["fingerprint"]
            ):
                # Increment pending-push counter; escalate to retry_exhausted
                # after _MAX_PENDING_PUSH_CYCLES to avoid indefinite pinning.
                pending_push_cycles = (
                    int(existing_review.get("pending_push_cycles", 0)) + 1
                )
                if pending_push_cycles >= _MAX_PENDING_PUSH_CYCLES:
                    status = "retry_exhausted"
                    merge_reason = (
                        f"Pending-push timeout: stuck {pending_push_cycles} "
                        "cycles awaiting operator push"
                    )
                    result.retry_exhausted_prs += 1
                    pending_push_cycles = 0
                else:
                    status = "retry_pending_push"
                    merge_state = "awaiting_operator_push"
                    merge_reason = "Validated remediation is waiting for explicit commit/push approval"
                    remediation_plan = existing_review.get("planned_remediation")
                    execution_result = existing_review.get("execution_result")
            elif attempts_used >= max_attempts:
                status = "retry_exhausted"
                merge_reason = f"Max retry attempts ({max_attempts}) exhausted"
                result.retry_exhausted_prs += 1
            elif loop_count > int(self.repo.config.review_care.get("max_loops", 2)):
                status = "loop_guard_paused"
                paused = True
                result.paused_prs += 1
            elif not dry_run and not lock_handle:
                status = "retry_lock_busy"
                merge_reason = "PR remediation lock already held"
            else:
                remediation_plan = self._plan_remediation(
                    snapshot, existing_review, dry_run
                )
                if remediation_plan:
                    status = remediation_plan["status"]
                    result.retry_planned_prs += 1
                    if remediation_plan["status"] == "retry_prepared":
                        result.retry_prepared_prs += 1
        elif merge_state_status in {"CLEAN", "UNKNOWN", "UNSTABLE"}:
            if (
                merge_ready_artifact_exists_for_snapshot
                or review_artifact_exists_for_snapshot
            ):
                status = "merge_ready"
                merge_state = "ready_for_merge"
                if merge_state_status == "CLEAN":
                    merge_reason = "QA review artifact exists for this snapshot and no actionable review blockers were found"
                else:
                    merge_reason = (
                        "QA review artifact exists for this snapshot and no actionable review blockers were found, "
                        f"with merge state {merge_state_status.lower()} pending fresh merge triage"
                    )
                result.merge_ready_prs += 1
            else:
                status = "pending_review"
                merge_state = "awaiting_review_artifact"
                if merge_state_status == "CLEAN":
                    merge_reason = "Clean PR, but bluei review for this snapshot has not been published yet"
                else:
                    merge_reason = (
                        "No actionable review blockers and merge state is "
                        f"{merge_state_status.lower()}, but bluei review for this snapshot has not been published yet"
                    )
        else:
            status = "awaiting_mergeability"
            merge_state = "awaiting_mergeability"
            merge_reason = (
                "No actionable review blockers, but merge state is "
                f"{merge_state_status.lower()}"
            )

        return {
            "loop_count": loop_count,
            "stale_pause": stale_pause,
            "status": status,
            "merge_state": merge_state,
            "merge_reason": merge_reason,
            "paused": paused,
            "remediation_plan": remediation_plan,
            "execution_result": execution_result,
            "pending_push_cycles": pending_push_cycles,
            "change_requester_count": current_change_requester_count,
            "actionable_comment_count": current_actionable_comment_count,
        }

    def _handle_retry_remediation(
        self,
        *,
        pr_number: int,
        pr_key: str,
        snapshot: Dict[str, Any],
        existing_review: Dict[str, Any],
        remediation_plan: Dict[str, Any],
        fallback_status: str,
        dry_run: bool,
        allow_review_push: bool,
        active_records: Dict[str, Any],
        review_records: Dict[str, Any],
        result: ReviewCycleResult,
    ) -> None:
        """Execute a prepared remediation plan and update PR records in place.

        Runs ``_execute_prepared_remediation``, applies the resulting
        execution status to the active/review records, refreshes merge-readiness
        for retry states, bumps the appropriate result counters, and appends a
        review event describing the execution outcome.
        """
        execution_result = self._execute_prepared_remediation(
            remediation_plan,
            existing_review,
            dry_run,
            allow_review_push=allow_review_push,
            snapshot=snapshot,
        )
        execution_status = execution_result.get("status") or fallback_status
        if execution_result.get("executed"):
            if execution_status in {
                "retry_executed",
                "retry_pending_push",
                "retry_pushed",
            }:
                result.retry_executed_prs += 1
            elif execution_status.startswith("retry_failed"):
                result.retry_failed_prs += 1
        active_records[pr_key]["status"] = execution_status
        active_records[pr_key]["execution_result"] = execution_result
        review_records[pr_key]["attempts_used"] = int(
            execution_result.get(
                "attempts_used",
                review_records[pr_key].get("attempts_used", 0),
            )
        )
        review_records[pr_key]["last_action"] = execution_status
        review_records[pr_key]["execution_result"] = execution_result
        review_records[pr_key]["planned_remediation"] = remediation_plan
        if execution_status == "retry_pending_push":
            active_records[pr_key]["merge_readiness"] = {
                "state": "awaiting_operator_push",
                "reason": "Validated remediation is waiting for explicit commit/push approval",
                "evaluated_at": snapshot["fetched_at"],
            }
        elif execution_status in {"retry_executed", "retry_pushed"}:
            active_records[pr_key]["merge_readiness"] = {
                "state": "awaiting_re_review",
                "reason": "Remediation pushed; awaiting new review snapshot"
                if execution_status == "retry_pushed"
                else "Remediation executed locally; awaiting commit/push boundary",
                "evaluated_at": snapshot["fetched_at"],
            }
        self.state.append_review_event(
            self.repo.config.name,
            {
                "pr_number": pr_number,
                "event": execution_status,
                "provider": "github",
                "fingerprint": snapshot["fingerprint"],
                "details": {
                    "backend_returncode": (
                        (execution_result or {}).get("backend_result") or {}
                    ).get("returncode"),
                    "changed_files": (execution_result or {}).get("changed_files"),
                    "validation_ok": (
                        (execution_result or {}).get("validation") or {}
                    ).get("ok"),
                    "push_status": (
                        (execution_result or {}).get("push_result") or {}
                    ).get("status"),
                },
            },
        )
