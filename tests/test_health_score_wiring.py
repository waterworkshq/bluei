"""Tests for _compute_health_score() in cli.py — pure function, no dependencies."""

import sys
from pathlib import Path
from typing import Any, Dict, List

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core" / "sandbox_local_runner"))

from core.sandbox_local_runner.cli import _compute_health_score


def _make_issue(status: str = "open") -> Dict[str, Any]:
    return {"issue_id": "QA-0001", "status": status, "finding_id": "abc"}


def _make_issues(*statuses: str) -> List[Dict[str, Any]]:
    return [_make_issue(s) for s in statuses]


class TestHealthScorePerfect:
    """100 — everything clean."""

    def test_zero_issues_zero_prs(self):
        score = _compute_health_score(raw_open_issues=0, live_open_prs=0, issues_list=[])
        assert score == 100

    def test_no_issues_no_terminal(self):
        score = _compute_health_score(
            raw_open_issues=0, live_open_prs=0,
            issues_list=_make_issues("closed", "completed"),
        )
        assert score == 100


class TestHealthScoreDeductions:
    """Penalties applied correctly."""

    def test_open_issues_deduct_5_each(self):
        # 3 open issues = -15
        score = _compute_health_score(
            raw_open_issues=3, live_open_prs=0,
            issues_list=_make_issues("open", "open", "open"),
        )
        assert score == 85

    def test_open_prs_deduct_10_each(self):
        # 2 open PRs = -20
        score = _compute_health_score(
            raw_open_issues=0, live_open_prs=2, issues_list=[],
        )
        assert score == 80

    def test_terminal_issues_deduct_3_each(self):
        # 4 terminal issues = -12
        score = _compute_health_score(
            raw_open_issues=0, live_open_prs=0,
            issues_list=_make_issues(
                "needs-human-max-retries-exceeded",
                "blocked_untracked_path",
                "needs-human-refactor-review",
                "open",  # not terminal
            ),
        )
        assert score == 91  # 100 - 3*3 = 91

    def test_combined_deductions(self):
        # 3 open issues = -15, 1 open PR = -10, 2 terminal = -6
        score = _compute_health_score(
            raw_open_issues=3, live_open_prs=1,
            issues_list=_make_issues(
                "open", "open", "open",
                "needs-human-max-retries-exceeded",
                "blocked_untracked_path",
            ),
        )
        assert score == 69  # 100 - 15 - 10 - 6 = 69


class TestHealthScoreBounds:
    """Clamped 0-100."""

    def test_floor_at_zero(self):
        score = _compute_health_score(
            raw_open_issues=50, live_open_prs=50, issues_list=[],
        )
        assert score == 0  # clamped

    def test_ceiling_at_100(self):
        score = _compute_health_score(raw_open_issues=0, live_open_prs=0, issues_list=[])
        assert score == 100

    def test_negative_deduction_clamped(self):
        score = _compute_health_score(
            raw_open_issues=100, live_open_prs=100, issues_list=[],
        )
        assert score == 0
