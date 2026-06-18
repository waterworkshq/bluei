"""Observation — extracted from review.cycle for single-responsibility.

Observation cycle: read-only PR scanning, comment classification, summary posting.
"""

from __future__ import annotations

import json
import logging
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

_logger = logging.getLogger(__name__)

from bluei.common.models import now_iso
from bluei.review.types import ReviewCycleResult

# Proactive inter-PR throttle. Default 0 keeps existing behavior; opt in via
# review_care.inter_pr_delay_seconds to space out sequential API calls and
# reduce the chance of tripping GitHub's secondary rate limit on PR-heavy repos.
_DEFAULT_INTER_PR_DELAY_SECONDS = 0.0


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


# Methods extracted to focused mixins (rec-16 followup):
# - status_classification.py: _classify_pr_status, _handle_retry_remediation
# - state_recording.py: _record_pr_in_active_state, _record_pr_in_review_state,
#   _append_pr_review_event, _persist_active_and_review_state
# - pr_targeting.py: _find_open_prs, _resolve_target_pr_for_run
# - publication_pipeline.py: _post_summary_to_github,
#   _resolve_pr_context_for_autonomous_run
