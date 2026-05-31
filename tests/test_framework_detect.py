"""Tests for bluei.engine.scaffold.framework_detect — test framework detection."""

import json

import pytest

from bluei.engine.scaffold.framework_detect import (
    detect_install_commands,
    detect_test_commands,
    detect_test_framework,
)


class TestDetectTestFramework:
    def test_javascript_returns_js_framework(self, tmp_path):
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({"devDependencies": {"jest": "^29.0.0"}}))
        assert detect_test_framework(tmp_path, "javascript") == "jest"

    def test_typescript_returns_js_framework(self, tmp_path):
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({"devDependencies": {"vitest": "^1.0.0"}}))
        assert detect_test_framework(tmp_path, "typescript") == "vitest"

    def test_go_returns_go_test(self, tmp_path):
        assert detect_test_framework(tmp_path, "go") == "go_test"

    def test_rust_returns_cargo_test(self, tmp_path):
        assert detect_test_framework(tmp_path, "rust") == "cargo_test"

    def test_python_default_is_pytest(self, tmp_path):
        assert detect_test_framework(tmp_path, "python") == "pytest"

    def test_unknown_lang_defaults_to_pytest(self, tmp_path):
        assert detect_test_framework(tmp_path) == "pytest"


class TestDetectPythonFramework:
    def test_pytest_ini(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        assert detect_test_framework(tmp_path) == "pytest"

    def test_pyproject_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
        assert detect_test_framework(tmp_path) == "pytest"

    def test_pyproject_with_pytest_string(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('dependencies = ["pytest"]\n')
        assert detect_test_framework(tmp_path) == "pytest"

    def test_setup_cfg_pytest(self, tmp_path):
        (tmp_path / "setup.cfg").write_text("[tool:pytest]\naddopts = -v\n")
        assert detect_test_framework(tmp_path) == "pytest"

    def test_conftest_py(self, tmp_path):
        (tmp_path / "conftest.py").write_text("import pytest\n")
        assert detect_test_framework(tmp_path) == "pytest"

    def test_unittest_cfg(self, tmp_path):
        (tmp_path / "unittest.cfg").write_text("[unittest]\n")
        assert detect_test_framework(tmp_path) == "unittest"

    def test_no_config_defaults_pytest(self, tmp_path):
        assert detect_test_framework(tmp_path) == "pytest"

    def test_pyproject_no_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'myproject'\n")
        assert detect_test_framework(tmp_path) == "pytest"


class TestDetectJsFramework:
    def test_vitest_in_deps(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"devDependencies": {"vitest": "^1.0.0"}})
        )
        assert detect_test_framework(tmp_path, "javascript") == "vitest"

    def test_jest_in_deps(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"dependencies": {"jest": "^29.0.0"}})
        )
        assert detect_test_framework(tmp_path, "javascript") == "jest"

    def test_mocha_in_deps(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"devDependencies": {"mocha": "^10.0.0"}})
        )
        assert detect_test_framework(tmp_path, "javascript") == "mocha"

    def test_jest_config_js(self, tmp_path):
        (tmp_path / "jest.config.js").write_text("module.exports = {}")
        assert detect_test_framework(tmp_path, "javascript") == "jest"

    def test_jest_config_ts(self, tmp_path):
        (tmp_path / "jest.config.ts").write_text("export default {}")
        assert detect_test_framework(tmp_path, "javascript") == "jest"

    def test_vitest_config_ts(self, tmp_path):
        (tmp_path / "vitest.config.ts").write_text("export default {}")
        assert detect_test_framework(tmp_path, "javascript") == "vitest"

    def test_vitest_config_js(self, tmp_path):
        (tmp_path / "vitest.config.js").write_text("export default {}")
        assert detect_test_framework(tmp_path, "javascript") == "vitest"

    def test_no_config_defaults_jest(self, tmp_path):
        assert detect_test_framework(tmp_path, "javascript") == "jest"

    def test_invalid_json_falls_back(self, tmp_path):
        (tmp_path / "package.json").write_text("NOT VALID JSON{{{")
        assert detect_test_framework(tmp_path, "javascript") == "jest"

    def test_empty_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        assert detect_test_framework(tmp_path, "javascript") == "jest"


class TestDetectInstallCommands:
    def test_pyproject_with_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\ndependencies = ['pytest']\n"
        )
        result = detect_install_commands(tmp_path)
        assert "pip install pytest" in result

    def test_pyproject_no_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
        assert detect_install_commands(tmp_path) == "pip install pytest"

    def test_requirements_dev(self, tmp_path):
        (tmp_path / "requirements-dev.txt").write_text("pytest\n")
        result = detect_install_commands(tmp_path)
        assert "pip install -r requirements-dev.txt" in result

    def test_setup_py(self, tmp_path):
        (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
        result = detect_install_commands(tmp_path)
        assert "pip install -e ." in result

    def test_no_files_defaults(self, tmp_path):
        assert detect_install_commands(tmp_path) == "pip install pytest"


class TestDetectTestCommands:
    def test_with_pytest_ini(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        assert detect_test_commands(tmp_path) == "pytest -q"

    def test_with_conftest(self, tmp_path):
        (tmp_path / "conftest.py").write_text("")
        assert detect_test_commands(tmp_path) == "pytest -q"

    def test_with_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        assert detect_test_commands(tmp_path) == "pytest -q"

    def test_nothing_defaults_pytest(self, tmp_path):
        assert detect_test_commands(tmp_path) == "pytest -q"

    def test_makefile_with_test(self, tmp_path):
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
        result = detect_test_commands(tmp_path)
        assert "pytest -q" in result
        assert "make test" in result

    def test_makefile_without_test(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        (tmp_path / "Makefile").write_text("build:\n\techo build\n")
        result = detect_test_commands(tmp_path)
        assert "make test" not in result
