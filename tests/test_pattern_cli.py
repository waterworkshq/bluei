import json
import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.pattern_store import (
    DEACTIVATION_THRESHOLD,
    FixPattern,
    FixPatternStore,
)
from bluei.engine.pattern_replay import PROMPT_HINT_THRESHOLD

sys.path.insert(0, ".")
from bin.bluei import (
    _cmd_patterns,
    _patterns_list,
    _patterns_show,
    _patterns_deactivate,
    _patterns_reactivate,
    _patterns_exclude,
    _patterns_unexclude,
    _parse_repo_arg,
    HELP_TEXT,
)


def make_pattern(**overrides):
    data = {
        "pattern_id": "fp-test1234567",
        "rule": "broad-except",
        "language": "python",
        "file_path": "src/*.py",
        "before_snippet": "except:\n    pass",
        "after_snippet": "except Exception:\n    pass",
        "diff_patch": "--- a/src/file.py\n+++ b/src/file.py\n-except:\n+except Exception:\n     pass\n",
        "confidence": 0.8,
        "success_count": 5,
        "failure_count": 1,
        "skip_count": 0,
        "source": "claude",
        "created_at": "2026-05-17T10:30:00+00:00",
        "last_used_at": "2026-05-18T14:22:00+00:00",
        "last_verified_at": "2026-05-18T14:22:00+00:00",
        "source_finding_ids": ["finding-abc", "finding-def"],
    }
    data.update(overrides)
    return FixPattern(**data)


@pytest.fixture
def store_with_patterns(tmp_path):
    store_path = tmp_path / "repos" / "myrepo" / "state" / "fix_patterns.jsonl"
    store = FixPatternStore(store_path)
    store.append(make_pattern(pattern_id="", confidence=0.8))
    store.append(
        make_pattern(
            pattern_id="",
            rule="trailing-whitespace",
            before_snippet="line \n",
            after_snippet="line\n",
            diff_patch="--- a/f.py\n+++ b/f.py\n-line \n+line\n",
            confidence=0.6,
            success_count=2,
        )
    )
    return store


@pytest.fixture
def store_empty(tmp_path):
    store_path = tmp_path / "repos" / "myrepo" / "state" / "fix_patterns.jsonl"
    return FixPatternStore(store_path)


class TestParseRepoArg:
    def test_extracts_repo_value(self):
        assert _parse_repo_arg(["--repo", "my-app"]) == "my-app"

    def test_returns_none_when_missing(self):
        assert _parse_repo_arg([]) is None

    def test_returns_none_when_no_value(self):
        assert _parse_repo_arg(["--repo"]) is None

    def test_ignores_other_args(self):
        assert _parse_repo_arg(["list", "--repo", "my-app"]) == "my-app"


class TestPatternsList:
    def test_list_with_active_patterns_formats_table(self, store_with_patterns, capsys):
        rc = _patterns_list(store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "broad-except" in out
        assert "trailing-whitespace" in out
        assert "PATTERN ID" in out
        assert "Total: 2 active patterns" in out

    def test_list_with_no_patterns(self, store_empty, capsys):
        rc = _patterns_list(store_empty)
        assert rc == 0
        out = capsys.readouterr().out
        assert "No active patterns found" in out

    def test_list_excludes_deactivated(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        store_with_patterns.update_confidence(pattern_id, -1.0)
        rc = _patterns_list(store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Total: 1 active patterns" in out


class TestPatternsShow:
    def test_show_valid_pattern_id(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        rc = _patterns_show([pattern_id], store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert f"Pattern:    {pattern_id}" in out
        assert "broad-except" in out
        assert "Before:" in out
        assert "After:" in out
        assert "Diff patch:" in out
        assert "except" in out

    def test_show_invalid_pattern_id(self, store_with_patterns, capsys):
        rc = _patterns_show(["fp-nonexistent"], store_with_patterns)
        assert rc == 1
        out = capsys.readouterr().err
        assert "not found" in out

    def test_show_no_pattern_id(self, store_with_patterns, capsys):
        rc = _patterns_show(["--repo", "myrepo"], store_with_patterns)
        assert rc == 1
        out = capsys.readouterr().err
        assert "requires a pattern_id" in out

    def test_show_displays_findings(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        rc = _patterns_show([pattern_id], store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "finding-abc" in out

    def test_show_displays_diff_with_color(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        rc = _patterns_show([pattern_id], store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "-except:" in out
        assert "+except Exception:" in out


class TestPatternsDeactivate:
    def test_deactivate_valid_pattern(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        rc = _patterns_deactivate([pattern_id], store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "deactivated" in out
        pattern = store_with_patterns._patterns[pattern_id]
        assert pattern.confidence == 0.0

    def test_deactivate_invalid_pattern(self, store_with_patterns, capsys):
        rc = _patterns_deactivate(["fp-nonexistent"], store_with_patterns)
        assert rc == 1
        out = capsys.readouterr().err
        assert "not found" in out

    def test_deactivate_no_pattern_id(self, store_with_patterns, capsys):
        rc = _patterns_deactivate(["--repo", "x"], store_with_patterns)
        assert rc == 1
        out = capsys.readouterr().err
        assert "requires a pattern_id" in out


class TestPatternsReactivate:
    def test_reactivate_valid_pattern(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        store_with_patterns.update_confidence(pattern_id, -1.0)
        assert store_with_patterns._patterns[pattern_id].confidence == 0.0

        rc = _patterns_reactivate([pattern_id], store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "reactivated" in out
        pattern = store_with_patterns._patterns[pattern_id]
        assert pattern.confidence == pytest.approx(PROMPT_HINT_THRESHOLD)

    def test_reactivate_invalid_pattern(self, store_with_patterns, capsys):
        rc = _patterns_reactivate(["fp-nonexistent"], store_with_patterns)
        assert rc == 1
        out = capsys.readouterr().err
        assert "not found" in out

    def test_reactivate_no_pattern_id(self, store_with_patterns, capsys):
        rc = _patterns_reactivate(["--repo", "x"], store_with_patterns)
        assert rc == 1
        out = capsys.readouterr().err
        assert "requires a pattern_id" in out


class TestCmdPatternsDispatch:
    def test_no_args_shows_help(self, capsys):
        rc = _cmd_patterns([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "patterns" in out

    def test_help_flag_shows_help(self, capsys):
        rc = _cmd_patterns(["--help"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "patterns" in out

    def test_missing_repo_returns_error(self, capsys):
        rc = _cmd_patterns(["list"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "--repo" in err

    def test_unknown_subcommand(self, tmp_path, capsys):
        with (
            patch("bluei.app.config.ConfigManager") as MockConfig,
            patch("bluei.app.state.StateManager") as MockState,
        ):
            MockConfig.return_value.repos_dir = tmp_path / "repos"
            mock_state = MagicMock()
            mock_state.get_fix_patterns_file.return_value = (
                tmp_path / "repos" / "r" / "state" / "fix_patterns.jsonl"
            )
            MockState.return_value = mock_state
            (tmp_path / "repos" / "r" / "state").mkdir(parents=True, exist_ok=True)
            (tmp_path / "repos" / "r" / "state" / "fix_patterns.jsonl").write_text("")

            rc = _cmd_patterns(["bogus", "--repo", "r"])
            assert rc == 1
            err = capsys.readouterr().err
            assert "unknown patterns subcommand" in err


class TestPatternsShowEnrichment:
    def test_show_renders_three_way_counts(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        rc = _patterns_show([pattern_id], store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Replay:" in out
        assert "HIT=5" in out
        assert "MISS=0" in out
        assert "FAILURE=1" in out

    def test_show_renders_last_failed_when_set(self, tmp_path, capsys):
        store_path = tmp_path / "repos" / "myrepo" / "state" / "fix_patterns.jsonl"
        store = FixPatternStore(store_path)
        store.append(
            make_pattern(
                pattern_id="",
                last_failed_at="2026-06-20T09:15:00+00:00",
            )
        )
        pattern_id = list(store._patterns.keys())[0]
        rc = _patterns_show([pattern_id], store)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Last failed: 2026-06-20T09:15:00" in out

    def test_show_shows_never_when_last_failed_unset(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        rc = _patterns_show([pattern_id], store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Last failed: never" in out

    def test_show_renders_excluded_paths_when_populated(self, tmp_path, capsys):
        store_path = tmp_path / "repos" / "myrepo" / "state" / "fix_patterns.jsonl"
        store = FixPatternStore(store_path)
        store.append(
            make_pattern(
                pattern_id="",
                excluded_paths=["vendor/*", "tests/*"],
            )
        )
        pattern_id = list(store._patterns.keys())[0]
        rc = _patterns_show([pattern_id], store)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Excluded:" in out
        assert "vendor/*" in out
        assert "tests/*" in out

    def test_show_omits_excluded_line_when_empty(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        rc = _patterns_show([pattern_id], store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Excluded:" not in out


class TestPatternsListMissColumn:
    def test_list_shows_miss_column(self, store_with_patterns, capsys):
        rc = _patterns_list(store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "MISS" in out
        assert "HIT" in out
        assert "FAIL" in out

    def test_list_renders_honest_counts(self, tmp_path, capsys):
        store_path = tmp_path / "repos" / "myrepo" / "state" / "fix_patterns.jsonl"
        store = FixPatternStore(store_path)
        store.append(
            make_pattern(
                pattern_id="",
                success_count=7,
                skip_count=3,
                failure_count=2,
            )
        )
        rc = _patterns_list(store)
        assert rc == 0
        out = capsys.readouterr().out
        assert " 7 " in out
        assert " 3 " in out
        assert " 2 " in out


class TestPatternsExclude:
    def test_exclude_adds_glob(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        rc = _patterns_exclude([pattern_id, "vendor/*"], store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert f"excluded path 'vendor/*' added" in out
        pattern = store_with_patterns._patterns[pattern_id]
        assert "vendor/*" in pattern.excluded_paths

    def test_exclude_unknown_pattern_errors(self, store_with_patterns, capsys):
        rc = _patterns_exclude(["fp-nonexistent", "vendor/*"], store_with_patterns)
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err

    def test_exclude_missing_glob_errors(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        rc = _patterns_exclude([pattern_id], store_with_patterns)
        assert rc == 1
        err = capsys.readouterr().err
        assert "requires" in err

    def test_exclude_missing_pattern_id_errors(self, store_with_patterns, capsys):
        rc = _patterns_exclude([], store_with_patterns)
        assert rc == 1
        err = capsys.readouterr().err
        assert "requires" in err

    def test_exclude_idempotent_when_already_excluded(
        self, store_with_patterns, capsys
    ):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        store_with_patterns.add_excluded_path(pattern_id, "vendor/*")
        rc = _patterns_exclude([pattern_id, "vendor/*"], store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "already excluded" in out


class TestPatternsUnexclude:
    def test_unexclude_removes_glob(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        store_with_patterns.add_excluded_path(pattern_id, "vendor/*")
        rc = _patterns_unexclude([pattern_id, "vendor/*"], store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert f"excluded path 'vendor/*' removed" in out
        pattern = store_with_patterns._patterns[pattern_id]
        assert "vendor/*" not in pattern.excluded_paths

    def test_unexclude_unknown_pattern_errors(self, store_with_patterns, capsys):
        rc = _patterns_unexclude(["fp-nonexistent", "vendor/*"], store_with_patterns)
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err

    def test_unexclude_missing_glob_errors(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        rc = _patterns_unexclude([pattern_id], store_with_patterns)
        assert rc == 1
        err = capsys.readouterr().err
        assert "requires" in err

    def test_unexclude_not_excluded_is_noop(self, store_with_patterns, capsys):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        rc = _patterns_unexclude([pattern_id, "vendor/*"], store_with_patterns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "was not excluded" in out


class TestExcludeEnforcementE2E:
    def test_exclude_then_lookup_returns_no_match(self, store_with_patterns):
        pattern_id = list(store_with_patterns._patterns.keys())[0]
        target_path = "src/module/foo.py"
        before = store_with_patterns._patterns[pattern_id].before_snippet
        rule = store_with_patterns._patterns[pattern_id].rule

        rc = _patterns_exclude([pattern_id, target_path], store_with_patterns)
        assert rc == 0
        match = store_with_patterns.lookup_structural(
            rule,
            before,
            "python",
            target_path=target_path,
        )
        assert match is None


class TestIntegration:
    def test_list_reads_from_jsonl_file(self, tmp_path, capsys):
        store_path = tmp_path / "repos" / "myrepo" / "state" / "fix_patterns.jsonl"
        store = FixPatternStore(store_path)
        pid = store.append(make_pattern(pattern_id="", confidence=0.75))
        del store

        store2 = FixPatternStore(store_path)
        rc = _patterns_list(store2)
        assert rc == 0
        out = capsys.readouterr().out
        assert "broad-except" in out
        assert "Total: 1 active patterns" in out

    def test_show_displays_diff_patch(self, tmp_path, capsys):
        store_path = tmp_path / "repos" / "myrepo" / "state" / "fix_patterns.jsonl"
        store = FixPatternStore(store_path)
        pid = store.append(make_pattern(pattern_id="", confidence=0.9))
        del store

        store2 = FixPatternStore(store_path)
        rc = _patterns_show([pid], store2)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Diff patch:" in out
        assert "--- a/src/file.py" in out
