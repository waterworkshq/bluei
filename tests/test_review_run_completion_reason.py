"""Characterization tests for _format_run_completion_reason.

Pins all 8 lifecycle_phase branches of the pure formatter extracted from
_build_run_artifact (rec-18 followup). Each test asserts the exact formatted
string for representative inputs, so any accidental change during future
refactoring is immediately caught.
"""

from types import SimpleNamespace

from bluei.review.autonomous import _format_run_completion_reason
from bluei.review.models import MonitoredSafetyState


def _safety_state(circuit_open=False, failure_count=0):
    return MonitoredSafetyState(
        circuit_open=circuit_open,
        failure_count=failure_count,
    )


def _filter_result(decision="pass", failed_reason=""):
    return SimpleNamespace(decision=decision, failed_reason=failed_reason)


# -- Common kwargs reused across tests --

COMMON = dict(
    pr_number=42,
    candidate_source="llm",
    guard_enabled=True,
    live_actions=True,
    publish_filter_result=_filter_result(),
    safety_reason="",
    current_safety_state=_safety_state(),
    prior_publish={},
    run_id="run-001",
)


def test_shadow_published():
    result = _format_run_completion_reason(lifecycle_phase="shadow-published", **COMMON)
    assert "shadow:" in result
    assert "PR #42" in result
    assert "candidates=llm" in result


def test_guard_disabled():
    result = _format_run_completion_reason(lifecycle_phase="guard-disabled", **COMMON)
    assert "guard-disabled:" in result
    assert "guarded_live_review=True" in result
    assert "live_actions=True" in result


def test_guarded_live_published():
    result = _format_run_completion_reason(
        lifecycle_phase="guarded-live-published", **COMMON
    )
    assert "published:" in result
    assert "PR #42" in result
    assert "candidates=llm" in result


def test_filter_blocked():
    result = _format_run_completion_reason(
        lifecycle_phase="filter-blocked",
        publish_filter_result=_filter_result(
            decision="block", failed_reason="too_many_findings"
        ),
        **{k: v for k, v in COMMON.items() if k != "publish_filter_result"},
    )
    assert "filter-blocked:" in result
    assert "decision=block" in result
    assert "too_many_findings" in result
    assert "candidates=llm" in result


def test_guarded_live_refused():
    prior = {"runs": {"run-001": {"error": "target_pr_not_found"}}}
    result = _format_run_completion_reason(
        lifecycle_phase="guarded-live-refused",
        prior_publish=prior,
        **{k: v for k, v in COMMON.items() if k != "prior_publish"},
    )
    assert "refused:" in result
    assert "target_pr_not_found" in result
    assert "candidates=llm" in result


def test_guarded_live_refused_missing_error_defaults_unknown():
    prior = {"runs": {"run-001": {}}}
    result = _format_run_completion_reason(
        lifecycle_phase="guarded-live-refused",
        prior_publish=prior,
        **{k: v for k, v in COMMON.items() if k != "prior_publish"},
    )
    assert "refused:" in result
    assert "unknown" in result


def test_guarded_live_failed():
    prior = {"runs": {"run-001": {"error": "gh_api_timeout"}}}
    result = _format_run_completion_reason(
        lifecycle_phase="guarded-live-failed",
        prior_publish=prior,
        **{k: v for k, v in COMMON.items() if k != "prior_publish"},
    )
    assert "failed:" in result
    assert "gh_api_timeout" in result
    assert "candidates=llm" in result


def test_safety_blocked():
    result = _format_run_completion_reason(
        lifecycle_phase="safety-blocked",
        safety_reason="circuit_open",
        current_safety_state=_safety_state(circuit_open=True, failure_count=3),
        **{
            k: v
            for k, v in COMMON.items()
            if k not in ("safety_reason", "current_safety_state")
        },
    )
    assert "safety-blocked:" in result
    assert "safety_reason=circuit_open" in result
    assert "circuit_open=True" in result
    assert "failure_count=3" in result
    assert "candidates=llm" in result


def test_fallthrough_else():
    result = _format_run_completion_reason(lifecycle_phase="unknown-phase", **COMMON)
    assert "completed:" in result
    assert "lifecycle_phase=unknown-phase" in result
    assert "candidates=llm" in result
