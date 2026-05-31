"""Tests for bluei.engine.clean_prs — stale PR detection and cleanup."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


class TestFetchOpenPrs:
    """Tests for _fetch_open_prs."""

    def test_fetch_open_prs_success(self, tmp_path: Path) -> None:
        from bluei.engine import clean_prs

        pr_data = [
            {
                "number": 1,
                "title": "fix: something",
                "headRefName": "branch-a",
                "createdAt": "2025-01-01T00:00:00Z",
                "updatedAt": "2025-01-02T00:00:00Z",
                "body": "",
                "labels": [],
            },
        ]
        with patch(
            "bluei.engine.clean_prs.run_capture", return_value=(0, json.dumps(pr_data))
        ):
            result = clean_prs._fetch_open_prs("owner/repo", tmp_path)
        assert len(result) == 1
        assert result[0]["number"] == 1

    def test_fetch_open_prs_failure(self, tmp_path: Path) -> None:
        from bluei.engine import clean_prs

        with patch("bluei.engine.clean_prs.run_capture", return_value=(1, "")):
            result = clean_prs._fetch_open_prs("owner/repo", tmp_path)
        assert result == []

    def test_fetch_open_prs_empty_output(self, tmp_path: Path) -> None:
        from bluei.engine import clean_prs

        with patch("bluei.engine.clean_prs.run_capture", return_value=(0, "")):
            result = clean_prs._fetch_open_prs("owner/repo", tmp_path)
        assert result == []

    def test_fetch_open_prs_bad_json(self, tmp_path: Path) -> None:
        from bluei.engine import clean_prs

        with patch("bluei.engine.clean_prs.run_capture", return_value=(0, "not json")):
            result = clean_prs._fetch_open_prs("owner/repo", tmp_path)
        assert result == []


class TestClosePr:
    """Tests for _close_pr."""

    def test_close_pr_success(self, tmp_path: Path) -> None:
        from bluei.engine import clean_prs

        with patch("bluei.engine.clean_prs.run_capture", return_value=(0, "")):
            ok = clean_prs._close_pr("owner/repo", 42, "reason", False, tmp_path)
        assert ok is True

    def test_close_pr_failure(self, tmp_path: Path) -> None:
        from bluei.engine import clean_prs

        with patch("bluei.engine.clean_prs.run_capture", return_value=(1, "error")):
            ok = clean_prs._close_pr("owner/repo", 42, "reason", False, tmp_path)
        assert ok is False

    def test_close_pr_dry_run(self, tmp_path: Path) -> None:
        from bluei.engine import clean_prs

        with patch("bluei.engine.clean_prs.run_capture") as mock:
            ok = clean_prs._close_pr("owner/repo", 42, "reason", True, tmp_path)
        assert ok is True
        mock.assert_not_called()


class TestFindDuplicatePrs:
    """Tests for _find_duplicate_prs."""

    def test_find_duplicate_prs_batch_titles(self) -> None:
        from bluei.engine import clean_prs

        prs = [
            {
                "number": 1,
                "title": "fix: resolve 5 ruff-c408 findings",
                "createdAt": "2025-01-01T00:00:00Z",
            },
            {
                "number": 2,
                "title": "fix: resolve 3 ruff-c408 findings",
                "createdAt": "2025-01-02T00:00:00Z",
            },
        ]
        result = clean_prs._find_duplicate_prs(prs)
        assert len(result) == 1
        assert result[0]["type"] == "duplicate"
        assert result[0]["pr_number"] == 1

    def test_find_duplicate_prs_solo_titles(self) -> None:
        from bluei.engine import clean_prs

        prs = [
            {
                "number": 1,
                "title": "fix: resolve ruff-c408 in src/a.py",
                "createdAt": "2025-01-01T00:00:00Z",
            },
            {
                "number": 2,
                "title": "fix: resolve ruff-c408 in src/b.py",
                "createdAt": "2025-01-02T00:00:00Z",
            },
        ]
        result = clean_prs._find_duplicate_prs(prs)
        assert len(result) == 1
        assert result[0]["pr_number"] == 1

    def test_find_duplicate_prs_no_duplicates(self) -> None:
        from bluei.engine import clean_prs

        prs = [
            {
                "number": 1,
                "title": "fix: resolve ruff-c408 findings",
                "createdAt": "2025-01-01T00:00:00Z",
            },
            {
                "number": 2,
                "title": "fix: resolve ruff-e501 findings",
                "createdAt": "2025-01-02T00:00:00Z",
            },
        ]
        result = clean_prs._find_duplicate_prs(prs)
        assert result == []

    def test_find_duplicate_prs_single_pr(self) -> None:
        from bluei.engine import clean_prs

        prs = [
            {
                "number": 1,
                "title": "fix: resolve ruff-c408 findings",
                "createdAt": "2025-01-01T00:00:00Z",
            },
        ]
        result = clean_prs._find_duplicate_prs(prs)
        assert result == []

    def test_find_duplicate_prs_empty_title(self) -> None:
        from bluei.engine import clean_prs

        prs = [
            {"number": 1, "title": "", "createdAt": "2025-01-01T00:00:00Z"},
            {"number": 2, "title": "", "createdAt": "2025-01-02T00:00:00Z"},
        ]
        result = clean_prs._find_duplicate_prs(prs)
        assert len(result) == 1


class TestPrAgeHours:
    """Tests for _pr_age_hours."""

    def test_pr_age_hours(self) -> None:
        from bluei.engine import clean_prs

        recent = datetime.now(timezone.utc) - timedelta(hours=5)
        pr = {"createdAt": recent.isoformat()}
        age = clean_prs._pr_age_hours(pr)
        assert 4.5 < age < 5.5

    def test_pr_age_hours_empty(self) -> None:
        from bluei.engine import clean_prs

        assert clean_prs._pr_age_hours({}) == 0
        assert clean_prs._pr_age_hours({"createdAt": ""}) == 0

    def test_pr_age_hours_bad_format(self) -> None:
        from bluei.engine import clean_prs

        assert clean_prs._pr_age_hours({"createdAt": "not-a-date"}) == 0


class TestFindStalePrs:
    """Tests for _find_stale_prs."""

    def test_find_stale_prs(self) -> None:
        from bluei.engine import clean_prs

        old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        prs = [
            {"number": 1, "title": "old pr", "updatedAt": old},
        ]
        result = clean_prs._find_stale_prs(prs, max_age_hours=48)
        assert len(result) == 1
        assert result[0]["type"] == "stale"
        assert result[0]["pr_number"] == 1

    def test_find_stale_prs_recent_pr(self) -> None:
        from bluei.engine import clean_prs

        recent = datetime.now(timezone.utc).isoformat()
        prs = [
            {"number": 1, "title": "recent pr", "updatedAt": recent},
        ]
        result = clean_prs._find_stale_prs(prs, max_age_hours=48)
        assert result == []

    def test_find_stale_prs_empty_updated(self) -> None:
        from bluei.engine import clean_prs

        prs = [{"number": 1, "title": "pr", "updatedAt": ""}]
        result = clean_prs._find_stale_prs(prs)
        assert result == []

    def test_find_stale_prs_bad_updated(self) -> None:
        from bluei.engine import clean_prs

        prs = [{"number": 1, "title": "pr", "updatedAt": "bad-date"}]
        result = clean_prs._find_stale_prs(prs)
        assert result == []


class TestCleanStalePrs:
    """Integration tests for clean_stale_prs."""

    def test_clean_stale_prs_dry_run(self, tmp_path: Path) -> None:
        from bluei.engine import clean_prs

        log_file = tmp_path / "log.txt"

        old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        pr_data = [
            {
                "number": 1,
                "title": "fix: resolve 5 ruff-c408 findings",
                "headRefName": "b1",
                "createdAt": old,
                "updatedAt": old,
                "body": "",
                "labels": [],
            },
            {
                "number": 2,
                "title": "fix: resolve 3 ruff-c408 findings",
                "headRefName": "b2",
                "createdAt": old,
                "updatedAt": old,
                "body": "",
                "labels": [],
            },
        ]

        with patch(
            "bluei.engine.clean_prs.run_capture", return_value=(0, json.dumps(pr_data))
        ):
            with patch("bluei.engine.clean_prs._append_text"):
                result = clean_prs.clean_stale_prs(
                    "owner/repo", tmp_path, log_file, dry_run=True
                )
        assert result["closed"] == 2
        assert result["duplicates"] == 1
        assert result["stale"] == 1

    def test_clean_stale_prs_no_prs(self, tmp_path: Path) -> None:
        from bluei.engine import clean_prs

        log_file = tmp_path / "log.txt"

        with patch("bluei.engine.clean_prs.run_capture", return_value=(0, "[]")):
            with patch("bluei.engine.clean_prs._append_text"):
                result = clean_prs.clean_stale_prs(
                    "owner/repo", tmp_path, log_file, dry_run=True
                )
        assert result["closed"] == 0
        assert result["duplicates"] == 0
        assert result["stale"] == 0

    def test_clean_stale_prs_close_failure(self, tmp_path: Path) -> None:
        from bluei.engine import clean_prs

        log_file = tmp_path / "log.txt"

        old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        pr_data = [
            {
                "number": 1,
                "title": "fix: resolve ruff-c408 in a.py",
                "headRefName": "b1",
                "createdAt": old,
                "updatedAt": old,
                "body": "",
                "labels": [],
            },
            {
                "number": 2,
                "title": "fix: resolve ruff-c408 in b.py",
                "headRefName": "b2",
                "createdAt": old,
                "updatedAt": old,
                "body": "",
                "labels": [],
            },
        ]

        def mock_run_capture(cmd, cwd, timeout=0):
            if "list" in cmd:
                return (0, json.dumps(pr_data))
            return (1, "error")

        with patch("bluei.engine.clean_prs.run_capture", side_effect=mock_run_capture):
            with patch("bluei.engine.clean_prs._append_text"):
                result = clean_prs.clean_stale_prs(
                    "owner/repo", tmp_path, log_file, dry_run=False
                )
        assert result["closed"] == 0

    def test_clean_stale_prs_dedup_removes_from_stale(self, tmp_path: Path) -> None:
        from bluei.engine import clean_prs

        log_file = tmp_path / "log.txt"

        old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        pr_data = [
            {
                "number": 1,
                "title": "fix: resolve 5 ruff-c408 findings",
                "headRefName": "b1",
                "createdAt": old,
                "updatedAt": old,
                "body": "",
                "labels": [],
            },
            {
                "number": 2,
                "title": "fix: resolve 3 ruff-c408 findings",
                "headRefName": "b2",
                "createdAt": old,
                "updatedAt": old,
                "body": "",
                "labels": [],
            },
        ]

        with patch(
            "bluei.engine.clean_prs.run_capture", return_value=(0, json.dumps(pr_data))
        ):
            with patch("bluei.engine.clean_prs._append_text"):
                result = clean_prs.clean_stale_prs(
                    "owner/repo", tmp_path, log_file, dry_run=True
                )
        assert result["duplicates"] >= 1
