import json
import subprocess
from unittest.mock import patch

import pytest

from bluei.engine.pattern_extractor import (
    extract,
    infer_language,
    _changed_files,
    _strip_diff_prefix,
    _snippets_from_unified_diff,
    _per_file_snippets,
    _compute_file_pattern,
    _detect_framework,
    _extract_composite,
    append_log,
)
from bluei.engine.pattern_store import FixPatternStore


def run(cmd, cwd):
    subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _pf(make_finding, **overrides):
    """Wrap conftest make_finding with pattern-extractor-specific defaults."""
    defaults = {
        "finding_id": "finding-1",
        "repo": "repo",
        "path": "src/example.py",
        "rule": "broad-except",
        "snippet": "except:",
        "confidence": 0.9,
    }
    defaults.update(overrides)
    return make_finding(**defaults)


@pytest.fixture
def store(tmp_path):
    return FixPatternStore(tmp_path / "state" / "fix_patterns.jsonl")


def commit_file(repo, relative_path, content):
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    run(["git", "add", relative_path], repo)
    run(["git", "commit", "-m", f"add {relative_path}"], repo)
    return path


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_extract_single_file_diff_stores_pattern_with_snippets(
    git_repo, store, tmp_path, make_finding
):
    path = commit_file(git_repo, "src/example.py", "except:\n    pass\n")
    path.write_text("except Exception:\n    pass\n", encoding="utf-8")

    pattern_id = extract(
        git_repo, _pf(make_finding), "claude", store, tmp_path / "run.log"
    )

    assert pattern_id is not None
    pattern = store.lookup("broad-except", "except:\n    pass")
    assert pattern is not None
    assert pattern.pattern_id == pattern_id
    assert pattern.before_snippet == "except:\n pass"
    assert pattern.after_snippet == "except Exception:\n pass"
    assert pattern.diff_patch.startswith("diff --git a/src/example.py b/src/example.py")
    assert pattern.source == "claude"
    assert pattern.file_path == "src/example.py"
    assert pattern.language == "python"
    assert pattern.source_finding_ids == ["finding-1"]


def test_normalization_dedents_mixed_indentation(
    git_repo, store, tmp_path, make_finding
):
    path = commit_file(
        git_repo, "src/example.py", "        if  ready:\n            return  old\n"
    )
    path.write_text("        if  ready:\n            return  new\n", encoding="utf-8")

    pattern_id = extract(
        git_repo,
        _pf(make_finding, rule="return-value"),
        "autofix",
        store,
        tmp_path / "run.log",
    )

    pattern = store.lookup("return-value", "if ready:\n return old")
    assert pattern_id == pattern.pattern_id
    assert pattern.before_snippet == "if ready:\n return old"
    assert pattern.after_snippet == "if ready:\n return new"


def test_normalization_strips_trailing_whitespace(
    git_repo, store, tmp_path, make_finding
):
    path = commit_file(git_repo, "src/example.py", "value = old   \n")
    path.write_text("value = new\t \n", encoding="utf-8")

    extract(
        git_repo,
        _pf(make_finding, rule="assignment"),
        "autofix",
        store,
        tmp_path / "run.log",
    )

    pattern = store.lookup("assignment", "value = old")
    assert pattern.before_snippet == "value = old"
    assert pattern.after_snippet == "value = new"


def test_normalization_collapses_multi_space(git_repo, store, tmp_path, make_finding):
    path = commit_file(git_repo, "src/example.py", "answer   =   old\n")
    path.write_text("answer   =   new\n", encoding="utf-8")

    extract(
        git_repo,
        _pf(make_finding, rule="spacing"),
        "autofix",
        store,
        tmp_path / "run.log",
    )

    pattern = store.lookup("spacing", "answer = old")
    assert pattern.before_snippet == "answer = old"
    assert pattern.after_snippet == "answer = new"


def test_deduplication_increments_success_count_for_same_rule_and_before(
    git_repo, store, tmp_path, make_finding
):
    path = commit_file(git_repo, "src/example.py", "except:\n    pass\n")
    path.write_text("except Exception:\n    pass\n", encoding="utf-8")

    first_id = extract(
        git_repo, _pf(make_finding), "claude", store, tmp_path / "run.log"
    )
    second_id = extract(
        git_repo,
        _pf(make_finding, finding_id="finding-2"),
        "claude",
        store,
        tmp_path / "run.log",
    )

    records = read_jsonl(store.store_path)
    assert second_id == first_id
    assert len(records) == 1
    assert records[0]["success_count"] == 2


def test_extract_skips_multi_file_diff(git_repo, store, tmp_path, make_finding):
    first = commit_file(git_repo, "src/one.py", "one = 1\n")
    second = commit_file(git_repo, "src/two.py", "two = 2\n")
    first.write_text("one = 10\n", encoding="utf-8")
    second.write_text("two = 20\n", encoding="utf-8")

    pattern_id = extract(
        git_repo,
        _pf(make_finding, path="src/one.py"),
        "autofix",
        store,
        tmp_path / "run.log",
    )

    assert pattern_id is not None
    assert pattern_id.startswith("comp-")


def test_extract_skips_empty_diff(git_repo, store, tmp_path, make_finding):
    commit_file(git_repo, "src/example.py", "unchanged = True\n")

    pattern_id = extract(
        git_repo, _pf(make_finding, rule="noop"), "autofix", store, tmp_path / "run.log"
    )

    assert pattern_id is None
    assert not store.store_path.exists()


def test_extract_returns_none_when_git_diff_fails(store, tmp_path, make_finding):
    pattern_id = extract(
        tmp_path / "missing-worktree",
        _pf(make_finding),
        "claude",
        store,
        tmp_path / "run.log",
    )

    assert pattern_id is None
    assert not store.store_path.exists()


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("a.py", "python"),
        ("a.ts", "typescript"),
        ("a.tsx", "typescript"),
        ("a.js", "javascript"),
        ("a.go", "go"),
        ("a.rs", "rust"),
        ("a.sh", "shell"),
    ],
)
def test_language_inferred_from_extension(path, language):
    assert infer_language(path) == language


def test_extract_logs_extraction_event(git_repo, store, tmp_path, make_finding):
    path = commit_file(git_repo, "src/example.py", "old = True\n")
    path.write_text("new = True\n", encoding="utf-8")
    log_file = tmp_path / "logs" / "run.log"

    pattern_id = extract(
        git_repo, _pf(make_finding, rule="rename"), "contextual", store, log_file
    )

    # Canonical append_log format is ``[ISO] message``. Substring match
    # keeps the assertion robust against timestamp variance.
    log_text = log_file.read_text(encoding="utf-8").strip()
    assert log_text.endswith(
        f"pattern-extract: rule=rename pattern_id={pattern_id} source=contextual"
    )
    assert log_text.startswith("[")


def test_integration_extract_from_real_git_worktree_writes_jsonl(
    git_repo, store, tmp_path, make_finding
):
    path = commit_file(git_repo, "src/example.py", 'name = "before"\n')
    path.write_text('name = "after"\n', encoding="utf-8")

    pattern_id = extract(
        git_repo,
        _pf(make_finding, rule="literal"),
        "claude",
        store,
        tmp_path / "run.log",
    )

    records = read_jsonl(store.store_path)
    assert len(records) == 1
    assert records[0]["pattern_id"] == pattern_id
    assert records[0]["before_snippet"] == 'name = "before"'
    assert records[0]["after_snippet"] == 'name = "after"'
    assert records[0]["diff_patch"].startswith(
        "diff --git a/src/example.py b/src/example.py"
    )


# ── Extracted from test_medium_modules_remaining.py ──
# Additional edge cases for diff parsing, extraction, and helper functions


class TestStripDiffPrefix:
    def test_no_prefix(self):
        assert _strip_diff_prefix("src/file.py") == "src/file.py"

    def test_a_prefix(self):
        assert _strip_diff_prefix("a/src/file.py") == "src/file.py"

    def test_b_prefix(self):
        assert _strip_diff_prefix("b/src/file.py") == "src/file.py"


class TestChangedFiles:
    def test_empty_diff(self):
        assert _changed_files("") == []

    def test_single_file(self):
        diff = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n"
        assert _changed_files(diff) == ["foo.py"]


class TestSnippetsFromUnifiedDiff:
    def test_multi_file_boundary(self):
        diff = (
            "diff --git a/one.py b/one.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/two.py b/two.py\n"
            "@@ -1 +1 @@\n"
            "-foo\n"
            "+bar\n"
        )
        before, after = _snippets_from_unified_diff(diff)
        assert "old" in before
        assert "new" in after
        assert "foo" in before
        assert "bar" in after

    def test_no_newline_marker(self):
        diff = "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n"
        before, after = _snippets_from_unified_diff(diff)
        assert "old" in before
        assert "new" in after


class TestComputeFilePattern:
    def test_shallow_path(self):
        assert _compute_file_pattern("file.py") == "*.py"

    def test_nested_path(self):
        assert _compute_file_pattern("src/utils/helpers.ts") == "src/**/*.ts"


class TestDetectFramework:
    def test_returns_first_detected_framework(self, tmp_path):
        assert _detect_framework(tmp_path, detected_frameworks=["django"]) == "django"

    def test_returns_first_of_multiple(self, tmp_path):
        assert (
            _detect_framework(tmp_path, detected_frameworks=["django", "flask"])
            == "django"
        )

    def test_returns_none_for_empty_list(self, tmp_path):
        assert _detect_framework(tmp_path, detected_frameworks=[]) is None

    def test_returns_none_when_omitted(self, tmp_path):
        # Backward-compat: omitting the parameter returns None.
        assert _detect_framework(tmp_path) is None
        assert _detect_framework(tmp_path, detected_frameworks=None) is None

    def test_detect_framework_does_not_import_from_app(self):
        """Verify pattern_extractor no longer reaches into app layer.

        Regression for rec-08: the lazy ``from bluei.app.onboarding``
        import inside ``_detect_framework`` was paid down via
        parameterization. If this assertion fires, someone reintroduced
        the engine→app dependency.
        """
        import inspect

        import bluei.engine.pattern_extractor as mod

        src = inspect.getsource(mod)
        assert "from bluei.app.onboarding" not in src, (
            "pattern_extractor should not import from bluei.app.onboarding (rec-08)"
        )


class TestExtractGitDiffFail:
    def test_nonzero_rc(self, tmp_path, make_finding):
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        finding = _pf(make_finding)
        log_file = tmp_path / "run.log"
        with patch("bluei.engine.pattern_extractor.run_capture", return_value=(1, "")):
            result = extract(tmp_path, finding, "autofix", store, log_file)
        assert result is None
        assert "git_diff_failed" in log_file.read_text()


class TestExtractNoFilesInDiff:
    def test_diff_without_file_headers(self, tmp_path, make_finding):
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        finding = _pf(make_finding)
        log_file = tmp_path / "run.log"
        diff_output = "some content without diff headers"
        with patch(
            "bluei.engine.pattern_extractor.run_capture", return_value=(0, diff_output)
        ):
            result = extract(tmp_path, finding, "autofix", store, log_file)
        assert result is None
        assert "no_files_in_diff" in log_file.read_text()


class TestExtractNoSnippetChange:
    def test_identical_before_after(self, tmp_path, make_finding):
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        finding = _pf(make_finding)
        log_file = tmp_path / "run.log"
        diff_output = (
            "diff --git a/test.py b/test.py\n"
            "--- a/test.py\n"
            "+++ b/test.py\n"
            "@@ -1 +1 @@\n"
            " same_line\n"
        )
        with patch(
            "bluei.engine.pattern_extractor.run_capture", return_value=(0, diff_output)
        ):
            result = extract(tmp_path, finding, "autofix", store, log_file)
        assert result is None
        assert "no_snippet_change" in log_file.read_text()


class TestExtractException:
    def test_exception_during_extract(self, tmp_path, make_finding):
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        finding = _pf(make_finding)
        log_file = tmp_path / "run.log"
        with patch(
            "bluei.engine.pattern_extractor.run_capture",
            side_effect=RuntimeError("boom"),
        ):
            result = extract(tmp_path, finding, "autofix", store, log_file)
        assert result is None
        assert "exception" in log_file.read_text()

    def test_exception_and_log_fails(self, tmp_path, make_finding):
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        finding = _pf(make_finding)
        log_file = tmp_path / "run.log"
        with patch(
            "bluei.engine.pattern_extractor.run_capture",
            side_effect=RuntimeError("boom"),
        ):
            with patch(
                "bluei.engine.pattern_extractor.append_log",
                side_effect=OSError("log fail"),
            ):
                result = extract(tmp_path, finding, "autofix", store, log_file)
        assert result is None


class TestExtractCompositeReturnsNone:
    def test_multi_file_diff_composite_fails(self, tmp_path, make_finding):
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        finding = _pf(make_finding, path="src/one.py")
        log_file = tmp_path / "run.log"
        diff_output = (
            "diff --git a/src/one.py b/src/one.py\n"
            "--- a/src/one.py\n+++ b/src/one.py\n"
            "@@ -1 +1 @@\n-old\n+new\n"
            "diff --git a/src/two.py b/src/two.py\n"
            "--- a/src/two.py\n+++ b/src/two.py\n"
            "@@ -1 +1 @@\n-foo\n+bar\n"
        )
        with patch(
            "bluei.engine.pattern_extractor.run_capture", return_value=(0, diff_output)
        ):
            with patch(
                "bluei.engine.pattern_extractor._extract_composite", return_value=None
            ):
                result = extract(tmp_path, finding, "autofix", store, log_file)
        assert result is None


class TestPerFileSnippets:
    def test_multi_file(self):
        diff = (
            "diff --git a/one.py b/one.py\n"
            "--- a/one.py\n+++ b/one.py\n"
            "@@ -1 +1 @@\n-old1\n+new1\n"
            "diff --git a/two.py b/two.py\n"
            "--- a/two.py\n+++ b/two.py\n"
            "@@ -1 +1 @@\n-old2\n+new2\n"
        )
        result = _per_file_snippets(diff)
        assert "one.py" in result
        assert "two.py" in result

    def test_short_header_line(self):
        diff = "diff --git a\n"
        result = _per_file_snippets(diff)
        assert len(result) == 0


class TestExtractCompositeEdgeCases:
    def test_no_file_snippets(self, tmp_path, make_finding):
        finding = _pf(make_finding)
        log_file = tmp_path / "run.log"
        with patch(
            "bluei.engine.pattern_extractor._per_file_snippets", return_value={}
        ):
            result = _extract_composite(
                tmp_path,
                finding,
                "diff text",
                ["f.py"],
                "autofix",
                log_file,
            )
        assert result is None

    def test_snippets_none_for_file(self, tmp_path, make_finding):
        finding = _pf(make_finding)
        log_file = tmp_path / "run.log"
        with patch(
            "bluei.engine.pattern_extractor._per_file_snippets",
            return_value={"other.py": ("a", "b")},
        ):
            result = _extract_composite(
                tmp_path,
                finding,
                "diff text",
                ["f.py"],
                "autofix",
                log_file,
            )
        assert result is None

    def test_no_snippet_change_in_all_files(self, tmp_path, make_finding):
        finding = _pf(make_finding, path="test.py")
        log_file = tmp_path / "run.log"
        same = "x = 1"
        with patch(
            "bluei.engine.pattern_extractor._per_file_snippets",
            return_value={"test.py": (same, same)},
        ):
            with patch(
                "bluei.engine.pattern_extractor.normalize_snippet",
                side_effect=lambda x: x,
            ):
                result = _extract_composite(
                    tmp_path,
                    finding,
                    "diff text",
                    ["test.py"],
                    "autofix",
                    log_file,
                )
        assert result is None

    def test_exception_handled(self, tmp_path, make_finding):
        finding = _pf(make_finding)
        log_file = tmp_path / "run.log"
        with patch(
            "bluei.engine.pattern_extractor._per_file_snippets",
            side_effect=RuntimeError("boom"),
        ):
            result = _extract_composite(
                tmp_path,
                finding,
                "diff text",
                ["f.py"],
                "autofix",
                log_file,
            )
        assert result is None
        assert "composite-extract-skip" in log_file.read_text()

    def test_exception_and_log_write_fails(self, tmp_path, make_finding):
        finding = _pf(make_finding)
        log_file = tmp_path / "run.log"
        with patch(
            "bluei.engine.pattern_extractor._per_file_snippets",
            side_effect=RuntimeError("boom"),
        ):
            with patch(
                "bluei.engine.pattern_extractor.append_log",
                side_effect=OSError("log fail"),
            ):
                result = _extract_composite(
                    tmp_path,
                    finding,
                    "diff text",
                    ["f.py"],
                    "autofix",
                    log_file,
                )
        assert result is None


class TestInferLanguageEdgeCases:
    def test_unknown_extension(self):
        assert infer_language("file.xyz") == "xyz"

    def test_no_extension(self):
        assert infer_language("Makefile") == "unknown"


class TestAppendLog:
    def test_creates_parent_dirs(self, tmp_path):
        log_file = tmp_path / "deep" / "nested" / "run.log"
        append_log(log_file, "test message")
        assert log_file.exists()
        assert "test message" in log_file.read_text()
