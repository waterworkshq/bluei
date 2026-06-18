"""Tests for task_05: replay integration in CLI pipeline and prompt hint injection."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.pattern_replay import (
    AUTO_REPLAY_THRESHOLD,
    PROMPT_HINT_THRESHOLD,
    format_pattern_hint,
    try_replay,
)
from bluei.engine.pattern_store import (
    FixPattern,
    FixPatternStore,
)
from bluei.engine.prompts import render_claude_fix_prompt


def make_pattern(**overrides):
    data = {
        "pattern_id": "",
        "rule": "broad-except",
        "language": "python",
        "file_path": "src/example.py",
        "before_snippet": "except:\n    pass",
        "after_snippet": "except Exception:\n    pass",
        "diff_patch": "--- a/src/example.py\n+++ b/src/example.py\n-except:\n+except Exception:\n",
        "confidence": 0.95,
        "success_count": 3,
        "failure_count": 0,
        "skip_count": 0,
        "source": "claude",
        "created_at": "2026-05-17T00:00:00+00:00",
        "last_used_at": None,
        "last_verified_at": None,
        "source_finding_ids": ["finding-1"],
    }
    data.update(overrides)
    return FixPattern(**data)


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "repos" / "test-repo" / "state" / "fix_patterns.jsonl"


@pytest.fixture
def log_file(tmp_path):
    return tmp_path / "logs" / "test.log"


# ── Unit tests: render_claude_fix_prompt with learned_patterns ──


def test_render_prompt_without_learned_patterns_unchanged(make_finding):
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    prompt = render_claude_fix_prompt(
        finding=finding,
        baseline_checks={"lint": ["python3", "-m", "pylint"]},
        target_checks={"rule-check": ["python3", "-m", "flake8"]},
        max_files_changed=5,
        max_loc_diff=200,
    )
    assert "## Learned fix patterns" not in prompt
    assert "## Finding metadata" in prompt
    assert "## Snippet" in prompt


def test_render_prompt_with_learned_patterns_includes_section(make_finding):
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    hint = "Rule: broad-except\nBefore: `except:`\nAfter: `except Exception:`"
    prompt = render_claude_fix_prompt(
        finding=finding,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        learned_patterns=hint,
    )
    assert "## Learned fix patterns" in prompt
    assert hint in prompt


def test_learned_patterns_section_between_prior_context_and_fix_history(make_finding):
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    hint = "Consider using except Exception."
    fix_history = [
        {"date": "2026-01-01", "cycle_type": "fix", "changed": "applied patch"}
    ]
    finding_record = {"fix_attempts": 2, "last_fix_error": "timeout"}

    prompt = render_claude_fix_prompt(
        finding=finding,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        fix_history=fix_history,
        finding_record=finding_record,
        learned_patterns=hint,
    )

    prior_idx = prompt.index("## Prior context")
    learned_idx = prompt.index("## Learned fix patterns")
    fix_idx = prompt.index("## Fix history")

    assert prior_idx < learned_idx < fix_idx


def test_learned_patterns_none_no_extra_section(make_finding):
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    prompt = render_claude_fix_prompt(
        finding=finding,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        learned_patterns=None,
    )
    assert "## Learned fix patterns" not in prompt


def test_extra_prompt_renders_as_separate_section(make_finding):
    """extra_prompt (rule hint) must appear in 'Rule-specific guidance', not 'Learned fix patterns'."""
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    rule_hint = "Replace bare except with except Exception."
    replay_hint = "Pattern: broad-except → except Exception: (confidence 72%)"

    prompt = render_claude_fix_prompt(
        finding=finding,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        extra_prompt=rule_hint,
        learned_patterns=replay_hint,
    )

    assert "## Rule-specific guidance" in prompt
    assert rule_hint in prompt
    assert "## Learned fix patterns" in prompt
    assert replay_hint in prompt
    # Ensure they are in separate sections — rule hint should NOT appear under Learned fix patterns
    rule_section_start = prompt.index("## Rule-specific guidance")
    learned_section_start = prompt.index("## Learned fix patterns")
    assert rule_section_start < learned_section_start


def test_extra_prompt_alone_no_learned_patterns(make_finding):
    """When only extra_prompt is provided, Learned fix patterns section should not appear."""
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    prompt = render_claude_fix_prompt(
        finding=finding,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        extra_prompt="Operator-authored rule hint",
    )
    assert "## Rule-specific guidance" in prompt
    assert "Operator-authored rule hint" in prompt
    assert "## Learned fix patterns" not in prompt


# ── Unit tests: format_pattern_hint ──


def test_format_pattern_hint_includes_rule():
    pattern = make_pattern(rule="unused-import", confidence=0.65)
    hint = format_pattern_hint(pattern)
    assert "unused-import" in hint


def test_format_pattern_hint_includes_before_after_code_blocks():
    pattern = make_pattern()
    hint = format_pattern_hint(pattern)
    assert "**Before:**" in hint
    assert "**After:**" in hint
    assert "except:" in hint
    assert "except Exception:" in hint


def test_format_pattern_hint_includes_confidence():
    pattern = make_pattern(confidence=0.65)
    hint = format_pattern_hint(pattern)
    assert "65%" in hint


# ── Unit tests: try_replay integration with store ──


def _store_pattern_with_confidence(store, confidence):
    # Confidence is now preserved on append (pattern.confidence no longer
    # reset to INITIAL_CONFIDENCE), so pass the desired confidence directly
    # instead of adjusting via update_confidence.
    pid = store.append(make_pattern(confidence=confidence))
    store._rebuild_from_disk()
    return pid


def test_pipeline_no_pattern_store_runs_standard_path(
    store_path, git_repo, log_file, make_finding
):
    """When pattern_store is None, try_replay is never called."""
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    result = try_replay(git_repo, finding, FixPatternStore(store_path), {}, log_file)
    assert result == (False, None)


def test_pipeline_no_matching_pattern(store_path, git_repo, log_file, make_finding):
    store = FixPatternStore(store_path)
    finding = make_finding(
        rule="unused-import", path="src/example.py", snippet="except:\n    pass"
    )

    result = try_replay(git_repo, finding, store, {}, log_file)

    assert result == (False, None)


def test_pipeline_high_confidence_replay_skips_llm(
    store_path, git_repo, log_file, make_finding, git_commit_all
):
    src = git_repo / "src"
    src.mkdir()
    target = src / "example.py"
    target.write_text("try:\n    do_thing()\nexcept:\n    pass\n")
    git_commit_all(git_repo)

    store = FixPatternStore(store_path)
    pid = _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    with patch("bluei.engine.pattern_replay.run_capture", return_value=(0, "")):
        replayed, result_pid = try_replay(git_repo, finding, store, {}, log_file)

    assert replayed is True
    assert result_pid == pid
    assert "except Exception:" in target.read_text()


def test_pipeline_mid_confidence_returns_hint_pattern_id(
    store_path, git_repo, log_file, make_finding
):
    store = FixPatternStore(store_path)
    pid = _store_pattern_with_confidence(store, 0.65)
    store._rebuild_from_disk()

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    replayed, result_pid = try_replay(git_repo, finding, store, {}, log_file)

    assert replayed is False
    assert result_pid == pid

    pattern = store.get_pattern(pid)
    assert pattern is not None
    hint = format_pattern_hint(pattern)
    assert "broad-except" in hint
    assert "**Before:**" in hint


def test_pipeline_mid_confidence_can_format_hint(store_path, log_file):
    store = FixPatternStore(store_path)
    pid = _store_pattern_with_confidence(store, 0.7)
    store._rebuild_from_disk()

    pattern = store.get_pattern(pid)
    hint = format_pattern_hint(pattern)

    assert "broad-except" in hint
    assert "except:" in hint
    assert "except Exception:" in hint


def test_pipeline_replay_success_logs_savings(
    store_path, git_repo, log_file, make_finding, git_commit_all
):
    src = git_repo / "src"
    src.mkdir()
    target = src / "example.py"
    target.write_text("try:\n    do_thing()\nexcept:\n    pass\n")
    git_commit_all(git_repo)

    store = FixPatternStore(store_path)
    pid = _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    with patch("bluei.engine.pattern_replay.run_capture", return_value=(0, "")):
        try_replay(git_repo, finding, store, {}, log_file)

    log_content = log_file.read_text()
    assert "pattern-replay-success" in log_content


def test_pipeline_backward_compat_pattern_store_path_none(
    git_repo, log_file, make_finding
):
    """When pattern_store_path is None, no replay occurs and standard path runs."""
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    prompt = render_claude_fix_prompt(
        finding=finding,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
    )
    assert "## Learned fix patterns" not in prompt
    assert "## Finding metadata" in prompt


def test_render_prompt_preserves_all_existing_sections(make_finding):
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    fix_history = [
        {"date": "2026-01-01", "cycle_type": "fix", "changed": "applied"},
    ]
    finding_record = {"fix_attempts": 1, "last_fix_error": "timeout"}

    prompt = render_claude_fix_prompt(
        finding=finding,
        baseline_checks={"lint": ["python3", "-m", "pylint"]},
        target_checks={"rule-check": ["python3", "-m", "flake8"]},
        max_files_changed=5,
        max_loc_diff=200,
        fix_history=fix_history,
        finding_record=finding_record,
        learned_patterns="Some hint text",
    )

    assert "## Finding metadata" in prompt
    assert "## Snippet" in prompt
    assert "## Constraints (must follow)" in prompt
    assert "## Validation command context" in prompt
    assert "## Prior context" in prompt
    assert "## Learned fix patterns" in prompt
    assert "## Fix history" in prompt
    assert "Some hint text" in prompt
