import pytest
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from bluei.engine.constants import (
    _load_bluei_registry,
    repo_findings_path,
    repo_patterns_path,
    repo_docs_index_path,
    repo_issues_path,
    load_llm_fixable_rules,
)


class TestLoadBlueiRegistryCorruptJson:
    def test_corrupt_json_returns_empty_repos(self, tmp_path):
        registry_file = tmp_path / "registry.json"
        registry_file.write_text("{{invalid json!!", encoding="utf-8")
        with patch(
            "bluei.engine.constants.bluei_registry_path", return_value=registry_file
        ):
            result = _load_bluei_registry()
        assert result == {"repos": {}}

    def test_oserror_on_read_returns_empty_repos(self, tmp_path):
        registry_file = tmp_path / "registry.json"
        registry_file.write_text('{"repos": {}}', encoding="utf-8")
        with patch(
            "bluei.engine.constants.bluei_registry_path", return_value=registry_file
        ):
            with patch.object(
                Path, "read_text", side_effect=OSError("permission denied")
            ):
                result = _load_bluei_registry()
        assert result == {"repos": {}}

    def test_nonexistent_file_returns_empty_repos(self, tmp_path):
        registry_file = tmp_path / "nonexistent.json"
        with patch(
            "bluei.engine.constants.bluei_registry_path", return_value=registry_file
        ):
            result = _load_bluei_registry()
        assert result == {"repos": {}}

    def test_valid_json_loads_normally(self, tmp_path):
        registry_file = tmp_path / "registry.json"
        data = {"repos": {"/some/path": {"slug": "some-path"}}}
        registry_file.write_text(json.dumps(data), encoding="utf-8")
        with patch(
            "bluei.engine.constants.bluei_registry_path", return_value=registry_file
        ):
            result = _load_bluei_registry()
        assert result == data


class TestRepoPathFunctions:
    @pytest.fixture(autouse=True)
    def _mock_bluei_repo_dir(self, tmp_path):
        repo_dir = tmp_path / "repos" / "test-repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        self._repo_dir = repo_dir
        with patch("bluei.engine.constants.bluei_repo_dir", return_value=repo_dir):
            yield

    def test_repo_findings_path(self):
        result = repo_findings_path(Path("/some/repo"))
        assert result == self._repo_dir / "findings.jsonl"

    def test_repo_patterns_path(self):
        result = repo_patterns_path(Path("/some/repo"))
        assert result == self._repo_dir / "fix_patterns.jsonl"

    def test_repo_docs_index_path(self):
        result = repo_docs_index_path(Path("/some/repo"))
        assert result == self._repo_dir / "docs_index.json"

    def test_repo_issues_path(self):
        result = repo_issues_path(Path("/some/repo"))
        assert result == self._repo_dir / "issues.json"


class TestLoadLlmFixableRulesMissingFile:
    def test_missing_yaml_returns_empty_dict(self, tmp_path):
        import bluei.engine.constants as mod

        mod._LLM_FIXABLE_RULES_CACHE = None
        fake_yaml = tmp_path / "nonexistent.yaml"
        with patch.object(
            Path, "parent", new_callable=lambda: property(lambda self: tmp_path)
        ):
            with patch.object(Path, "exists", return_value=False):
                result = load_llm_fixable_rules()
        assert result == {}
        mod._LLM_FIXABLE_RULES_CACHE = None


class TestLoadLlmFixableRulesImportError:
    def test_import_error_returns_inline_defaults(self):
        import bluei.engine.constants as mod

        mod._LLM_FIXABLE_RULES_CACHE = None
        fake_yaml = Path("/fake/llm_fixable_rules.yaml")

        def fake_parent_getter(self):
            return Path("/fake")

        with patch("bluei.engine.constants.Path.__truediv__", return_value=fake_yaml):
            with patch.object(Path, "exists", return_value=True):
                import builtins

                real_import = builtins.__import__

                def blocking_import(name, *args, **kwargs):
                    if name == "yaml":
                        raise ImportError("no yaml module")
                    return real_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=blocking_import):
                    result = load_llm_fixable_rules()

        assert "ruff-b904" in result
        assert "ruff-s311" in result
        assert result["ruff-b904"]["description"] == "raise without cause"
        assert result["ruff-s311"]["description"] == "stdlib random in security context"
        mod._LLM_FIXABLE_RULES_CACHE = None


class TestLoadLlmFixableRulesYamlException:
    def test_yaml_exception_returns_empty_dict(self):
        import bluei.engine.constants as mod

        mod._LLM_FIXABLE_RULES_CACHE = None

        with patch.object(
            Path, "parent", new_callable=lambda: property(lambda self: Path("/fake"))
        ):
            with patch.object(Path, "exists", return_value=True):
                with patch("builtins.open", side_effect=Exception("read failure")):
                    result = load_llm_fixable_rules()

        assert result == {}
        mod._LLM_FIXABLE_RULES_CACHE = None
