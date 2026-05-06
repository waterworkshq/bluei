#!/usr/bin/env python3
"""Tests for obsidian_sync.py and daily_summary.py."""

import os
import sys
import re
from pathlib import Path
from unittest.mock import patch

import pytest

QA_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QA_ROOT))
sys.path.insert(0, str(QA_ROOT / "scripts"))


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def setup_temp_repos(tmp_path):
    """Create a temp repos dir with a minimal ky-like state."""
    repos = tmp_path / "repos"
    repos.mkdir()

    ky = repos / "ky"
    ky.mkdir()
    (ky / "state").mkdir()
    (ky / "runs").mkdir()

    # status.json has stale counts (open_issues=3, findings_entries=10)
    # but issues.json has 2 open issues and findings.jsonl has 8 entries.
    # This fixture is designed to test that source-of-truth files are used
    # instead of the stale status.json current_counts.
    (ky / "state" / "status.json").write_text(
        '{"current_counts":{"open_issues":3,"open_prs":2,"findings_entries":10,"issue_records_total":3}}'
    )
    (ky / "state" / "issues.json").write_text("""{
        "issues": [
            {"id":"QA-0001","status":"open","rule":"complexity","snippet":"Method too complex",
             "github":{"issue_number":25},"created_at":"2026-03-10T00:00:00Z","updated_at":"2026-03-10T00:00:00Z"},
            {"id":"QA-0002","status":"open","rule":"max-lines","snippet":"File too many lines",
             "github":{"issue_number":20},"created_at":"2026-03-07T00:00:00Z","updated_at":"2026-03-07T00:00:00Z"},
            {"id":"QA-0003","status":"closed","rule":"lint","snippet":"Lint error fixed",
             "github":{"issue_number":30},"created_at":"2026-03-09T00:00:00Z","updated_at":"2026-03-12T00:00:00Z"}
        ]
    }""")
    # findings.jsonl is the source of truth for findings count (8 entries)
    (ky / "state" / "findings.jsonl").write_text(
        '{"finding_id":"f1","rule":"complexity","snippet":"Method too complex","status":"open"}\n'
        '{"finding_id":"f2","rule":"max-lines","snippet":"File too many lines","status":"open"}\n'
        '{"finding_id":"f3","rule":"lint","snippet":"Lint error","status":"open"}\n'
        '{"finding_id":"f4","rule":"type","snippet":"Type error","status":"open"}\n'
        '{"finding_id":"f5","rule":"security","snippet":"Security issue","status":"open"}\n'
        '{"finding_id":"f6","rule":"perf","snippet":"Perf issue","status":"open"}\n'
        '{"finding_id":"f7","rule":"style","snippet":"Style issue","status":"open"}\n'
        '{"finding_id":"f8","rule":"docs","snippet":"Docs issue","status":"open"}\n'
    )
    (ky / "runs" / "run-20260322020001-abc123.json").write_text("""{
        "id":"run-20260322020001-abc123","repo_id":"repo-ky","phase":"issue-cycle",
        "started_at":"2026-03-22T02:00:00+00:00","ended_at":"2026-03-22T02:00:01+00:00",
        "duration_seconds":1,"dry_run":false,"findings_detected":2,"issues_created":1,
        "fix_attempts":0,"fixes_verified":0,"prs_created":0,"merges_completed":0,
        "health_before":90.0,"health_after":92.0,"health_delta":2.0,"status":"completed","error":null
    }""")
    (ky / "state" / "active_prs.json").write_text("""{
        "version":1,"prs":{
            "33":{"pr_number":33,"branch":"qa/live-complexity-xxx","author":"vikasagarwal101","status":"pending_review"},
            "31":{"pr_number":31,"branch":"fix/issue-30-xxx","author":"vikasagarwal101","status":"pending_review"}
        }
    }""")
    (ky / "state" / "health_history.jsonl").write_text(
        '{"timestamp":"2026-03-22T02:00:00+00:00","score":92.0,"findings_count":10}\n'
    )
    # Merge-cycle run
    (ky / "runs" / "run-20260322003001-xyz789.json").write_text("""{
        "id":"run-20260322003001-xyz789","repo_id":"repo-ky","phase":"merge-cycle",
        "started_at":"2026-03-22T00:30:00+00:00","ended_at":"2026-03-22T00:30:01+00:00",
        "duration_seconds":1,"dry_run":false,"findings_detected":0,"issues_created":0,
        "prs_created":0,"merges_completed":0,"health_before":92.0,"health_after":92.0,
        "health_delta":0.0,"status":"completed","error":null
    }""")

    # Zulip minimal setup
    zulip = repos / "zulip"
    zulip.mkdir()
    (zulip / "state").mkdir()
    (zulip / "runs").mkdir()
    (zulip / "state" / "status.json").write_text(
        '{"current_counts":{"open_issues":1,"open_prs":0,"findings_entries":1}}'
    )
    (zulip / "state" / "issues.json").write_text('{"issues":[]}')
    (zulip / "state" / "active_prs.json").write_text('{"version":1,"prs":{}}')
    (zulip / "state" / "health_history.jsonl").write_text(
        '{"timestamp":"2026-03-22T02:00:00+00:00","score":100.0,"findings_count":1}\n'
    )
    # No findings.jsonl for zulip (intentional - tests absence)

    return repos


# ─── _replace_repo_section tests ───────────────────────────────────────────────

class TestReplaceRepoSection:
    def test_replaces_new_format_section(self):
        from obsidian_sync import _replace_repo_section
        content = """# Issue-Cycle Log - 2026-03-22

## ky (vikasagarwal101/ky)

### Summary

Old ky content.

## zulip (vikasagarwal101/zulip)

### Summary

Zulip content.
"""
        new = "## ky (vikasagarwal101/ky)\n\n### Summary\n\nNew ky content.\n"
        result = _replace_repo_section(content, "ky", new)
        assert "New ky content" in result
        assert "Old ky content" not in result
        assert "Zulip content" in result

    def test_replaces_old_format_section_without_parens(self):
        """Old format (no ## <repo> (owner/<repo>) headers): replaces the first
        ## section heading and all its content with the new ky section."""
        from obsidian_sync import _replace_repo_section
        # Old format: file starts with # title, then ## Summary (no ## <repo> header)
        content = """# Issue-Cycle Log - 2026-03-22

## Summary

Old summary content.

## OtherSection

Other content.
"""
        new = "## ky (vikasagarwal101/ky)\n\n### Summary\n\nNew ky content.\n"
        result = _replace_repo_section(content, "ky", new)
        assert "New ky content" in result
        assert "Old summary content" not in result
        # File header is preserved
        assert "# Issue-Cycle Log - 2026-03-22" in result
        # Following sections are preserved
        assert "Other content" in result

    def test_appends_when_repo_section_not_found(self):
        """Content with other-repo sections: ky section gets replaced (no ## ky yet),
        other-repo sections stay. ky is appended after them."""
        from obsidian_sync import _replace_repo_section
        content = """# Issue-Cycle Log - 2026-03-22

## other-repo (owner/other-repo)

Other-repo content.
"""
        new = "## ky (vikasagarwal101/ky)\n\n### Summary\n\nKy content.\n"
        result = _replace_repo_section(content, "ky", new)
        assert "Ky content" in result
        # other-repo section stays
        assert "other-repo" in result
        assert "Other-repo content" in result
        # ky appears after other-repo
        assert result.index("ky") > result.index("other-repo")

    def test_empty_content_creates_section(self):
        from obsidian_sync import _replace_repo_section
        new = "## ky (vikasagarwal101/ky)\n\n### Summary\n\nKy content.\n"
        result = _replace_repo_section("", "ky", new)
        assert "Ky content" in result

    def test_idempotent_second_write(self):
        """Running twice should not duplicate sections."""
        from obsidian_sync import _replace_repo_section
        content = """# Issue-Cycle Log - 2026-03-22

## ky (vikasagarwal101/ky)

### Summary

First write.
"""
        new = "## ky (vikasagarwal101/ky)\n\n### Summary\n\nSecond write.\n"
        result1 = _replace_repo_section(content, "ky", new)
        result2 = _replace_repo_section(result1, "ky", new)
        # Should only appear once
        assert result2.count("Second write") == 1


# ─── Phase builder tests ───────────────────────────────────────────────────────

class TestPhaseBuilders:
    def test_build_issue_cycle_derives_counts_from_source_files(self, setup_temp_repos):
        """Counts must be derived from source-of-truth files (issues.json, findings.jsonl),
        NOT from stale status.json current_counts.

        Fixture: status.json has open_issues=3, findings_entries=10 (stale).
                issues.json has 2 open issues (correct).
                findings.jsonl has 8 entries (correct).
        """
        from obsidian_sync import _build_issue_cycle, REPOS_DIR
        import obsidian_sync
        orig = obsidian_sync.REPOS_DIR
        obsidian_sync.REPOS_DIR = setup_temp_repos
        try:
            result = _build_issue_cycle("ky", "2026-03-22")
            # Source-of-truth values: 2 open issues (from issues.json), 8 findings (from findings.jsonl)
            assert "Open Issues | 2 |" in result
            assert "Findings Tracked | 8 |" in result
            assert "Health Score" in result
            assert "#25" in result
            assert "#20" in result
        finally:
            obsidian_sync.REPOS_DIR = orig

    def test_build_issue_cycle_hides_closed_issues(self, setup_temp_repos):
        from obsidian_sync import _build_issue_cycle
        import obsidian_sync
        orig = obsidian_sync.REPOS_DIR
        obsidian_sync.REPOS_DIR = setup_temp_repos
        try:
            result = _build_issue_cycle("ky", "2026-03-22")
            # Closed issue #30 should not appear in open issues
            assert "#30" not in result
        finally:
            obsidian_sync.REPOS_DIR = orig

    def test_build_merge_cycle_shows_active_prs(self, setup_temp_repos):
        from obsidian_sync import _build_merge_cycle
        import obsidian_sync
        orig = obsidian_sync.REPOS_DIR
        obsidian_sync.REPOS_DIR = setup_temp_repos
        try:
            result = _build_merge_cycle("ky", "2026-03-22")
            assert "33" in result
            assert "31" in result
            assert "Active Tracked | 2 |" in result
        finally:
            obsidian_sync.REPOS_DIR = orig

    def test_build_qa_monitor_health_icon(self, setup_temp_repos):
        from obsidian_sync import _build_qa_monitor
        import obsidian_sync
        orig = obsidian_sync.REPOS_DIR
        obsidian_sync.REPOS_DIR = setup_temp_repos
        try:
            result = _build_qa_monitor("ky", "2026-03-22")
            # Score 92.0 -> green check
            assert "92.0" in result
            assert "✅" in result
        finally:
            obsidian_sync.REPOS_DIR = orig


# ─── daily_summary tests ────────────────────────────────────────────────────────

class TestDailySummary:
    def test_build_summary_markdown_has_required_sections(self, setup_temp_repos):
        from daily_summary import build_summary_markdown
        import daily_summary
        orig = daily_summary.REPOS_DIR
        daily_summary.REPOS_DIR = setup_temp_repos
        try:
            result = build_summary_markdown("ky", "2026-03-22")
            assert "## Health & Metrics" in result
            assert "## Today's Activity" in result
            assert "## Open Issues" in result
            assert "## Active PRs" in result
            assert "## Recent Runs" in result
            assert "ky" in result
        finally:
            daily_summary.REPOS_DIR = orig

    def test_summary_uses_health_icon(self, setup_temp_repos):
        from daily_summary import build_summary_markdown
        import daily_summary
        orig = daily_summary.REPOS_DIR
        daily_summary.REPOS_DIR = setup_temp_repos
        try:
            result = build_summary_markdown("ky", "2026-03-22")
            assert "✅" in result  # Score 92.0 -> green
        finally:
            daily_summary.REPOS_DIR = orig


# ─── Sync integration tests ─────────────────────────────────────────────────────

class TestSyncPhase:
    def test_sync_phase_writes_file(self, setup_temp_repos, tmp_path):
        from obsidian_sync import sync_phase, OBSIDIAN_ROOT
        import obsidian_sync
        orig_repos = obsidian_sync.REPOS_DIR
        orig_obs = obsidian_sync.OBSIDIAN_ROOT
        obsidian_sync.REPOS_DIR = setup_temp_repos
        obsidian_sync.OBSIDIAN_ROOT = tmp_path
        try:
            success = sync_phase("ky", "issue-cycle", "2026-03-22")
            assert success
            out_file = tmp_path / "issue-cycle" / "2026-03-22.md"
            assert out_file.exists()
            content = out_file.read_text()
            assert "## ky (vikasagarwal101/ky)" in content
        finally:
            obsidian_sync.REPOS_DIR = orig_repos
            obsidian_sync.OBSIDIAN_ROOT = orig_obs

    def test_sync_phase_idempotent(self, setup_temp_repos, tmp_path):
        from obsidian_sync import sync_phase, OBSIDIAN_ROOT
        import obsidian_sync
        orig_repos = obsidian_sync.REPOS_DIR
        orig_obs = obsidian_sync.OBSIDIAN_ROOT
        obsidian_sync.REPOS_DIR = setup_temp_repos
        obsidian_sync.OBSIDIAN_ROOT = tmp_path
        try:
            sync_phase("ky", "issue-cycle", "2026-03-22")
            sync_phase("ky", "issue-cycle", "2026-03-22")
            out_file = tmp_path / "issue-cycle" / "2026-03-22.md"
            content = out_file.read_text()
            assert content.count("## ky (vikasagarwal101/ky)") == 1
        finally:
            obsidian_sync.REPOS_DIR = orig_repos
            obsidian_sync.OBSIDIAN_ROOT = orig_obs

    def test_sync_two_repos_same_file(self, setup_temp_repos, tmp_path):
        """Both repos should appear in same date-stamped file, each once."""
        from obsidian_sync import sync_phase, OBSIDIAN_ROOT
        import obsidian_sync
        orig_repos = obsidian_sync.REPOS_DIR
        orig_obs = obsidian_sync.OBSIDIAN_ROOT
        obsidian_sync.REPOS_DIR = setup_temp_repos
        obsidian_sync.OBSIDIAN_ROOT = tmp_path
        try:
            sync_phase("ky", "issue-cycle", "2026-03-22")
            sync_phase("zulip", "issue-cycle", "2026-03-22")
            out_file = tmp_path / "issue-cycle" / "2026-03-22.md"
            content = out_file.read_text()
            assert "## ky (vikasagarwal101/ky)" in content
            assert "## zulip (vikasagarwal101/zulip)" in content
            assert content.count("## ky (vikasagarwal101/ky)") == 1
            assert content.count("## zulip (vikasagarwal101/zulip)") == 1
        finally:
            obsidian_sync.REPOS_DIR = orig_repos
            obsidian_sync.OBSIDIAN_ROOT = orig_obs


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
