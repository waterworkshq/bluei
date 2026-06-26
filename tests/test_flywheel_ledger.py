#!/usr/bin/env python3
"""Tests for flywheel_ledger exposure in extract_report_data and markdown rendering.

Covers PROMPT-06 requirements (T3.1 + T3.4):
- extract_report_data returns flywheel_ledger from status.json (or {} when absent)
- generate_markdown_report renders Deterministic Flywheel section when populated
- Section omitted entirely when flywheel_ledger is None/empty
- Divide-by-zero (attempted=0) renders "n/a", not a crash
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bluei.app.report import ReportGenerator
from bluei.app.models import Repo, RepoConfig, RepoStatus
from bluei.engine.report import extract_report_data


# ── Fixture: canonical flywheel_ledger block (matches slice 5 output shape) ──

FULL_LEDGER = {
    "findings_attempted": 25,
    "resolved_deterministic_by_stage": {
        "pattern-replay": 12,
        "recipe": 5,
        "autofix": 3,
    },
    "resolved_deterministic_total": 20,
    "resolved_llm": 3,
    "exhausted": 2,
    "pattern_replay_resolutions": 12,
    "savings_usd": 0.48,
    "cost_total_usd": 1.25,
    "rates": {},
    "active_pattern_count": 8,
    "top_failing_patterns": [
        {"pattern_id": "pat-001", "rule": "broad-except", "failure_count": 5},
        {"pattern_id": "pat-002", "rule": "ruff-c408", "failure_count": 3},
        {"pattern_id": "pat-003", "rule": "shellcheck-sc2086", "failure_count": 2},
    ],
}


def _make_repo(name="test-repo") -> Repo:
    config = RepoConfig(
        id=f"repo-{name}",
        name=name,
        path=f"/tmp/{name}",
        language="python",
    )
    return Repo(
        config=config,
        status=RepoStatus.READY,
        current_findings_count=5,
        current_health_score=82.0,
        total_fixes=2,
        total_prs=1,
        total_merges=0,
    )


# ── T3.1: extract_report_data returns flywheel_ledger ──


class TestExtractReportDataFlywheelLedger:
    """T3.1: extract_report_data surfaces flywheel_ledger from status.json."""

    def _setup_state(self, tmp_path, repo_name="fw-repo", status_extra=None):
        state_dir = tmp_path / "repos" / repo_name / "state"
        state_dir.mkdir(parents=True)
        status = {
            "current_counts": {"open_issues": 1, "open_prs": 0},
            "health": {"score": 70.0},
            "language": "python",
            "last_run_at": "2026-06-25T10:00:00Z",
        }
        if status_extra:
            status.update(status_extra)
        (state_dir / "status.json").write_text(json.dumps(status))
        return state_dir

    def _setup_state_with_ledger(self, tmp_path, repo_name="fw-repo"):
        return self._setup_state(tmp_path, repo_name, {"flywheel_ledger": FULL_LEDGER})

    def test_returns_flywheel_ledger_when_present(self, tmp_path):
        state_dir = self._setup_state_with_ledger(tmp_path)
        data = extract_report_data(
            repo_path="/tmp/fw-repo",
            repo_name="fw-repo",
            state_dir=state_dir,
        )
        assert "flywheel_ledger" in data
        ledger = data["flywheel_ledger"]
        assert ledger["findings_attempted"] == 25
        assert ledger["resolved_deterministic_total"] == 20
        assert ledger["pattern_replay_resolutions"] == 12

    def test_returns_empty_dict_when_absent(self, tmp_path):
        state_dir = self._setup_state(tmp_path)
        data = extract_report_data(
            repo_path="/tmp/fw-repo",
            repo_name="fw-repo",
            state_dir=state_dir,
        )
        assert "flywheel_ledger" in data
        assert data["flywheel_ledger"] == {}

    def test_returns_empty_dict_when_null(self, tmp_path):
        state_dir = self._setup_state(tmp_path, status_extra={"flywheel_ledger": None})
        data = extract_report_data(
            repo_path="/tmp/fw-repo",
            repo_name="fw-repo",
            state_dir=state_dir,
        )
        assert data["flywheel_ledger"] == {}


# ── T3.4: generate_markdown_report renders the section ──


class TestFlywheelMarkdownSection:
    """T3.4: generate_markdown_report renders Deterministic Flywheel section."""

    def setup_method(self):
        self.rg = ReportGenerator()

    def test_section_heading_present_when_ledger_populated(self):
        repo = _make_repo()
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, flywheel_ledger=FULL_LEDGER
        )
        assert "## Deterministic Flywheel" in result

    def test_resolution_rate_num_denom_percent(self):
        repo = _make_repo()
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, flywheel_ledger=FULL_LEDGER
        )
        # 20/25 (80.0%) deterministic
        assert "20/25 (80.0%) deterministic" in result
        # 3/25 (12.0%) LLM fallback
        assert "3/25 (12.0%) LLM fallback" in result

    def test_per_stage_breakdown(self):
        repo = _make_repo()
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, flywheel_ledger=FULL_LEDGER
        )
        assert "pattern-replay: 12" in result
        assert "recipe: 5" in result
        assert "autofix: 3" in result
        assert "LLM fallback: 3" in result
        assert "Exhausted: 2" in result

    def test_pattern_replay_stats(self):
        repo = _make_repo()
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, flywheel_ledger=FULL_LEDGER
        )
        assert "12 resolving hits" in result
        assert "8 active patterns" in result

    def test_top_failing_patterns(self):
        repo = _make_repo()
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, flywheel_ledger=FULL_LEDGER
        )
        assert "`pat-001`" in result
        assert "broad-except (failure_count=5)" in result
        assert "`pat-002`" in result
        assert "ruff-c408 (failure_count=3)" in result
        assert "`pat-003`" in result
        assert "shellcheck-sc2086 (failure_count=2)" in result

    def test_cost_savings(self):
        repo = _make_repo()
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, flywheel_ledger=FULL_LEDGER
        )
        assert "$1.25 spent" in result
        assert "$0.48 avoided" in result

    def test_adr0003_footnote_present(self):
        repo = _make_repo()
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, flywheel_ledger=FULL_LEDGER
        )
        assert (
            "Dollar savings reflect standalone Pattern-replay substitutions" in result
        )
        assert "cascade-internal Pattern/composite" in result
        assert "throughput, not cost" in result

    def test_section_omitted_when_ledger_none(self):
        repo = _make_repo()
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, flywheel_ledger=None
        )
        assert "## Deterministic Flywheel" not in result

    def test_section_omitted_when_ledger_empty(self):
        repo = _make_repo()
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, flywheel_ledger={}
        )
        assert "## Deterministic Flywheel" not in result

    def test_divide_by_zero_guards_to_na(self):
        repo = _make_repo()
        zero_ledger = {
            "findings_attempted": 0,
            "resolved_deterministic_total": 0,
            "resolved_llm": 0,
            "exhausted": 0,
        }
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, flywheel_ledger=zero_ledger
        )
        assert "## Deterministic Flywheel" in result
        assert "n/a" in result
        # Should not contain any division by zero crash
        assert "ZeroDivisionError" not in result

    def test_position_after_vitality_components(self):
        """Section should appear between Vitality Components and Specks Breakdown."""
        repo = _make_repo()
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, flywheel_ledger=FULL_LEDGER
        )
        vitality_pos = result.find("## Vitality Components")
        flywheel_pos = result.find("## Deterministic Flywheel")
        specks_pos = result.find("## Specks Breakdown")
        assert vitality_pos < flywheel_pos < specks_pos

    def test_top_failing_patterns_limited_to_three(self):
        """Even if more than 3 patterns exist, only top 3 are rendered."""
        repo = _make_repo()
        ledger = {
            **FULL_LEDGER,
            "top_failing_patterns": [
                {
                    "pattern_id": f"pat-{i:03d}",
                    "rule": f"rule-{i}",
                    "failure_count": 10 - i,
                }
                for i in range(5)
            ],
        }
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, flywheel_ledger=ledger
        )
        assert "pat-000" in result
        assert "pat-001" in result
        assert "pat-002" in result
        assert "pat-003" not in result
        assert "pat-004" not in result


class TestFlywheelFromStateDir:
    """Integration: from_state_dir passes flywheel_ledger through to markdown."""

    def test_from_state_dir_renders_flywheel_section(self, tmp_path):
        state_dir = tmp_path / "repos" / "fw-int" / "state"
        state_dir.mkdir(parents=True)
        status = {
            "current_counts": {"open_issues": 1, "open_prs": 0},
            "health": {"score": 70.0},
            "language": "python",
            "last_run_at": "2026-06-25T10:00:00Z",
            "flywheel_ledger": FULL_LEDGER,
        }
        (state_dir / "status.json").write_text(json.dumps(status))

        markdown = ReportGenerator.from_state_dir(
            repo_name="fw-int",
            state_dir=state_dir,
            workspace=tmp_path,
        )
        assert "## Deterministic Flywheel" in markdown
        assert "20/25 (80.0%) deterministic" in markdown

    def test_from_state_dir_omits_section_when_no_ledger(self, tmp_path):
        state_dir = tmp_path / "repos" / "fw-empty" / "state"
        state_dir.mkdir(parents=True)
        status = {
            "current_counts": {"open_issues": 0, "open_prs": 0},
            "health": {"score": 50.0},
            "language": "python",
        }
        (state_dir / "status.json").write_text(json.dumps(status))

        markdown = ReportGenerator.from_state_dir(
            repo_name="fw-empty",
            state_dir=state_dir,
            workspace=tmp_path,
        )
        assert "## Deterministic Flywheel" not in markdown


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
