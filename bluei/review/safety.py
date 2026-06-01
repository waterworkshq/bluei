"""Safety — extracted from review.cycle for single-responsibility.

Guard checks, circuit breaker, publish filters, monitored safety state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

from bluei.app.models import LiveRolloutMode
from bluei.review.models import (
    MonitoredSafetyState,
    normalize_finding_header,
    FindingSeverity,
)
from bluei.review.types import PublishFilterResult


class SafetyMixin:
    def _is_guarded_live_review_enabled(self) -> Tuple[bool, str]:
        guarded_flag = bool(
            self.repo.config.review_care.get("guarded_live_review", False)
        )
        live_actions = bool(self.repo.config.github.get("live_actions", False))

        if guarded_flag and live_actions:
            return (True, "guard-passed")
        if guarded_flag and not live_actions:
            return (False, "guard-failed-live-actions-disabled")
        if not guarded_flag and live_actions:
            return (False, "guard-failed-guarded-live-review-disabled")
        return (False, "guard-failed-both-disabled")

    def _get_live_rollout_mode(self) -> Tuple[LiveRolloutMode, str]:
        raw = self.repo.config.review_care.get(
            "live_rollout_mode", LiveRolloutMode.LOCAL_ONLY.value
        )
        live_actions = bool(self.repo.config.github.get("live_actions", False))

        try:
            mode = LiveRolloutMode(raw)
        except ValueError:
            return (
                LiveRolloutMode.LOCAL_ONLY,
                f"unknown-live-rollout-mode:{raw}-fallback-to-local-only",
            )

        if (
            mode in (LiveRolloutMode.SHADOW, LiveRolloutMode.LIMITED)
            and not live_actions
        ):
            return (
                LiveRolloutMode.LOCAL_ONLY,
                f"rollout-{mode.value}-requires-live-actions-false-falling-back-to-local-only",
            )

        if mode == LiveRolloutMode.SHADOW:
            return (LiveRolloutMode.SHADOW, "shadow-mode-active")

        if mode == LiveRolloutMode.LIMITED:
            guarded_enabled, guard_reason = self._is_guarded_live_review_enabled()
            if guarded_enabled:
                return (LiveRolloutMode.LIMITED, "limited-mode-guard-passed")
            else:
                return (
                    LiveRolloutMode.LOCAL_ONLY,
                    f"limited-mode-guard-failed-{guard_reason}-fallback-to-local-only",
                )

        return (LiveRolloutMode.LOCAL_ONLY, "local-only-default")

    def _emit_guard_event(
        self,
        run_id: str,
        pr_number: Optional[int],
        guard_enabled: bool,
        guard_reason: str,
    ) -> None:
        event_name = (
            "autonomous-review-guard-passed"
            if guard_enabled
            else "autonomous-review-guard-blocked"
        )
        self.state.append_review_event(
            self.repo.config.name,
            {
                "event": event_name,
                "run_id": run_id,
                "provider": "local-stub",
                "details": {
                    "guard_enabled": guard_enabled,
                    "guard_reason": guard_reason,
                    "targeted_pr": pr_number,
                    "live_actions": self.repo.config.github.get("live_actions", False),
                    "guarded_live_review": self.repo.config.review_care.get(
                        "guarded_live_review", False
                    ),
                },
            },
        )

    def _check_limited_publish_filters(
        self,
        findings_total: int,
        findings_list: List[Dict[str, Any]],
        pr_context: Optional[Dict[str, Any]],
    ) -> "PublishFilterResult":
        max_findings = int(
            self.repo.config.review_care.get("limited_max_findings_count", 10)
        )
        if findings_total > max_findings:
            return PublishFilterResult(
                passed=False,
                decision="fail",
                failed_reason=(
                    f"findings_count={findings_total} exceeds "
                    f"limited_max_findings_count={max_findings}"
                ),
            )

        require_pr = bool(
            self.repo.config.review_care.get("limited_require_pr_context", True)
        )
        if require_pr and pr_context is None:
            return PublishFilterResult(
                passed=False,
                decision="fail",
                failed_reason="pr_context required but not available",
            )
        if (
            require_pr
            and pr_context is not None
            and pr_context.get("pr_number") is None
        ):
            return PublishFilterResult(
                passed=False,
                decision="fail",
                failed_reason="pr_context present but pr_number is None",
            )

        allowed_severities_raw = self.repo.config.review_care.get(
            "limited_allowed_severities", None
        )
        if allowed_severities_raw is not None:
            allowed_severities = {s.lower().strip() for s in allowed_severities_raw}
            failing_severities: List[str] = []
            for f in findings_list:
                severity_val = f.get("severity")
                if isinstance(severity_val, FindingSeverity):
                    sev_str = severity_val.value
                elif isinstance(severity_val, str):
                    sev_str = severity_val.lower().strip()
                else:
                    sev_str = "unknown"
                if sev_str not in allowed_severities:
                    failing_severities.append(sev_str)
            if failing_severities:
                unique_failing = sorted(set(failing_severities))
                return PublishFilterResult(
                    passed=False,
                    decision="fail",
                    failed_reason=(
                        f"severity subset check failed: "
                        f"found [{', '.join(unique_failing)}] "
                        f"but limited_allowed_severities={sorted(allowed_severities)}"
                    ),
                )

        allowed_headers_raw = self.repo.config.review_care.get(
            "limited_allowed_headers", None
        )
        if allowed_headers_raw is not None:
            allowed_headers = {h.lower().strip() for h in allowed_headers_raw}
            failing_headers: List[str] = []
            for f in findings_list:
                header_val = f.get("header", "")
                norm_header = normalize_finding_header(str(header_val))
                if norm_header not in allowed_headers:
                    failing_headers.append(str(header_val))
            if failing_headers:
                unique_failing = sorted(set(failing_headers))
                return PublishFilterResult(
                    passed=False,
                    decision="fail",
                    failed_reason=(
                        f"header subset check failed: "
                        f"found [{', '.join(unique_failing[:5])}"
                        f"{' ...' if len(unique_failing) > 5 else ''}] "
                        f"but limited_allowed_headers={sorted(allowed_headers)}"
                    ),
                )

        return PublishFilterResult(passed=True, decision="pass", failed_reason="")

    def _check_monitored_safety(
        self,
    ) -> Tuple[bool, str, "MonitoredSafetyState"]:
        safety_data = self.state.load_monitored_safety_state(self.repo.config.name)
        safety_state = MonitoredSafetyState.from_dict(safety_data)

        if safety_state.auto_rollback_active:
            return (
                False,
                f"auto-rollback-active-{safety_state.auto_rollback_reason or 'feedback-threshold-exceeded'}",
                safety_state,
            )

        if safety_state.circuit_open and not safety_state.check_cooldown_ready():
            return (
                False,
                f"circuit-open-cooldown-active-until-{safety_state.cooldown_until}",
                safety_state,
            )

        if safety_state.circuit_open:
            safety_state.circuit_open = False
            self.state.save_monitored_safety_state(
                self.repo.config.name, safety_state.to_dict()
            )
            return (
                False,
                f"circuit-closed-cooldown-expired-failure-count-{safety_state.failure_count}",
                safety_state,
            )

        return (True, "circuit-closed", safety_state)

    def _evaluate_feedback_auto_rollback(
        self,
        run_id: str,
    ) -> "MonitoredSafetyState":
        safety_data = self.state.load_monitored_safety_state(self.repo.config.name)
        safety_state = MonitoredSafetyState.from_dict(safety_data)

        if safety_state.auto_rollback_active:
            return safety_state
        if not self.repo.config.review_care.get(
            "monitored_auto_rollback_enabled", False
        ):
            return safety_state

        window = int(self.repo.config.review_care.get("monitored_feedback_window", 20))
        min_events = int(
            self.repo.config.review_care.get("monitored_feedback_min_events", 3)
        )
        threshold = float(
            self.repo.config.review_care.get(
                "monitored_negative_feedback_threshold", 0.3
            )
        )

        feedback_events = self.state.load_feedback_events(self.repo.config.name)
        recent = feedback_events[-window:] if window > 0 else feedback_events
        negative_signals = {"negative", "request_change", "conflict"}
        positive_signals = {"positive", "approve"}

        negatives = 0
        positives = 0
        for event in recent:
            signal = (
                str(event.get("signal") or event.get("sentiment") or "").strip().lower()
            )
            if signal in negative_signals:
                negatives += 1
            elif signal in positive_signals:
                positives += 1

        considered = negatives + positives
        if considered < min_events or considered <= 0:
            return safety_state

        negative_ratio = negatives / considered
        if negative_ratio < threshold:
            return safety_state

        safety_state.auto_rollback_active = True
        safety_state.auto_rollback_reason = (
            f"negative-feedback-ratio-{negative_ratio:.2f}-"
            f"over-threshold-{threshold:.2f}-from-{negatives}-of-{considered}-signals"
        )
        safety_state.auto_rollback_triggered_at = datetime.now(timezone.utc).isoformat()
        safety_state.last_failure_reason = safety_state.auto_rollback_reason
        self.state.save_monitored_safety_state(
            self.repo.config.name, safety_state.to_dict()
        )
        self.state.append_review_event(
            self.repo.config.name,
            {
                "event": "monitored-safety-auto-rollback-activated",
                "run_id": run_id,
                "provider": "local-stub",
                "details": {
                    "negative_signals": negatives,
                    "positive_signals": positives,
                    "considered_signals": considered,
                    "negative_ratio": round(negative_ratio, 4),
                    "threshold": threshold,
                    "window": window,
                    "min_events": min_events,
                    "reason": safety_state.auto_rollback_reason,
                },
            },
        )
        return safety_state

    def _record_publish_failure_for_safety(
        self,
        run_id: str,
        failure_reason: str,
    ) -> "MonitoredSafetyState":
        safety_data = self.state.load_monitored_safety_state(self.repo.config.name)
        safety_state = MonitoredSafetyState.from_dict(safety_data)

        threshold = int(
            self.repo.config.review_care.get("monitored_failure_threshold", 3)
        )
        cooldown_seconds = int(
            self.repo.config.review_care.get("monitored_cooldown_seconds", 300)
        )

        safety_state.failure_count += 1
        safety_state.last_failure_at = datetime.now(timezone.utc).isoformat()
        safety_state.last_failure_reason = failure_reason

        if safety_state.failure_count >= threshold:
            safety_state.circuit_open = True
            expires = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
            safety_state.cooldown_until = expires.isoformat()
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "monitored-safety-circuit-opened",
                    "run_id": run_id,
                    "provider": "local-stub",
                    "details": {
                        "failure_count": safety_state.failure_count,
                        "threshold": threshold,
                        "cooldown_seconds": cooldown_seconds,
                        "cooldown_until": safety_state.cooldown_until,
                        "last_failure_reason": failure_reason,
                    },
                },
            )
        else:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "monitored-safety-failure-recorded",
                    "run_id": run_id,
                    "provider": "local-stub",
                    "details": {
                        "failure_count": safety_state.failure_count,
                        "threshold": threshold,
                        "last_failure_reason": failure_reason,
                    },
                },
            )

        self.state.save_monitored_safety_state(
            self.repo.config.name, safety_state.to_dict()
        )
        return safety_state

    def _record_publish_success_for_safety(self, run_id: str) -> "MonitoredSafetyState":
        safety_data = self.state.load_monitored_safety_state(self.repo.config.name)
        safety_state = MonitoredSafetyState.from_dict(safety_data)

        if safety_state.failure_count > 0 or safety_state.circuit_open:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "monitored-safety-circuit-reset",
                    "run_id": run_id,
                    "provider": "local-stub",
                    "details": {
                        "prior_failure_count": safety_state.failure_count,
                        "prior_circuit_open": safety_state.circuit_open,
                    },
                },
            )

        safety_state.record_success()
        self.state.save_monitored_safety_state(
            self.repo.config.name, safety_state.to_dict()
        )
        return safety_state
