#!/usr/bin/env python3
"""
test_directive_seeding_e2e.py — Phase 5 End-to-End smoke test.

Validates the full feedback loop for directive-seeding improvements:
  discover → fix attempt → failure → log → extended cooldown → prompt with context.

Uses ONLY temp directories — no live files, no subprocess calls, no network.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

_workdir = os.environ.get("WORKSPACE_ROOT")
if _workdir:
    sys.path.insert(0, _workdir)
else:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from bluei.engine.state import (
    append_findings,
    load_finding_record,
    filter_findings_by_cooldown,
    mark_finding_activity,
    load_state,
    save_state,
    get_effective_cooldown,
    increment_fix_attempt,
    update_finding_record,
)
from bluei.engine.utils import (
    append_lesson,
    load_lessons_for_finding,
)
from bluei.engine.prompts import render_claude_fix_prompt
from bluei.engine.lifecycle import apply_claude_fix
from bluei.engine.constants import DEFAULT_FINDING_COOLDOWN_SECONDS


FINDING_ID = "e2e-phase5-001"
BASE_COOLDOWN = DEFAULT_FINDING_COOLDOWN_SECONDS


@pytest.fixture
def workspace(tmp_path):
    """Shared workspace with findings, lessons, state, and log files."""

    class Ws:
        findings_file = tmp_path / "findings.jsonl"
        lessons_file = tmp_path / "LESSONS_LOG.md"
        state_file = tmp_path / "state.json"
        log_file = tmp_path / "runner.log"

    return Ws()


@pytest.fixture
def seeded_finding(workspace, make_finding):
    """Write a single finding and return it."""
    f = make_finding(
        finding_id=FINDING_ID,
        path="src/test.py",
        line=10,
        rule="ruff-b007",
        snippet="x = 1",
        confidence=0.9,
    )
    append_findings(workspace.findings_file, [f])
    return f


def _set_failure_count(state_file, finding_id, count, last_action_at=None):
    state = load_state(state_file)
    if last_action_at:
        state["finding_activity"][finding_id]["last_action_at"] = last_action_at
    state["finding_activity"][finding_id]["failure_count"] = count
    save_state(state_file, state)


def _backdate_action(state_file, finding_id, hours_ago=5):
    state = load_state(state_file)
    past = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    state["finding_activity"][finding_id]["last_action_at"] = past
    save_state(state_file, state)
    return past


class TestDiscovery:
    def test_finding_written_to_jsonl(self, workspace, make_finding):
        f = make_finding(finding_id=FINDING_ID, path="src/test.py", rule="ruff-b007")
        written = append_findings(workspace.findings_file, [f])
        assert written == 1

        rec = load_finding_record(FINDING_ID, workspace.findings_file)
        assert rec is not None
        assert rec.get("fix_attempts", 0) == 0


class TestFixFailure:
    def test_increment_fix_attempt_updates_record(self, workspace, seeded_finding):
        error_msg = "claude rc=1 output=<simulated failure>"
        increment_fix_attempt(FINDING_ID, workspace.findings_file, error_msg)

        rec = load_finding_record(FINDING_ID, workspace.findings_file)
        assert rec["fix_attempts"] == 1
        assert rec["last_fix_error"] == error_msg
        assert rec["last_fix_at"] is not None

    def test_failure_records_activity_and_lesson(self, workspace, seeded_finding):
        error_msg = "claude rc=1"

        state = load_state(workspace.state_file)
        mark_finding_activity(
            state,
            [FINDING_ID],
            action="fix-attempt",
            failure_count=1,
            last_error=error_msg,
        )
        save_state(workspace.state_file, state)

        append_lesson(
            lessons_file=workspace.lessons_file,
            cycle_type="fix-cycle",
            finding_id=FINDING_ID,
            what_broke="claude rc=1",
        )
        append_lesson(
            lessons_file=workspace.lessons_file,
            cycle_type="fix-cycle",
            finding_id=FINDING_ID,
            what_changed="tried adding noqa comment",
        )

        lessons = load_lessons_for_finding(FINDING_ID, workspace.lessons_file)
        assert len(lessons) >= 1
        for lesson in lessons:
            assert lesson["finding_id"] == FINDING_ID


class TestCooldownFiltering:
    def test_base_cooldown_elapsed_allows_finding(self, workspace, seeded_finding):
        state = load_state(workspace.state_file)
        mark_finding_activity(
            state, [FINDING_ID], action="fix-attempt", failure_count=0
        )
        save_state(workspace.state_file, state)
        _backdate_action(workspace.state_file, FINDING_ID, hours_ago=5)

        state = load_state(workspace.state_file)
        allowed, suppressed = filter_findings_by_cooldown(
            [seeded_finding],
            state,
            cooldown_seconds=BASE_COOLDOWN,
            log_file=workspace.log_file,
        )
        assert len(allowed) == 1
        assert len(suppressed) == 0

    def test_failure_count_2_suppresses_finding(self, workspace, seeded_finding):
        state = load_state(workspace.state_file)
        mark_finding_activity(
            state, [FINDING_ID], action="fix-attempt", failure_count=2
        )
        save_state(workspace.state_file, state)
        past = _backdate_action(workspace.state_file, FINDING_ID, hours_ago=5)

        state = load_state(workspace.state_file)
        effective = get_effective_cooldown(FINDING_ID, state, BASE_COOLDOWN)
        assert effective == BASE_COOLDOWN * (2**2)

        allowed, suppressed = filter_findings_by_cooldown(
            [seeded_finding],
            state,
            cooldown_seconds=BASE_COOLDOWN,
            log_file=workspace.log_file,
        )
        assert len(allowed) == 0
        assert len(suppressed) == 1
        assert suppressed[0].finding_id == FINDING_ID

    def test_reset_failure_count_allows_finding_again(self, workspace, seeded_finding):
        state = load_state(workspace.state_file)
        mark_finding_activity(
            state, [FINDING_ID], action="fix-attempt", failure_count=0
        )
        save_state(workspace.state_file, state)
        _backdate_action(workspace.state_file, FINDING_ID, hours_ago=5)

        state = load_state(workspace.state_file)
        allowed, suppressed = filter_findings_by_cooldown(
            [seeded_finding],
            state,
            cooldown_seconds=BASE_COOLDOWN,
            log_file=workspace.log_file,
        )
        assert len(allowed) == 1
        assert len(suppressed) == 0


class TestPromptContext:
    def test_prompt_includes_fix_history(self, workspace, seeded_finding):
        error_msg = "claude rc=1"
        increment_fix_attempt(FINDING_ID, workspace.findings_file, error_msg)

        append_lesson(
            lessons_file=workspace.lessons_file,
            cycle_type="fix-cycle",
            finding_id=FINDING_ID,
            what_broke="claude rc=1",
        )
        append_lesson(
            lessons_file=workspace.lessons_file,
            cycle_type="fix-cycle",
            finding_id=FINDING_ID,
            what_changed="tried adding noqa comment",
        )

        rec = load_finding_record(FINDING_ID, workspace.findings_file)
        fix_history = load_lessons_for_finding(FINDING_ID, workspace.lessons_file)
        prompt = render_claude_fix_prompt(
            finding=seeded_finding,
            baseline_checks={},
            target_checks={},
            max_files_changed=5,
            max_loc_diff=200,
            fix_history=fix_history,
            finding_record=rec,
        )
        assert "## Prior context" in prompt
        assert "## Fix history" in prompt
        assert "Attempts: 1" in prompt
        assert "claude rc=1" in prompt
        assert "tried adding noqa comment" in prompt


class TestSuccessReset:
    def test_success_resets_failure_count_and_cooldown(self, workspace, seeded_finding):
        state = load_state(workspace.state_file)
        mark_finding_activity(
            state, [FINDING_ID], action="fix-attempt", failure_count=2
        )
        save_state(workspace.state_file, state)

        update_finding_record(
            FINDING_ID, workspace.findings_file, {"fix_success": True}
        )
        state = load_state(workspace.state_file)
        mark_finding_activity(
            state,
            [FINDING_ID],
            action="fix-succeeded",
            failure_count=0,
            last_error=None,
        )
        save_state(workspace.state_file, state)

        effective = get_effective_cooldown(FINDING_ID, state, BASE_COOLDOWN)
        assert effective == BASE_COOLDOWN

        rec = load_finding_record(FINDING_ID, workspace.findings_file)
        assert rec["fix_success"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
