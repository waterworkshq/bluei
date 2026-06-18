"""State recording — extracted from review.observation.ObservationMixin.

Persistence of PR state mutations: building active/review state records,
appending review events, and finalizing/persisting state after the review-cycle
comment is published."""

from __future__ import annotations

from typing import Any, Dict, Optional

from bluei.review.types import ReviewCycleResult


class StateRecordingMixin:
    def _record_pr_in_active_state(
        self,
        *,
        pr: Dict[str, Any],
        pr_number: int,
        pr_key: str,
        snapshot: Dict[str, Any],
        previous_active_records: Dict[str, Any],
        status: str,
        merge_state: str,
        merge_reason: str,
        remediation_plan: Optional[Dict[str, Any]],
        execution_result: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build the active-state record describing the PR's current review posture."""
        return {
            "pr_number": pr_number,
            "url": pr.get("url"),
            "branch": pr.get("headRefName") or snapshot["branch"],
            "author": (pr.get("author") or {}).get("login") or snapshot["author"],
            "source": "bluei-heuristic",
            "status": status,
            "opened_at": (previous_active_records.get(pr_key, {}) or {}).get(
                "opened_at"
            )
            or snapshot["fetched_at"],
            "updated_at": snapshot["fetched_at"],
            "provider_summary": {
                "provider": "github",
                "review_decision": snapshot["review_decision"],
                "merge_state_status": snapshot["merge_state_status"],
                "active_change_requesters": snapshot["active_change_requesters"],
                "actionable_comment_count": len(snapshot["actionable_comments"]),
                "score_optional": None,
            },
            "merge_readiness": {
                "state": merge_state,
                "reason": merge_reason,
                "evaluated_at": snapshot["fetched_at"],
            },
            "remediation_plan": remediation_plan,
            "execution_result": execution_result,
        }

    def _record_pr_in_review_state(
        self,
        *,
        snapshot: Dict[str, Any],
        existing_review: Dict[str, Any],
        status: str,
        merge_reason: str,
        remediation_plan: Optional[Dict[str, Any]],
        execution_result: Optional[Dict[str, Any]],
        loop_count: int,
        paused: bool,
        stale_pause: bool,
        retry_eligible: bool,
        pending_push_cycles: int = 0,
    ) -> Dict[str, Any]:
        """Build the review-state record capturing the snapshot and remediation outcome."""
        return {
            "last_provider": "github",
            "last_polled_at": snapshot["fetched_at"],
            "last_snapshot_fingerprint": snapshot["fingerprint"],
            "last_snapshot": {
                "review_decision": snapshot["review_decision"],
                "merge_state_status": snapshot["merge_state_status"],
                "active_change_requesters": snapshot["active_change_requesters"],
                "actionable_comment_count": len(snapshot["actionable_comments"]),
                "informational_comment_count": len(snapshot["informational_comments"]),
            },
            "attempts_used": int(
                (execution_result or {}).get(
                    "attempts_used", existing_review.get("attempts_used", 0)
                )
            ),
            "loop_count": loop_count,
            "pending_push_cycles": pending_push_cycles,
            "retry_eligible": retry_eligible,
            # H6: top-level stable-signal counts so the next cycle can detect
            # reviewer rephrase (fingerprint changed but counts unchanged)
            # without resetting loop_count indefinitely.
            "change_requester_count": len(snapshot["active_change_requesters"]),
            "actionable_comment_count": len(snapshot["actionable_comments"]),
            "last_action": status,
            "last_action_at": snapshot["fetched_at"],
            "last_action_reason": merge_reason,
            "planned_remediation": remediation_plan,
            "execution_result": execution_result,
            "last_review_comment_key": existing_review.get("last_review_comment_key"),
            "last_review_comment_url": existing_review.get("last_review_comment_url"),
            "last_review_comment_at": existing_review.get("last_review_comment_at"),
            "escalation": (
                {
                    "kind": "loop_guard_paused",
                    "reason": "fingerprint repeated beyond threshold",
                    "at": snapshot["fetched_at"],
                }
                if paused and not stale_pause
                else None
            ),
        }

    def _append_pr_review_event(
        self,
        *,
        pr_number: int,
        snapshot: Dict[str, Any],
        status: str,
        loop_count: int,
        remediation_plan: Optional[Dict[str, Any]],
        execution_result: Optional[Dict[str, Any]],
    ) -> None:
        """Append the primary review-event entry for a PR snapshot."""
        self.state.append_review_event(
            self.repo.config.name,
            {
                "pr_number": pr_number,
                "event": status,
                "provider": "github",
                "fingerprint": snapshot["fingerprint"],
                "details": {
                    "review_decision": snapshot["review_decision"],
                    "actionable_comment_count": len(snapshot["actionable_comments"]),
                    "loop_count": loop_count,
                    "planned_prompt_file": (remediation_plan or {}).get("prompt_file"),
                    "worktree_path": (
                        (remediation_plan or {}).get("worktree") or {}
                    ).get("worktree_path"),
                    "backend_returncode": (
                        (execution_result or {}).get("backend_result") or {}
                    ).get("returncode"),
                    "changed_files": (execution_result or {}).get("changed_files"),
                    "validation_ok": (
                        (execution_result or {}).get("validation") or {}
                    ).get("ok"),
                },
            },
        )

    def _persist_active_and_review_state(
        self,
        *,
        pr_number: int,
        pr_key: str,
        snapshot: Dict[str, Any],
        active_state: Dict[str, Any],
        review_state: Dict[str, Any],
        active_records: Dict[str, Any],
        review_records: Dict[str, Any],
        result: ReviewCycleResult,
    ) -> None:
        """Finalize retry eligibility, publish the review-cycle comment,
        update records with publication info, and persist state.

        Computes ``final_retry_eligible`` from the post-remediation status,
        publishes the review cycle comment (reusing the prior publication when
        the publication key matches), records the comment URL on both active
        and review records, and writes state to disk.
        """
        final_status = active_records[pr_key]["status"]
        final_retry_eligible = final_status in {
            "review_feedback_detected",
            "retry_planned",
            "retry_prepared",
            "retry_lock_busy",
            "retry_failed",
            "retry_failed_timeout",
            "retry_failed_validation",
            "retry_no_changes",
        }
        active_records[pr_key]["retry_eligible"] = final_retry_eligible
        review_records[pr_key]["retry_eligible"] = final_retry_eligible
        if final_retry_eligible:
            result.retry_eligible_prs += 1

        final_merge_readiness = active_records[pr_key]["merge_readiness"]
        publication_key = f"{snapshot['fingerprint']}:{final_status}"
        review_comment_url = self._publish_review_cycle_comment(
            pr_number=pr_number,
            summary_text=self._build_review_cycle_comment(
                pr_number=pr_number,
                snapshot=snapshot,
                status=final_status,
                merge_readiness=final_merge_readiness,
                execution_result=active_records[pr_key].get("execution_result"),
            ),
            publication_key=publication_key,
            existing_review=review_records[pr_key],
        )
        if review_comment_url:
            review_records[pr_key]["last_review_comment_key"] = publication_key
            review_records[pr_key]["last_review_comment_url"] = review_comment_url
            review_records[pr_key]["last_review_comment_at"] = snapshot["fetched_at"]
            active_records[pr_key]["review_comment"] = {
                "url": review_comment_url,
                "key": publication_key,
                "published_at": snapshot["fetched_at"],
            }
        elif review_records[pr_key].get("last_review_comment_url"):
            active_records[pr_key]["review_comment"] = {
                "url": review_records[pr_key].get("last_review_comment_url"),
                "key": review_records[pr_key].get("last_review_comment_key"),
                "published_at": review_records[pr_key].get("last_review_comment_at"),
            }
        self._persist_review_state(active_state, review_state, result)
