"""The only newly supported bare rule requires its actual producing tool."""

import pytest

from bluei.engine.validation import responsible_detector_ran


@pytest.mark.parametrize("producer", [
    {}, {"status": "ran"}, {"status": "skipped", "tool_outcome": "tool-ran"},
    {"status": "failed", "tool_outcome": "tool-ran"},
    {"status": "unavailable", "tool_outcome": "tool-missing"},
    {"status": "ran", "tool_outcome": "tool-error"},
    {"status": "ran", "tool_outcome": "unknown", "fallback_findings": 1},
])
def test_no_console_cannot_borrow_missing_or_fallback_tool_evidence(producer):
    assert not responsible_detector_ran("no-console", {"plugin:plugin-typescript": producer})


def test_only_exact_no_console_uses_the_typescript_tool_producer():
    outcomes = {"plugin:plugin-typescript": {"status": "ran", "tool_outcome": "tool-ran"}}
    assert responsible_detector_ran("no-console", outcomes)
    for rule in ("no-alert", "NO-CONSOLE", "er-eslint-no-console", "other-no-console"):
        assert not responsible_detector_ran(rule, outcomes)
    assert not responsible_detector_ran("no-console", {"xo-linter": {"status": "ran", "tool_outcome": "tool-ran"}})
