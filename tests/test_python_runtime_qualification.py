"""Runtime-shadowing qualification for bare Python tool commands.

Real-subprocess tests (no models): when the environment repo owns a local
``.venv``/``venv`` copy of a bare python/pytest/ruff/mypy tool but PATH
resolves the bare name elsewhere, the check is recorded as unavailable
(runtime-shadowing) — never silently rewritten, never executed against the
foreign runtime. Explicit operator commands (any argv[0] containing ``/``,
incl. inferred absolute venv ``python -m`` invocations) are honored as
given. poetry/uv and unknown wrappers stay outside the guard.
"""

import shutil
import sys
from pathlib import Path

from bluei.engine.validation import RC_UNAVAILABLE, run_named_checks


def _write_tool(bin_dir: Path, name: str, marker: Path) -> Path:
    """Real executable stand-in that records a marker when it runs."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    tool = bin_dir / name
    tool.write_text(f"#!/bin/sh\nprintf x > '{marker}'\nexit 0\n")
    tool.chmod(0o755)
    return tool


def _venv_tool(repo: Path, name: str, venv_dir: str = ".venv") -> Path:
    tool = repo / venv_dir / "bin" / name
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    return tool


def _run(repo: Path, cmd, tmp_path: Path, environment_repo=None):
    return run_named_checks(
        repo,
        {"check": cmd},
        tmp_path / "log.txt",
        "baseline",
        environment_repo=environment_repo,
    )["check"]


class TestPathStandinExecution:
    def test_bare_tool_executes_when_no_venv(self, tmp_path, monkeypatch):
        marker = tmp_path / "ran"
        path_bin = _write_tool(tmp_path / "pathbin", "pytest", marker)
        monkeypatch.setenv("PATH", str(path_bin.parent))
        repo = tmp_path / "repo"
        repo.mkdir()
        result = _run(repo, ["pytest", "-q"], tmp_path)
        assert result["available"] is True
        assert result["rc"] == 0
        assert result["unavailable_reason"] is None
        assert marker.is_file()

    def test_local_venv_tool_on_path_is_not_shadowed(self, tmp_path, monkeypatch):
        marker = tmp_path / "ran"
        repo = tmp_path / "repo"
        repo.mkdir()
        venv_pytest = _write_tool(repo / ".venv" / "bin", "pytest", marker)
        monkeypatch.setenv("PATH", str(venv_pytest.parent))
        result = _run(repo, ["pytest", "-q"], tmp_path)
        assert result["available"] is True
        assert result["rc"] == 0
        assert marker.is_file()


class TestRuntimeShadowingUnavailable:
    def test_source_venv_shadows_bare_tool_via_environment_repo(
        self, tmp_path, monkeypatch
    ):
        marker = tmp_path / "ran"
        path_bin = _write_tool(tmp_path / "pathbin", "pytest", marker)
        monkeypatch.setenv("PATH", str(path_bin.parent))
        source = tmp_path / "source"
        source.mkdir()
        _venv_tool(source, "pytest")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result = _run(worktree, ["pytest", "-q"], tmp_path, environment_repo=source)
        assert result["available"] is False
        assert result["unavailable_reason"] == "runtime-shadowing"
        assert result["rc"] == RC_UNAVAILABLE
        assert result["cmd"] == ["pytest", "-q"]  # never rewritten
        assert not marker.is_file()  # foreign runtime never launched

    def test_default_environment_repo_is_cwd(self, tmp_path, monkeypatch):
        marker = tmp_path / "ran"
        path_bin = _write_tool(tmp_path / "pathbin", "pytest", marker)
        monkeypatch.setenv("PATH", str(path_bin.parent))
        repo = tmp_path / "repo"
        repo.mkdir()
        _venv_tool(repo, "pytest")
        result = _run(repo, ["pytest", "-q"], tmp_path)
        assert result["available"] is False
        assert result["unavailable_reason"] == "runtime-shadowing"

    def test_venv_dir_variant_also_shadows(self, tmp_path, monkeypatch):
        marker = tmp_path / "ran"
        path_bin = _write_tool(tmp_path / "pathbin", "pytest", marker)
        monkeypatch.setenv("PATH", str(path_bin.parent))
        source = tmp_path / "source"
        source.mkdir()
        _venv_tool(source, "pytest", venv_dir="venv")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result = _run(worktree, ["pytest", "-q"], tmp_path, environment_repo=source)
        assert result["available"] is False
        assert result["unavailable_reason"] == "runtime-shadowing"

    def test_bare_python_shadowed_before_workload_qualification(
        self, tmp_path, monkeypatch
    ):
        marker = tmp_path / "ran"
        path_bin = _write_tool(tmp_path / "pathbin", "python3", marker)
        monkeypatch.setenv("PATH", str(path_bin.parent))
        source = tmp_path / "source"
        source.mkdir()
        _venv_tool(source, "python3")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result = _run(
            worktree, ["python3", "-c", "print(1)"], tmp_path,
            environment_repo=source,
        )
        assert result["available"] is False
        assert result["unavailable_reason"] == "runtime-shadowing"
        assert not marker.is_file()

    def test_empty_declared_venv_refuses_host_bare_tool(self, tmp_path, monkeypatch):
        # Runtime ambiguity: even an empty declared .venv/bin refuses a bare
        # tool resolving outside it (conservative fail-closed intent).
        marker = tmp_path / "ran"
        path_bin = _write_tool(tmp_path / "pathbin", "pytest", marker)
        monkeypatch.setenv("PATH", str(path_bin.parent))
        source = tmp_path / "source"
        source.mkdir()
        (source / ".venv" / "bin").mkdir(parents=True)  # declared, empty
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result = _run(worktree, ["pytest", "-q"], tmp_path, environment_repo=source)
        assert result["available"] is False
        assert result["unavailable_reason"] == "runtime-shadowing"
        assert not marker.is_file()

    def test_venv_without_bin_dir_refuses_host(self, tmp_path, monkeypatch):
        # Legacy bootstrapped venv: .venv root exists with no bin/ — still a
        # declared runtime (lexists on the root, broken symlinks included).
        marker = tmp_path / "ran"
        path_bin = _write_tool(tmp_path / "pathbin", "pytest", marker)
        monkeypatch.setenv("PATH", str(path_bin.parent))
        source = tmp_path / "source"
        source.mkdir()
        (source / ".venv").mkdir()  # no bin/ inside
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result = _run(worktree, ["pytest", "-q"], tmp_path, environment_repo=source)
        assert result["available"] is False
        assert result["unavailable_reason"] == "runtime-shadowing"
        assert not marker.is_file()

    def test_broken_python_symlink_venv_refuses_host(self, tmp_path, monkeypatch):
        # Legacy venv with a dangling .venv/bin/python still declares the
        # runtime — no symlink resolution, no entry scanning.
        marker = tmp_path / "ran"
        path_bin = _write_tool(tmp_path / "pathbin", "pytest", marker)
        monkeypatch.setenv("PATH", str(path_bin.parent))
        source = tmp_path / "source"
        source.mkdir()
        link = source / ".venv" / "bin" / "python"
        link.parent.mkdir(parents=True)
        link.symlink_to(tmp_path / "nonexistent-interpreter")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result = _run(worktree, ["pytest", "-q"], tmp_path, environment_repo=source)
        assert result["available"] is False
        assert result["unavailable_reason"] == "runtime-shadowing"
        assert not marker.is_file()


class TestPythonOnlyVenvShadow:
    """A declared venv bin dir — python-only, empty, or broken — owns the
    bare-tool runtime; foreign host tools are refused, never substituted."""

    def test_python_only_venv_shadows_host_bare_pytest(self, tmp_path, monkeypatch):
        marker = tmp_path / "ran"
        path_bin = _write_tool(tmp_path / "pathbin", "pytest", marker)
        monkeypatch.setenv("PATH", str(path_bin.parent))
        source = tmp_path / "source"
        source.mkdir()
        _venv_tool(source, "python")  # no local pytest wrapper at all
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result = _run(worktree, ["pytest", "-q"], tmp_path, environment_repo=source)
        assert result["available"] is False
        assert result["unavailable_reason"] == "runtime-shadowing"
        assert not marker.is_file()

    def test_bare_python3_blocked_when_venv_has_only_python(
        self, tmp_path, monkeypatch
    ):
        marker = tmp_path / "ran"
        path_bin = _write_tool(tmp_path / "pathbin", "python3", marker)
        monkeypatch.setenv("PATH", str(path_bin.parent))
        source = tmp_path / "source"
        source.mkdir()
        _venv_tool(source, "python")  # venv lacks a python3 entry
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result = _run(
            worktree, ["python3", "-c", "print(1)"], tmp_path,
            environment_repo=source,
        )
        assert result["available"] is False
        assert result["unavailable_reason"] == "runtime-shadowing"
        assert not marker.is_file()

    def test_venv_symlinked_python_distinct_from_host(self, tmp_path, monkeypatch):
        # .venv/bin/python3 -> host interpreter: Path.resolve() would call
        # both "the same binary", but the venv launch changes sys.prefix —
        # identity is the lexical bin dir, so the host path still shadows.
        source = tmp_path / "source"
        source.mkdir()
        link = source / ".venv" / "bin" / "python3"
        link.parent.mkdir(parents=True)
        link.symlink_to(sys.executable)
        real_which = shutil.which

        def fake_which(name, *args, **kwargs):
            return str(sys.executable) if name == "python3" else real_which(
                name, *args, **kwargs
            )

        monkeypatch.setattr(shutil, "which", fake_which)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result = _run(
            worktree, ["python3", "-c", "print(1)"], tmp_path,
            environment_repo=source,
        )
        assert result["available"] is False
        assert result["unavailable_reason"] == "runtime-shadowing"

    def test_absolute_python_with_local_venv_no_error(self, tmp_path):
        # Explicit absolute interpreter is honored as given: the guard must
        # not run shutil.which on it (would TypeError) nor shadow it.
        source = tmp_path / "source"
        source.mkdir()
        link = source / ".venv" / "bin" / "python"
        link.parent.mkdir(parents=True)
        link.symlink_to(sys.executable)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result = _run(
            worktree,
            [sys.executable, "-c", "print(1)"],
            tmp_path,
            environment_repo=source,
        )
        assert result["available"] is True
        assert result["rc"] == 0


class TestExplicitCommandsHonored:
    def test_absolute_operator_cmd_bypasses_guard(self, tmp_path, monkeypatch):
        marker = tmp_path / "ran"
        path_bin = _write_tool(tmp_path / "pathbin", "pytest", marker)
        monkeypatch.setenv("PATH", str(path_bin.parent))
        source = tmp_path / "source"
        source.mkdir()
        _venv_tool(source, "pytest")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result = _run(
            worktree, [str(path_bin), "-q"], tmp_path, environment_repo=source
        )
        assert result["available"] is True
        assert result["rc"] == 0
        assert marker.is_file()

    def test_inferred_venv_python_dash_m_unaffected(self, tmp_path, monkeypatch):
        # Runnable module from cwd (stdlib `sys` has no __main__ and exits 1):
        # proves the absolute interpreter is honored under shadow context and
        # -m resolves against the execution cwd.
        marker = tmp_path / "ran"
        path_bin = _write_tool(tmp_path / "pathbin", "pytest", marker)
        monkeypatch.setenv("PATH", str(path_bin.parent))
        source = tmp_path / "source"
        source.mkdir()
        _venv_tool(source, "pytest")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tinycheck.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('x')\n"
        )
        result = _run(
            worktree,
            [sys.executable, "-m", "tinycheck"],
            tmp_path,
            environment_repo=source,
        )
        assert result["available"] is True
        assert result["rc"] == 0
        assert marker.is_file()


class TestWrappersOutsideGuard:
    def test_poetry_wrapper_not_shadow_guarded(self, tmp_path, monkeypatch):
        marker = tmp_path / "ran"
        path_bin = _write_tool(tmp_path / "pathbin", "poetry", marker)
        monkeypatch.setenv("PATH", str(path_bin.parent))
        source = tmp_path / "source"
        source.mkdir()
        _venv_tool(source, "poetry")  # local copy exists; guard must not fire
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        result = _run(worktree, ["poetry", "check"], tmp_path,
                      environment_repo=source)
        assert result["available"] is True
        assert result["rc"] == 0
        assert marker.is_file()
