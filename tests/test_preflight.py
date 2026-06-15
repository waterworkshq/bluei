"""Tests for the preflight module and onboarding gate."""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestRunPreflight:
    """Tests for bluei.app.preflight.run_preflight()"""

    def test_preflight_returns_result_for_python_repo(self, tmp_path):
        """A Python repo should detect python language and check for ruff."""
        from bluei.app.preflight import run_preflight

        (tmp_path / "main.py").write_text("print('hello')\n")

        with patch("shutil.which", return_value="/usr/bin/ruff"):
            result = run_preflight(tmp_path, run_validation=False)

        assert result.language == "python"
        assert len(result.tool_checks) > 0
        # At least one tool should be checked
        tool_names = [tc.tool for tc in result.tool_checks]
        assert any(t in tool_names for t in ["ruff", "python3", "python"])

    def test_preflight_reports_missing_tools(self, tmp_path):
        """Missing tools should appear in missing_tools and make result not ready."""
        from bluei.app.preflight import run_preflight

        (tmp_path / "main.py").write_text("print('hello')\n")

        with patch("shutil.which", return_value=None):
            result = run_preflight(tmp_path, run_validation=False)

        assert len(result.missing_tools) > 0
        assert not result.ready

    def test_preflight_with_all_tools_present(self, tmp_path):
        """When all tools are found and no validation, result should be ready."""
        from bluei.app.preflight import run_preflight

        (tmp_path / "main.py").write_text("print('hello')\n")

        with patch("shutil.which", return_value="/usr/local/bin/fake"):
            result = run_preflight(tmp_path, run_validation=False)

        assert result.ready
        assert result.missing_tools == []

    def test_preflight_nonexistent_path(self, tmp_path):
        """Non-existent path should produce an error."""
        from bluei.app.preflight import run_preflight

        result = run_preflight(tmp_path / "nonexistent", run_validation=False)

        assert not result.ready
        assert len(result.errors) > 0
        assert "not a directory" in result.errors[0].lower()

    def test_preflight_runs_validation_commands(self, tmp_path):
        """When run_validation=True, validation commands should be executed."""
        from bluei.app.preflight import run_preflight, ValidationCheck

        (tmp_path / "main.py").write_text("print('hello')\n")

        def fake_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("shutil.which", return_value="/usr/bin/ruff"):
            with patch.object(subprocess, "run", side_effect=fake_run):
                result = run_preflight(tmp_path, run_validation=True)

        # Should have attempted some validation commands
        assert isinstance(result.validation_checks, list)
        for vc in result.validation_checks:
            assert isinstance(vc, ValidationCheck)

    def test_preflight_validation_failure_makes_not_ready(self, tmp_path):
        """Failed validation commands should make the result not ready."""
        from bluei.app.preflight import run_preflight

        (tmp_path / "main.py").write_text("print('hello')\n")

        def fake_run(cmd, **kwargs):
            return MagicMock(returncode=1, stdout="", stderr="error")

        with patch("shutil.which", return_value="/usr/bin/ruff"):
            with patch(
                "bluei.app.preflight.infer_baseline_checks",
                return_value=[["ruff", "check"], ["pytest"]],
            ):
                with patch.object(subprocess, "run", side_effect=fake_run):
                    result = run_preflight(tmp_path, run_validation=True)

        assert len(result.failed_validations) > 0
        assert not result.ready

    def test_preflight_validation_timeout(self, tmp_path):
        """Timed-out validation should be reported as failed."""
        from bluei.app.preflight import run_preflight

        (tmp_path / "main.py").write_text("print('hello')\n")

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

        with patch("shutil.which", return_value="/usr/bin/ruff"):
            with patch(
                "bluei.app.preflight.infer_baseline_checks", return_value=[["pytest"]]
            ):
                with patch.object(subprocess, "run", side_effect=fake_run):
                    result = run_preflight(tmp_path, run_validation=True)

        assert any(
            "timed out" in vc.stderr_snippet.lower() for vc in result.validation_checks
        )


class TestPreflightResult:
    """Tests for the PreflightResult dataclass."""

    def test_ready_property_all_clear(self):
        from bluei.app.preflight import PreflightResult, ToolCheck, ValidationCheck

        result = PreflightResult(
            repo_path="/tmp",
            tool_checks=[ToolCheck(tool="ruff", found=True, path="/usr/bin/ruff")],
            validation_checks=[ValidationCheck(command=["ruff", "check"], passed=True)],
        )
        assert result.ready

    def test_ready_property_missing_tool(self):
        from bluei.app.preflight import PreflightResult, ToolCheck

        result = PreflightResult(
            repo_path="/tmp",
            tool_checks=[ToolCheck(tool="ruff", found=False)],
        )
        assert not result.ready
        assert "ruff" in result.missing_tools

    def test_ready_property_with_errors(self):
        from bluei.app.preflight import PreflightResult

        result = PreflightResult(
            repo_path="/tmp",
            errors=["Something went wrong"],
        )
        assert not result.ready


class TestOnboardPreflightGate:
    """Tests for D6: preflight gate in OnboardEngine.onboard()"""

    def test_onboard_aborts_when_tools_missing(self, tmp_path):
        """onboard() should raise ValueError when preflight finds missing tools."""
        from bluei.app.onboarding import OnboardEngine, OnboardOptions
        from bluei.app.config import ConfigManager
        from bluei.app.registry import RepoRegistry
        from bluei.app.health import HealthEngine
        from bluei.app.state import StateManager
        from bluei.app.plugins import PluginLoader

        (tmp_path / "main.py").write_text("print('hello')\n")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config_mgr = ConfigManager(workspace)
        engine = OnboardEngine(
            RepoRegistry(config_mgr),
            PluginLoader(workspace / "plugins"),
            HealthEngine(),
            StateManager(config_mgr.repos_dir),
        )

        options = OnboardOptions(name="test-repo")

        with patch("shutil.which", return_value=None):
            with patch.object(engine, "select_plugin", return_value="plugin-python"):
                try:
                    engine.onboard(tmp_path, options)
                    assert False, "Should have raised ValueError"
                except ValueError as exc:
                    assert (
                        "preflight" in str(exc).lower()
                        or "missing tools" in str(exc).lower()
                    )

    def test_onboard_skip_preflight_allows_missing_tools(self, tmp_path):
        """onboard() with skip_preflight=True should proceed despite missing tools."""
        from bluei.app.onboarding import OnboardEngine, OnboardOptions
        from bluei.app.config import ConfigManager
        from bluei.app.registry import RepoRegistry
        from bluei.app.health import HealthEngine
        from bluei.app.state import StateManager
        from bluei.app.plugins import PluginLoader

        (tmp_path / "main.py").write_text("print('hello')\n")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config_mgr = ConfigManager(workspace)
        engine = OnboardEngine(
            RepoRegistry(config_mgr),
            PluginLoader(workspace / "plugins"),
            HealthEngine(),
            StateManager(config_mgr.repos_dir),
        )

        options = OnboardOptions(name="test-repo", skip_preflight=True)

        # Should NOT raise about missing tools (might fail later for other reasons,
        # but the preflight ValueError should not fire)
        with patch("shutil.which", return_value=None):
            try:
                engine.onboard(tmp_path, options)
            except ValueError as exc:
                # If it raises, it should NOT be about preflight/missing tools
                assert "preflight" not in str(exc).lower()
                assert "missing tools" not in str(exc).lower()
            except Exception:
                pass  # Other errors are OK — we only care that preflight didn't block
