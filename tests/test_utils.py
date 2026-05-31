"""Tests for bluei.engine.utils — lesson loading, rotation, command helpers."""

from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from bluei.engine.utils import (
    run_no_capture,
    sanitize_command_template,
    _rotate_lessons_if_needed,
    _entry_older_than,
    _lesson_age_days,
    append_lesson,
    load_lessons_for_finding,
    load_lessons_for_rule,
    load_failure_clusters_for_rule,
    assert_safe_repo,
    branch_suffix,
)


# --- run_no_capture ---


def test_run_no_capture_success(tmp_path):
    rc = run_no_capture(["echo", "hello"], tmp_path)
    assert rc == 0


def test_run_no_capture_failure(tmp_path):
    rc = run_no_capture(["false"], tmp_path)
    assert rc != 0


# --- sanitize_command_template ---


def test_sanitize_command_template_truncation():
    long_str = "a " * 600
    result = sanitize_command_template(long_str)
    assert result.endswith("...<truncated>")
    assert len(result) < len(long_str)


def test_sanitize_command_template_normal():
    result = sanitize_command_template("echo hello")
    assert result == "echo hello"


# --- lesson rotation edge cases ---


def test_rotate_lessons_nonexistent_file(tmp_path):
    result = _rotate_lessons_if_needed(tmp_path / "no_such_file.md")
    assert result is None


def test_rotate_lessons_oserror_suppressed(tmp_path):
    lessons_file = tmp_path / "lessons.md"
    lessons_file.write_text("## 2026-01-01 | test\n- **Broke:** stuff\n")
    with patch("pathlib.Path.read_text", side_effect=OSError("permission denied")):
        _rotate_lessons_if_needed(lessons_file)


# --- _entry_older_than ---


def test_entry_older_than_whitespace_only():
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _entry_older_than("   \n  \n  ", cutoff) is False


def test_entry_older_than_invalid_date():
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _entry_older_than("not-a-date | test", cutoff) is False


# --- _lesson_age_days ---


def test_lesson_age_days_empty():
    assert _lesson_age_days("") is None


def test_lesson_age_days_invalid():
    assert _lesson_age_days("not-a-date") is None


def test_lesson_age_days_valid_recent():
    today = datetime.now().strftime("%Y-%m-%d")
    result = _lesson_age_days(today)
    assert result is not None
    assert result == 0


# --- load_lessons_for_finding decay branches ---


def test_load_lessons_finding_decay_unparseable_date(tmp_path):
    lessons_file = tmp_path / "lessons.md"
    lessons_file.write_text(
        "## not-a-date | test\nfinding_id: fid-x\n- **Broke:** unparseable date entry\n"
    )
    result = load_lessons_for_finding("fid-x", lessons_file)
    assert len(result) == 1
    assert result[0]["decayed"] is True


def test_load_lessons_finding_decay_old_excluded(tmp_path):
    lessons_file = tmp_path / "lessons.md"
    old_date = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    lessons_file.write_text(
        f"## {old_date} | test\nfinding_id: fid-old\n- **Broke:** too old entry\n"
    )
    result = load_lessons_for_finding("fid-old", lessons_file)
    assert len(result) == 0


def test_load_lessons_finding_decay_warn_flagged(tmp_path):
    lessons_file = tmp_path / "lessons.md"
    warn_date = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d")
    lessons_file.write_text(
        f"## {warn_date} | test\nfinding_id: fid-warn\n- **Broke:** aging entry\n"
    )
    result = load_lessons_for_finding("fid-warn", lessons_file)
    assert len(result) == 1
    assert result[0]["decayed"] is True


def test_load_lessons_finding_decay_fresh_not_flagged(tmp_path):
    lessons_file = tmp_path / "lessons.md"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lessons_file.write_text(
        f"## {today} | test\nfinding_id: fid-fresh\n- **Broke:** fresh entry\n"
    )
    result = load_lessons_for_finding("fid-fresh", lessons_file)
    assert len(result) == 1
    assert result[0]["decayed"] is False


def test_load_lessons_finding_mixed_decay(tmp_path):
    lessons_file = tmp_path / "lessons.md"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    old = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    warn = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d")
    content = (
        f"## bad-date | test\nfinding_id: fid-mix\n- **Broke:** unparseable\n\n"
        f"## {old} | test\nfinding_id: fid-mix\n- **Broke:** too old\n\n"
        f"## {warn} | test\nfinding_id: fid-mix\n- **Broke:** aging\n\n"
        f"## {today} | test\nfinding_id: fid-mix\n- **Broke:** fresh\n"
    )
    lessons_file.write_text(content)
    result = load_lessons_for_finding("fid-mix", lessons_file)
    assert len(result) == 3
    dates = [e["date"] for e in result]
    assert today in dates
    assert warn in dates
    assert "bad-date" in dates


# --- load_lessons_for_rule ---


def test_load_lessons_rule_worked_field_parsed(tmp_path):
    lessons_file = tmp_path / "lessons.md"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lessons_file.write_text(
        f"## {today} | test\n"
        f"- **Broke:** my-rule broke here\n"
        f"- **Worked:** my-rule was fixed by X\n"
    )
    result = load_lessons_for_rule("my-rule", lessons_file)
    assert len(result) == 1
    assert "my-rule was fixed by X" in result[0]["worked"]


def test_load_lessons_rule_decay_none_date_flagged(tmp_path):
    lessons_file = tmp_path / "lessons.md"
    lessons_file.write_text("## not-a-date | test\n- **Broke:** target-rule issue\n")
    result = load_lessons_for_rule("target-rule", lessons_file)
    assert len(result) == 1
    assert result[0]["decayed"] is True


# --- load_failure_clusters_for_rule ---


def test_failure_clusters_path_no_regex_match(tmp_path):
    lessons_file = tmp_path / "lessons.md"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entries = ""
    for i in range(3):
        entries += (
            f"\n## {today} | test\n"
            f"- **Broke:** path=src/foo.py my-rule-abc happened here\n"
        )
    lessons_file.write_text(entries)
    result = load_failure_clusters_for_rule(
        "my-rule-abc",
        lessons_file,
        min_count=3,
        min_ratio=0.9,
    )
    assert result is not None
    assert "my-rule-abc" in result


def test_failure_clusters_colon_split_three_parts(tmp_path):
    lessons_file = tmp_path / "lessons.md"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entries = ""
    for i in range(3):
        entries += (
            f"\n## {today} | test\n- **Broke:** xyz-rule: src/app.py: some_error_msg\n"
        )
    lessons_file.write_text(entries)
    result = load_failure_clusters_for_rule(
        "xyz-rule",
        lessons_file,
        min_count=3,
        min_ratio=0.9,
    )
    assert result is not None
    assert "some_error_msg" in result


def test_failure_clusters_colon_split_two_parts(tmp_path):
    lessons_file = tmp_path / "lessons.md"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entries = ""
    for i in range(3):
        entries += f"\n## {today} | test\n- **Broke:** ab-rule: just_two_parts\n"
    lessons_file.write_text(entries)
    result = load_failure_clusters_for_rule(
        "ab-rule",
        lessons_file,
        min_count=3,
        min_ratio=0.9,
    )
    assert result is not None
    assert "ab-rule" in result


def test_failure_clusters_plain_broke_text(tmp_path):
    lessons_file = tmp_path / "lessons.md"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entries = ""
    for i in range(3):
        entries += f"\n## {today} | test\n- **Broke:** plain-rule plain error text\n"
    lessons_file.write_text(entries)
    result = load_failure_clusters_for_rule(
        "plain-rule",
        lessons_file,
        min_count=3,
        min_ratio=0.9,
    )
    assert result is not None
    assert "plain error text" in result


def test_failure_clusters_too_few_failures_returns_none(tmp_path):
    lessons_file = tmp_path / "lessons.md"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lessons_file.write_text(
        f"\n## {today} | test\n- **Broke:** rare-rule only one failure\n"
    )
    result = load_failure_clusters_for_rule("rare-rule", lessons_file, min_count=3)
    assert result is None


# --- assert_safe_repo ---


def test_assert_safe_repo(tmp_path):
    assert_safe_repo(tmp_path)


# --- branch_suffix ---


def test_branch_suffix_all_special_chars():
    assert branch_suffix("---") == "finding"


def test_branch_suffix_empty_string():
    assert branch_suffix("") == "finding"


def test_branch_suffix_truncates_to_32():
    long_val = "a" * 50
    result = branch_suffix(long_val)
    assert len(result) == 32
    assert result == "a" * 32


def test_branch_suffix_normal():
    assert branch_suffix("Hello World 123") == "hello-world-123"
