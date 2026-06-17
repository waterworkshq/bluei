"""Observation — extracted from review.cycle for single-responsibility.

Observation cycle: read-only PR scanning, comment classification, summary posting.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

from bluei.common.models import LiveRolloutMode, now_iso
from bluei.review.models import PublishStatus
from bluei.review.types import ReviewCycleResult

# Proactive inter-PR throttle. Default 0 keeps existing behavior; opt in via
# review_care.inter_pr_delay_seconds to space out sequential API calls and
# reduce the chance of tripping GitHub's secondary rate limit on PR-heavy repos.
_DEFAULT_INTER_PR_DELAY_SECONDS = 0.0

# Maximum number of consecutive cycles a PR may stay in retry_pending_push
# (with unchanged fingerprint) before escalating to retry_exhausted. Prevents
# indefinite pinning when the retry path reuses execution_result without
# re-running remediation, freezing attempts_used and bypassing escalation.
_MAX_PENDING_PUSH_CYCLES = 5


class ObservationMixin:
    def _run_observation_cycle(
        self, dry_run: bool = True, allow_review_push: bool = False
    ) -> ReviewCycleResult:
        result = ReviewCycleResult()
        managed_prs = self.provider.list_managed_prs()
        active_state = self.state.load_active_prs(self.repo.config.name)
        review_state = self.state.load_review_state(self.repo.config.name)
        previous_active_records = dict(active_state.get("prs", {}))
        previous_review_records = dict(review_state.get("prs", {}))
        active_records: Dict[str, Any] = {}
        review_records: Dict[str, Any] = {}
        active_state["prs"] = active_records
        review_state["prs"] = review_records

        _MAX_UNREACHABLE_ATTEMPTS = 10
        _RECOVERY_BACKOFF_MINUTES = [30, 60, 120, 240, 480, 720, 1440, 2880, 5760]
        current_pr_numbers = {str(p["number"]) for p in managed_prs}
        for pr_key, prev_record in previous_active_records.items():
            if pr_key not in current_pr_numbers:
                if (
                    prev_record.get("status") == "temporarily_unreachable"
                    and not dry_run
                ):
                    recovery_attempts = prev_record.get("recovery_attempts", 0)
                    next_recovery_at = prev_record.get("next_recovery_at")
                    now = datetime.now(timezone.utc)

                    if next_recovery_at:
                        try:
                            if isinstance(next_recovery_at, str):
                                nxt = datetime.fromisoformat(next_recovery_at)
                            else:
                                nxt = next_recovery_at
                            if now < nxt:
                                active_records[pr_key] = {
                                    **prev_record,
                                    "status": "temporarily_unreachable",
                                    "updated_at": now_iso(),
                                }
                                review_records[pr_key] = previous_review_records.get(
                                    pr_key, {}
                                )
                                continue
                        except (ValueError, TypeError):
                            pass

                    if recovery_attempts >= _MAX_UNREACHABLE_ATTEMPTS:
                        # Inverted dependency: invoke the escalation_writer
                        # callback (wired by app/runner.py) instead of importing
                        # write_escalation directly from app/. When no callback
                        # is wired, escalation is logged only. getattr()
                        # defensively tolerates engines built via __new__.
                        _writer = getattr(self, "escalation_writer", None)
                        if _writer is not None:
                            try:
                                _writer(
                                    message=(
                                        f"PR #{pr_key} unreachable after {recovery_attempts} recovery attempts — "
                                        "manual intervention needed"
                                    ),
                                    severity="error",
                                    repo=self.repo.config.name,
                                )
                            except Exception:
                                _logger.debug(
                                    "escalation_writer callback raised", exc_info=True
                                )
                        _logger.warning(
                            "Unreachable PR #%s exceeded %d recovery attempts — escalated",
                            pr_key,
                            _MAX_UNREACHABLE_ATTEMPTS,
                        )
                        active_records[pr_key] = {
                            **prev_record,
                            "status": "permanently_unreachable",
                            "recovery_attempts": recovery_attempts,
                            "updated_at": now_iso(),
                        }
                        review_records[pr_key] = previous_review_records.get(pr_key, {})
                        continue

                    try:
                        slug = self.provider._get_repo_slug()
                        pr_data = json.loads(
                            self._run_repo_cmd(
                                ["gh", "api", f"/repos/{slug}/pulls/{pr_key}"],
                            )
                        )
                        if pr_data.get("state") == "open":
                            _logger.info(
                                "Recovered previously unreachable PR #%s "
                                "(was stuck for %d attempts)",
                                pr_key,
                                recovery_attempts,
                            )
                            managed_prs.append(pr_data)
                            current_pr_numbers.add(pr_key)
                            continue
                    except Exception as exc:
                        _logger.debug(
                            "Recovery fetch failed for PR #%s: %s", pr_key, exc
                        )

                    new_attempts = recovery_attempts + 1
                    backoff_idx = min(
                        new_attempts - 1, len(_RECOVERY_BACKOFF_MINUTES) - 1
                    )
                    backoff_minutes = _RECOVERY_BACKOFF_MINUTES[backoff_idx]
                    next_recovery = now + timedelta(minutes=backoff_minutes)
                    _logger.info(
                        "Unreachable PR #%s recovery attempt %d failed — "
                        "next attempt in %d min",
                        pr_key,
                        new_attempts,
                        backoff_minutes,
                    )
                    active_records[pr_key] = {
                        **prev_record,
                        "status": "temporarily_unreachable",
                        "recovery_attempts": new_attempts,
                        "next_recovery_at": next_recovery.isoformat(),
                        "updated_at": now_iso(),
                    }
                    review_records[pr_key] = previous_review_records.get(pr_key, {})
                    continue

                active_records[pr_key] = {
                    **prev_record,
                    "status": "temporarily_unreachable",
                    "updated_at": now_iso(),
                }
                review_records[pr_key] = previous_review_records.get(pr_key, {})

        inter_pr_delay_seconds = float(
            (self.repo.config.review_care or {}).get(
                "inter_pr_delay_seconds", _DEFAULT_INTER_PR_DELAY_SECONDS
            )
        )

        for idx, pr in enumerate(
            managed_prs[
                : int(self.repo.config.review_care.get("max_prs_per_run", 1) or 1) * 20
            ]
        ):
            if idx > 0 and not dry_run and inter_pr_delay_seconds > 0:
                _time.sleep(inter_pr_delay_seconds)
            pr_number = int(pr["number"])
            pr_key = str(pr_number)
            lock_handle = None
            try:
                if not dry_run:
                    lock_handle = self._acquire_pr_lock(pr_number)
                snapshot = self.provider.fetch_review_snapshot(pr_number)
                existing_review = previous_review_records.get(pr_key, {})
                classification = self._classify_pr_status(
                    snapshot=snapshot,
                    existing_review=existing_review,
                    dry_run=dry_run,
                    lock_handle=lock_handle,
                    result=result,
                )
                loop_count = classification["loop_count"]
                stale_pause = classification["stale_pause"]
                status = classification["status"]
                merge_state = classification["merge_state"]
                merge_reason = classification["merge_reason"]
                paused = classification["paused"]
                remediation_plan = classification["remediation_plan"]
                execution_result = classification["execution_result"]
                pending_push_cycles = classification["pending_push_cycles"]

                final_retry_eligible = False

                result.active_prs += 1
                active_records[pr_key] = self._record_pr_in_active_state(
                    pr=pr,
                    pr_number=pr_number,
                    pr_key=pr_key,
                    snapshot=snapshot,
                    previous_active_records=previous_active_records,
                    status=status,
                    merge_state=merge_state,
                    merge_reason=merge_reason,
                    remediation_plan=remediation_plan,
                    execution_result=execution_result,
                )
                review_records[pr_key] = self._record_pr_in_review_state(
                    snapshot=snapshot,
                    existing_review=existing_review,
                    status=status,
                    merge_reason=merge_reason,
                    remediation_plan=remediation_plan,
                    execution_result=execution_result,
                    loop_count=loop_count,
                    paused=paused,
                    stale_pause=stale_pause,
                    retry_eligible=final_retry_eligible,
                    pending_push_cycles=pending_push_cycles,
                )
                if not dry_run:
                    self._append_pr_review_event(
                        pr_number=pr_number,
                        snapshot=snapshot,
                        status=status,
                        loop_count=loop_count,
                        remediation_plan=remediation_plan,
                        execution_result=execution_result,
                    )

                    if (
                        remediation_plan
                        and remediation_plan.get("status") == "retry_prepared"
                    ):
                        self._handle_retry_remediation(
                            pr_number=pr_number,
                            pr_key=pr_key,
                            snapshot=snapshot,
                            existing_review=existing_review,
                            remediation_plan=remediation_plan,
                            fallback_status=status,
                            dry_run=dry_run,
                            allow_review_push=allow_review_push,
                            active_records=active_records,
                            review_records=review_records,
                            result=result,
                        )

                    self._persist_active_and_review_state(
                        pr_number=pr_number,
                        pr_key=pr_key,
                        snapshot=snapshot,
                        active_state=active_state,
                        review_state=review_state,
                        active_records=active_records,
                        review_records=review_records,
                        result=result,
                    )
            finally:
                self._release_pr_lock(lock_handle)

        if not dry_run:
            self._persist_review_state(active_state, review_state, result)
        return result

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

    def _find_open_prs(self) -> List[Dict[str, Any]]:
        try:
            owner = (
                self.repo.config.github.get("owner")
                or self.repo.config.name.split("/")[0]
                if "/" in self.repo.config.name
                else ""
            )
            repo_name = (
                self.repo.config.github.get("repo")
                or self.repo.config.name.split("/")[1]
                if "/" in self.repo.config.name
                else self.repo.config.name
            )
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--json",
                    "number,title,updatedAt",
                    "--repo",
                    f"{owner}/{repo_name}",
                    "--state",
                    "open",
                    "--limit",
                    "10",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return []
            prs = json.loads(result.stdout)
            prs.sort(key=lambda p: p.get("updatedAt", ""), reverse=True)
            return prs
        except Exception:
            return []

    def _resolve_target_pr_for_run(
        self,
        prior_publish: Dict[str, Any],
    ) -> Tuple[Optional[int], str]:
        runs_entries = prior_publish.get("runs", {})
        prior_targeted: List[int] = []
        for rid, rentry in runs_entries.items():
            tpn = rentry.get("targeted_pr_number")
            if tpn is not None:
                try:
                    prior_targeted.append(int(tpn))
                except (TypeError, ValueError):
                    pass

        if len(set(prior_targeted)) == 1:
            confirmed = prior_targeted[0]
            open_prs = self._find_open_prs()
            open_numbers = {p["number"] for p in open_prs}
            if open_numbers and confirmed not in open_numbers:
                return (
                    None,
                    f"prior-targeted-pr-{confirmed}-now-closed",
                )
            return (
                confirmed,
                f"prior-targeted-pr-{confirmed}-reused",
            )

        try:
            managed_prs = self.provider.list_managed_prs()
        except Exception:
            managed_prs = []
        if len(managed_prs) == 1:
            pr_number = int(managed_prs[0]["number"])
            return (pr_number, f"single-managed-pr-{pr_number}")
        if len(managed_prs) > 1:
            return (
                None,
                f"multiple-managed-prs-{len(managed_prs)}-refused",
            )

        open_prs = self._find_open_prs()
        if len(open_prs) == 1:
            pr_number = int(open_prs[0]["number"])
            return (pr_number, f"single-open-pr-{pr_number}")
        if len(open_prs) > 1:
            return (
                None,
                f"multiple-open-prs-{len(open_prs)}-refused",
            )

        return (None, "no-open-prs")

    def _post_summary_to_github(
        self,
        summary_text: str,
        run_id: str,
        prior_publish: Dict[str, Any],
        target_pr_number: Optional[int] = None,
    ) -> Optional[str]:
        if not self.repo.config.github.get("live_actions", False):
            return None

        runs_entries = prior_publish.setdefault("runs", {})
        existing = runs_entries.get(run_id, {})
        if existing.get("comment_url"):
            return existing["comment_url"]

        if run_id not in runs_entries:
            runs_entries[run_id] = {}

        if target_pr_number is None:
            target_pr_number, resolution_reason = self._resolve_target_pr_for_run(
                prior_publish,
            )
        else:
            resolution_reason = f"explicit-{target_pr_number}"

        live_rollout_mode, _ = self._get_live_rollout_mode()
        if live_rollout_mode == LiveRolloutMode.SHADOW:
            runs_entries[run_id]["status"] = PublishStatus.PENDING.value
            runs_entries[run_id]["shadow"] = True
            runs_entries[run_id]["shadow_summary_text"] = summary_text
            runs_entries[run_id]["targeted_pr_number"] = target_pr_number
            runs_entries[run_id]["rollout_mode"] = LiveRolloutMode.SHADOW.value

            open_prs = self._find_open_prs()
            open_numbers = {p["number"] for p in open_prs}
            if target_pr_number is None:
                targeting_outcome = "no-target-pr"
            elif open_prs and target_pr_number not in open_numbers:
                targeting_outcome = f"target-pr-{target_pr_number}-not-open"
            else:
                targeting_outcome = f"target-pr-{target_pr_number}-would-post"

            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "autonomous-review-shadow-published",
                    "run_id": run_id,
                    "provider": "github",
                    "details": {
                        "pr": target_pr_number,
                        "rollout_mode": LiveRolloutMode.SHADOW.value,
                        "summary_length": len(summary_text),
                        "targeting_outcome": targeting_outcome,
                        "open_prs": list(open_numbers) if open_prs else [],
                        "action": "shadow-would-have-published",
                    },
                },
            )
            return None

        if target_pr_number is None:
            runs_entries[run_id]["status"] = PublishStatus.FAILED.value
            runs_entries[run_id]["error"] = f"target-refused:{resolution_reason}"
            runs_entries[run_id]["targeted_pr_number"] = None
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "autonomous-review-publish-refused",
                    "run_id": run_id,
                    "provider": "github",
                    "details": {
                        "reason": resolution_reason,
                        "action": "stayed-local-only",
                    },
                },
            )
            return None

        open_prs = self._find_open_prs()
        open_numbers = {p["number"] for p in open_prs}
        if open_prs and target_pr_number not in open_numbers:
            runs_entries[run_id]["status"] = PublishStatus.FAILED.value
            runs_entries[run_id]["error"] = f"target-pr-{target_pr_number}-not-open"
            runs_entries[run_id]["targeted_pr_number"] = None
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "autonomous-review-publish-refused",
                    "run_id": run_id,
                    "provider": "github",
                    "details": {
                        "reason": f"target-pr-{target_pr_number}-not-open",
                        "targeted_pr": target_pr_number,
                        "open_prs": list(open_numbers),
                        "action": "stayed-local-only",
                    },
                },
            )
            return None

        owner = (
            self.repo.config.github.get("owner") or self.repo.config.name.split("/")[0]
        )
        repo_name = (
            self.repo.config.github.get("repo") or self.repo.config.name.split("/")[1]
        )

        base_delay_seconds = min(
            float(self.repo.config.review_care.get("retry_delay_minutes", 15)) * 60.0,
            4.0,
        )
        max_retries = 2

        def _is_transient_failure(returncode: int, stderr: str, stdout: str) -> bool:
            if returncode == 124:
                return True
            combined = (stderr + stdout).lower()
            if any(
                kw in combined
                for kw in [
                    "rate limit",
                    "rate_limit",
                    "429",
                    "too many requests",
                    "secondary rate limit",
                    "abuse rate limit",
                ]
            ):
                return True
            if any(
                kw in combined
                for kw in [
                    "connection reset",
                    "connection refused",
                    "name or service not known",
                    "network is unreachable",
                    "temporary failure in name resolution",
                    "could not resolve host",
                    "ssl",
                    "tls",
                ]
            ):
                return True
            return False

        last_err = ""
        published_url: Optional[str] = None
        try:
            for attempt in range(max_retries + 1):
                try:
                    result = subprocess.run(
                        [
                            "gh",
                            "pr",
                            "comment",
                            str(target_pr_number),
                            "--repo",
                            f"{owner}/{repo_name}",
                            "--body",
                            summary_text,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                except Exception as exc:
                    last_err = str(exc)
                    if attempt < max_retries:
                        delay = base_delay_seconds * (2**attempt)
                        self.state.append_review_event(
                            self.repo.config.name,
                            {
                                "event": "autonomous-review-publish-retry",
                                "run_id": run_id,
                                "provider": "github",
                                "details": {
                                    "pr": target_pr_number,
                                    "attempt": attempt + 1,
                                    "max_retries": max_retries + 1,
                                    "delay_seconds": delay,
                                    "error": last_err,
                                },
                            },
                        )

                        _time.sleep(delay)
                        continue
                    last_err = f"gh-call-exception:{last_err}"
                    if run_id in runs_entries:
                        runs_entries[run_id]["status"] = PublishStatus.FAILED.value
                        runs_entries[run_id]["error"] = last_err
                        runs_entries[run_id]["targeted_pr_number"] = target_pr_number
                    published_url = None
                    break

                if result.returncode == 0:
                    published_url = result.stdout.strip()
                    if run_id in runs_entries:
                        runs_entries[run_id]["status"] = PublishStatus.PUBLISHED.value
                        runs_entries[run_id]["comment_url"] = published_url
                        runs_entries[run_id]["targeted_pr_number"] = target_pr_number
                    self.state.append_review_event(
                        self.repo.config.name,
                        {
                            "event": "autonomous-review-published",
                            "run_id": run_id,
                            "provider": "github",
                            "details": {
                                "pr": target_pr_number,
                                "comment_url": published_url,
                                "resolution": resolution_reason,
                            },
                        },
                    )
                    break

                last_err = (
                    result.stderr.strip()
                    or f"gh-pr-comment-failed (code {result.returncode})"
                )
                if attempt < max_retries and _is_transient_failure(
                    result.returncode, last_err, result.stdout.strip()
                ):
                    delay = base_delay_seconds * (2**attempt)
                    self.state.append_review_event(
                        self.repo.config.name,
                        {
                            "event": "autonomous-review-publish-retry",
                            "run_id": run_id,
                            "provider": "github",
                            "details": {
                                "pr": target_pr_number,
                                "attempt": attempt + 1,
                                "max_retries": max_retries + 1,
                                "delay_seconds": delay,
                                "error": last_err,
                            },
                        },
                    )

                    _time.sleep(delay)
                    continue

                if run_id in runs_entries:
                    runs_entries[run_id]["status"] = PublishStatus.FAILED.value
                    runs_entries[run_id]["error"] = last_err
                    runs_entries[run_id]["targeted_pr_number"] = target_pr_number
                self.state.append_review_event(
                    self.repo.config.name,
                    {
                        "event": "autonomous-review-publish-failed",
                        "run_id": run_id,
                        "provider": "github",
                        "details": {
                            "pr": target_pr_number,
                            "error": last_err,
                            "retries_exhausted": attempt >= max_retries,
                        },
                    },
                )
                published_url = None
                break
        except Exception as exc:
            last_err = str(exc)
            if run_id in runs_entries:
                runs_entries[run_id]["status"] = PublishStatus.FAILED.value
                runs_entries[run_id]["error"] = last_err
                runs_entries[run_id]["targeted_pr_number"] = target_pr_number
            published_url = None

        return published_url

    def _resolve_pr_context_for_autonomous_run(
        self,
        prior_publish: Dict[str, Any],
    ) -> Tuple[Optional[int], str]:
        configured_pr = self.repo.config.github.get("pr_number")
        if configured_pr is not None:
            try:
                pr_num = int(configured_pr)
                return (pr_num, f"explicit-config-pr-{pr_num}")
            except (TypeError, ValueError):
                pass

        if self.repo.config.github.get("live_actions", False):
            pr_num, reason = self._resolve_target_pr_for_run(prior_publish)
            if pr_num is not None:
                return (pr_num, f"resolved-{reason}")
            return (None, reason)

        return (None, "live-actions-disabled")
