"""Tests for bluei.engine.git_utils — git helpers and docs index management."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


class TestGitLastCommitForPath:
    """Tests for _git_last_commit_for_path."""

    def test_git_last_commit_for_path_success(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        with patch("bluei.engine.git_utils.run_capture", return_value=(0, "abc123\n")):
            result = git_utils._git_last_commit_for_path(tmp_path, "src/a.py")
        assert result == "abc123"

    def test_git_last_commit_for_path_failure(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        with patch("bluei.engine.git_utils.run_capture", return_value=(1, "")):
            result = git_utils._git_last_commit_for_path(tmp_path, "src/a.py")
        assert result == ""


class TestCodePathsForDocsIndex:
    """Tests for _code_paths_for_docs_index."""

    def test_with_src_dir(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        src_dir = tmp_path / "src" / "qa_sandbox"
        src_dir.mkdir(parents=True)
        (src_dir / "module.py").write_text("pass", encoding="utf-8")
        (src_dir / "helper.py").write_text("pass", encoding="utf-8")
        result = git_utils._code_paths_for_docs_index(tmp_path)
        assert len(result) == 2

    def test_no_src_dir(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        result = git_utils._code_paths_for_docs_index(tmp_path)
        assert result == []

    def test_with_legacy_price(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        (tmp_path / "price.py").write_text("pass", encoding="utf-8")
        result = git_utils._code_paths_for_docs_index(tmp_path)
        assert len(result) == 1
        assert result[0].name == "price.py"


class TestHasInlineDoc:
    """Tests for _has_inline_doc."""

    def test_module_docstring(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        py_file = tmp_path / "mod.py"
        py_file.write_text('"""Module doc."""\n', encoding="utf-8")
        assert git_utils._has_inline_doc(py_file) is True

    def test_function_docstring(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        py_file = tmp_path / "mod.py"
        py_file.write_text("def foo():\n    '''doc'''\n    pass\n", encoding="utf-8")
        assert git_utils._has_inline_doc(py_file) is True

    def test_class_docstring(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        py_file = tmp_path / "mod.py"
        py_file.write_text("class Foo:\n    '''doc'''\n    pass\n", encoding="utf-8")
        assert git_utils._has_inline_doc(py_file) is True

    def test_async_function(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        py_file = tmp_path / "mod.py"
        py_file.write_text(
            "async def foo():\n    '''doc'''\n    pass\n", encoding="utf-8"
        )
        assert git_utils._has_inline_doc(py_file) is True

    def test_no_docstring(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        py_file = tmp_path / "mod.py"
        py_file.write_text("def foo():\n    pass\n", encoding="utf-8")
        assert git_utils._has_inline_doc(py_file) is False

    def test_bad_syntax(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        py_file = tmp_path / "mod.py"
        py_file.write_text("def foo(:\n", encoding="utf-8")
        assert git_utils._has_inline_doc(py_file) is False

    def test_read_error(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        missing = tmp_path / "nonexistent.py"
        assert git_utils._has_inline_doc(missing) is False


class TestExternalDocText:
    """Tests for _external_doc_text."""

    def test_readme(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        (tmp_path / "README.md").write_text(
            "some text about qa sandbox", encoding="utf-8"
        )
        result = git_utils._external_doc_text(tmp_path)
        assert "qa sandbox" in result

    def test_with_docs_dir(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (tmp_path / "README.md").write_text("readme", encoding="utf-8")
        (docs_dir / "guide.md").write_text("guide text", encoding="utf-8")
        (docs_dir / "api.md").write_text("api text", encoding="utf-8")
        result = git_utils._external_doc_text(tmp_path)
        assert "guide text" in result
        assert "api text" in result

    def test_no_docs(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        result = git_utils._external_doc_text(tmp_path)
        assert result == ""

    def test_read_error(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        (tmp_path / "README.md").write_text("ok", encoding="utf-8")
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        bad_file = docs_dir / "bad.md"
        bad_file.write_text("ok", encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=PermissionError("no")):
            result = git_utils._external_doc_text(tmp_path)
        assert isinstance(result, str)


class TestRefreshDocsIndex:
    """Tests for refresh_docs_index."""

    def test_covered(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        src_dir = tmp_path / "src" / "qa_sandbox"
        src_dir.mkdir(parents=True)
        py_file = src_dir / "module.py"
        py_file.write_text('"""Docstring."""\n', encoding="utf-8")
        (tmp_path / "README.md").write_text(
            "qa_sandbox.module is great", encoding="utf-8"
        )

        docs_index = tmp_path / "output" / "docs_index.json"
        log_file = tmp_path / "log.txt"

        with patch("bluei.engine.git_utils.run_capture", return_value=(0, "sha123")):
            with patch("bluei.engine.state._append_text"):
                entries = git_utils.refresh_docs_index(tmp_path, docs_index, log_file)
        assert len(entries) == 1
        assert entries[0]["coverage_status"] == "covered"
        assert docs_index.exists()
        data = json.loads(docs_index.read_text(encoding="utf-8"))
        assert "entries" in data

    def test_inline_only(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        src_dir = tmp_path / "src" / "qa_sandbox"
        src_dir.mkdir(parents=True)
        py_file = src_dir / "module.py"
        py_file.write_text('"""Docstring."""\n', encoding="utf-8")

        docs_index = tmp_path / "output" / "docs_index.json"
        log_file = tmp_path / "log.txt"

        with patch("bluei.engine.git_utils.run_capture", return_value=(0, "sha123")):
            with patch("bluei.engine.state._append_text"):
                entries = git_utils.refresh_docs_index(tmp_path, docs_index, log_file)
        assert entries[0]["coverage_status"] == "inline-only"

    def test_external_only(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        src_dir = tmp_path / "src" / "qa_sandbox"
        src_dir.mkdir(parents=True)
        py_file = src_dir / "module.py"
        py_file.write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "README.md").write_text(
            "qa_sandbox.module is mentioned", encoding="utf-8"
        )

        docs_index = tmp_path / "output" / "docs_index.json"
        log_file = tmp_path / "log.txt"

        with patch("bluei.engine.git_utils.run_capture", return_value=(0, "sha123")):
            with patch("bluei.engine.state._append_text"):
                entries = git_utils.refresh_docs_index(tmp_path, docs_index, log_file)
        assert entries[0]["coverage_status"] == "external-only"

    def test_uncovered(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        src_dir = tmp_path / "src" / "qa_sandbox"
        src_dir.mkdir(parents=True)
        py_file = src_dir / "module.py"
        py_file.write_text("x = 1\n", encoding="utf-8")

        docs_index = tmp_path / "output" / "docs_index.json"
        log_file = tmp_path / "log.txt"

        with patch("bluei.engine.git_utils.run_capture", return_value=(0, "sha123")):
            with patch("bluei.engine.state._append_text"):
                entries = git_utils.refresh_docs_index(tmp_path, docs_index, log_file)
        assert entries[0]["coverage_status"] == "uncovered"

    def test_empty(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        docs_index = tmp_path / "output" / "docs_index.json"
        log_file = tmp_path / "log.txt"

        with patch("bluei.engine.state._append_text"):
            entries = git_utils.refresh_docs_index(tmp_path, docs_index, log_file)
        assert entries == []
        assert docs_index.exists()
        data = json.loads(docs_index.read_text(encoding="utf-8"))
        assert "entries" in data

    def test_with_legacy_price(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        (tmp_path / "price.py").write_text('"""Price logic."""\n', encoding="utf-8")
        (tmp_path / "README.md").write_text("price.py is documented", encoding="utf-8")

        docs_index = tmp_path / "output" / "docs_index.json"
        log_file = tmp_path / "log.txt"

        with patch("bluei.engine.git_utils.run_capture", return_value=(0, "sha456")):
            with patch("bluei.engine.state._append_text"):
                entries = git_utils.refresh_docs_index(tmp_path, docs_index, log_file)
        assert len(entries) == 1
        assert entries[0]["code_path"] == "price.py"
        assert entries[0]["coverage_status"] == "covered"


class TestLoadDocsIndex:
    """Tests for load_docs_index."""

    def test_file_not_found(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        result = git_utils.load_docs_index(tmp_path / "nonexistent.json")
        assert result == []

    def test_valid_list(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        index_file = tmp_path / "index.json"
        data = [{"code_path": "a.py", "status": "covered"}]
        index_file.write_text(json.dumps(data), encoding="utf-8")
        result = git_utils.load_docs_index(index_file)
        assert len(result) == 1

    def test_valid_dict(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        index_file = tmp_path / "index.json"
        data = {"entries": [{"code_path": "a.py"}]}
        index_file.write_text(json.dumps(data), encoding="utf-8")
        result = git_utils.load_docs_index(index_file)
        assert len(result) == 1

    def test_dict_no_entries(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        index_file = tmp_path / "index.json"
        index_file.write_text(json.dumps({"other": "data"}), encoding="utf-8")
        result = git_utils.load_docs_index(index_file)
        assert result == []

    def test_bad_type(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        index_file = tmp_path / "index.json"
        index_file.write_text("42", encoding="utf-8")
        result = git_utils.load_docs_index(index_file)
        assert result == []

    def test_bad_json(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        index_file = tmp_path / "index.json"
        index_file.write_text("not json", encoding="utf-8")
        result = git_utils.load_docs_index(index_file)
        assert result == []

    def test_filters_non_dicts(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        index_file = tmp_path / "index.json"
        data = [{"a": 1}, "bad", 42, {"b": 2}]
        index_file.write_text(json.dumps(data), encoding="utf-8")
        result = git_utils.load_docs_index(index_file)
        assert len(result) == 2

    def test_non_list_entries(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        index_file = tmp_path / "index.json"
        data = {"entries": "not a list"}
        index_file.write_text(json.dumps(data), encoding="utf-8")
        result = git_utils.load_docs_index(index_file)
        assert result == []


class TestGetBranch:
    """Tests for get_branch."""

    def test_direct(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        with patch("subprocess.check_output", return_value="main\n"):
            result = git_utils.get_branch(tmp_path)
        assert result == "main"

    def test_with_config_yaml(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        real_repo = tmp_path / "real-repo"
        real_repo.mkdir()
        config = tmp_path / "config.yaml"
        config.write_text(f"path: {real_repo}\n", encoding="utf-8")

        with patch("subprocess.check_output", return_value="feature-branch\n"):
            result = git_utils.get_branch(tmp_path)
        assert result == "feature-branch"

    def test_config_nonexistent_path(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        config = tmp_path / "config.yaml"
        config.write_text("path: /nonexistent/path\n", encoding="utf-8")

        with patch("subprocess.check_output", return_value="main\n"):
            result = git_utils.get_branch(tmp_path)
        assert result == "main"

    def test_bad_config_yaml(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        config = tmp_path / "config.yaml"
        config.write_text(": bad yaml {{{\n", encoding="utf-8")

        with patch("subprocess.check_output", return_value="main\n"):
            result = git_utils.get_branch(tmp_path)
        assert result == "main"

    def test_no_config(self, tmp_path: Path) -> None:
        from bluei.engine import git_utils

        with patch("subprocess.check_output", return_value="develop\n"):
            result = git_utils.get_branch(tmp_path)
        assert result == "develop"
