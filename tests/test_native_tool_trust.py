"""Executable-boundary tests for repository-tool trust in native linters.

bluei.engine.linters must never execute tsc/xo/ruff binaries planted inside
the scanned repository's node_modules/.bin unless
BLUEI_TRUST_REPOSITORY_TOOLS=1 (same contract as the plugin wrapper in
bluei/app/plugins.py), and external PATH executables that resolve outside the
scanned repo are used by default — except xo: xo loads and executes
repository-controlled JS config (package.json "xo", .xo-config, eslint
configs) from its working directory, so ANY host xo run against the repo,
even a fully external one, requires the explicit trust flag.

No real xo is installed on this host and installing one is not allowed, so
the xo tests below prove the execution boundary and trust gating with fake
executables only — they are NOT a real xo config-loading integration test.

All tests use real on-disk executables and real subprocess boundaries — no
mocks of shutil.which/subprocess — so a planted binary that runs leaves a
marker file behind and fails the test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bluei.engine.linters import (
    discover_python_linter_findings,
    discover_typescript_type_findings,
    discover_xo_linter_findings,
)


# ── helpers ──────────────────────────────────────────────────────


def _make_tool(path: Path, marker: Path, stdout: str) -> Path:
    """Create a real fake tool: appends to marker, then prints stdout.

    Only shell builtins are used (echo/printf): tests run with an isolated
    PATH, so external commands like cat are intentionally unavailable.
    """
    import shlex

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f"echo ran-{path.name} >> {shlex.quote(str(marker))}\n"
        f"printf '%s\\n' {shlex.quote(stdout)}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _ts_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")


def _xo_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "package.json").write_text(
        json.dumps({"devDependencies": {"xo": "^0.50"}}), encoding="utf-8"
    )


def _tsc_stdout(repo: Path) -> str:
    return (
        f"{repo}/src/mod.ts(3,7): error TS7005: Variable 'x' implicitly has "
        "an 'any' type.\n"
    )


def _xo_stdout(repo: Path) -> str:
    return json.dumps(
        [
            {
                "filePath": str(repo / "src/a.ts"),
                "messages": [
                    {
                        "ruleId": "max-lines",
                        "message": "too long",
                        "line": 10,
                        "column": 1,
                        "severity": 1,
                    }
                ],
            }
        ]
    )


def _py_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "example.py").write_text("x = 1\n", encoding="utf-8")


def _ruff_stdout() -> str:
    return json.dumps(
        [
            {
                "code": "E501",
                "message": "line too long",
                "location": {"row": 3},
                "filename": "example.py",
            }
        ]
    )


@pytest.fixture
def no_trust(monkeypatch):
    monkeypatch.delenv("BLUEI_TRUST_REPOSITORY_TOOLS", raising=False)


@pytest.fixture
def trust(monkeypatch):
    monkeypatch.setenv("BLUEI_TRUST_REPOSITORY_TOOLS", "1")


@pytest.fixture
def empty_bin(tmp_path, monkeypatch):
    """PATH with an empty bin dir: no external tsc/xo can resolve."""
    bin_dir = tmp_path / "empty-bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Isolate ~ so the ~/.local/bin/ruff fallback cannot leak host state."""
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.mark.parametrize("adapter", ["tsc", "xo", "plugin-xo"])
def test_relative_path_tool_is_pinned_before_child_changes_directory(
    tmp_path, monkeypatch, no_trust, adapter
):
    host = tmp_path / "host"
    host.mkdir()
    repo = tmp_path / "repo"
    _ts_repo(repo) if adapter == "tsc" else _xo_repo(repo)
    name = "tsc" if adapter == "tsc" else "xo"
    host_marker, repo_marker = tmp_path / "host-ran", tmp_path / "repo-ran"
    output = _tsc_stdout(repo) if adapter == "tsc" else _xo_stdout(repo)
    _make_tool(host / "tools" / name, host_marker, output)
    _make_tool(repo / "tools" / name, repo_marker, output)
    monkeypatch.chdir(host)
    monkeypatch.setenv("PATH", "tools")
    if adapter in ("xo", "plugin-xo"):
        # XO loads repository-controlled config, so every xo adapter
        # (native and plugin) requires explicit trust even for external
        # executables; tsc resolved outside the repo stays usable untrusted.
        monkeypatch.setenv("BLUEI_TRUST_REPOSITORY_TOOLS", "1")
    if adapter == "plugin-xo":
        from plugins.typescript.plugin import Plugin
        plugin = Plugin()
        assert plugin._run_tool(repo) is not None
    elif adapter == "tsc":
        discover_typescript_type_findings(repo, tmp_path / "run.log")
    else:
        discover_xo_linter_findings(repo, tmp_path / "run.log")
    assert host_marker.exists()
    assert not repo_marker.exists()


# ── planted repository binaries must not run without opt-in ──────


def test_planted_local_tsc_not_executed_without_optin(
    tmp_path, monkeypatch, no_trust, empty_bin
):
    repo = tmp_path / "repo"
    _ts_repo(repo)
    marker = tmp_path / "marker"
    _make_tool(repo / "node_modules" / ".bin" / "tsc", marker, _tsc_stdout(repo))

    outcome: dict = {}
    findings = discover_typescript_type_findings(repo, repo / "run.log", outcome)

    assert not marker.exists(), "planted repo tsc executed without opt-in"
    assert findings == []
    rec = outcome["typescript-types"]
    assert rec["status"] == "unavailable"
    assert rec["count"] == 0
    assert rec.get("reason")


def test_planted_local_xo_not_executed_without_optin(
    tmp_path, monkeypatch, no_trust, empty_bin
):
    repo = tmp_path / "repo"
    _xo_repo(repo)
    marker = tmp_path / "marker"
    _make_tool(repo / "node_modules" / ".bin" / "xo", marker, _xo_stdout(repo))

    outcome: dict = {}
    findings = discover_xo_linter_findings(repo, repo / "run.log", outcome)

    assert not marker.exists(), "planted repo xo executed without opt-in"
    assert findings == []
    rec = outcome["xo-linter"]
    assert rec["status"] == "unavailable"
    assert rec["count"] == 0
    assert rec.get("reason")


# ── opt-in executes the trusted repository binary ────────────────


def test_opted_in_local_tsc_executes(tmp_path, monkeypatch, trust, empty_bin):
    repo = tmp_path / "repo"
    _ts_repo(repo)
    marker = tmp_path / "marker"
    _make_tool(repo / "node_modules" / ".bin" / "tsc", marker, _tsc_stdout(repo))

    outcome: dict = {}
    findings = discover_typescript_type_findings(repo, repo / "run.log", outcome)

    assert marker.exists(), "opted-in local tsc should have executed"
    assert len(findings) == 1
    assert findings[0].rule == "type-explicit-any"
    assert outcome["typescript-types"] == {
        "status": "ran",
        "count": 1,
    }


def test_opted_in_local_xo_executes(tmp_path, monkeypatch, trust, empty_bin):
    repo = tmp_path / "repo"
    _xo_repo(repo)
    marker = tmp_path / "marker"
    _make_tool(repo / "node_modules" / ".bin" / "xo", marker, _xo_stdout(repo))

    outcome: dict = {}
    findings = discover_xo_linter_findings(repo, repo / "run.log", outcome)

    assert marker.exists(), "opted-in local xo should have executed"
    assert len(findings) == 1
    assert findings[0].rule == "xo-max-lines"
    assert outcome["xo-linter"] == {"status": "ran", "count": 1}


# ── external PATH executables are the default ────────────────────


def test_external_tsc_preferred_over_planted_local(
    tmp_path, monkeypatch, no_trust
):
    repo = tmp_path / "repo"
    _ts_repo(repo)
    ext_marker = tmp_path / "ext-marker"
    local_marker = tmp_path / "local-marker"
    ext_bin = tmp_path / "ext-bin"
    ext_bin.mkdir()
    _make_tool(ext_bin / "tsc", ext_marker, _tsc_stdout(repo))
    _make_tool(repo / "node_modules" / ".bin" / "tsc", local_marker, _tsc_stdout(repo))
    monkeypatch.setenv("PATH", str(ext_bin))

    outcome: dict = {}
    findings = discover_typescript_type_findings(repo, repo / "run.log", outcome)

    assert ext_marker.exists(), "external PATH tsc should have executed"
    assert not local_marker.exists(), "repo-local tsc must not shadow external tsc"
    assert len(findings) == 1
    assert outcome["typescript-types"]["status"] == "ran"


def test_external_xo_blocked_without_optin(tmp_path, monkeypatch, no_trust):
    """Even a fully external xo loads repo-controlled JS config from its cwd,
    so it must not run without explicit trust (fake executable: boundary and
    gate proof only, not real xo config-loading integration)."""
    repo = tmp_path / "repo"
    _xo_repo(repo)
    ext_marker = tmp_path / "ext-marker"
    local_marker = tmp_path / "local-marker"
    ext_bin = tmp_path / "ext-bin"
    ext_bin.mkdir()
    _make_tool(ext_bin / "xo", ext_marker, _xo_stdout(repo))
    _make_tool(repo / "node_modules" / ".bin" / "xo", local_marker, _xo_stdout(repo))
    monkeypatch.setenv("PATH", str(ext_bin))

    outcome: dict = {}
    findings = discover_xo_linter_findings(repo, repo / "run.log", outcome)

    assert not ext_marker.exists(), "external xo ran without opt-in"
    assert not local_marker.exists(), "repo-local xo ran without opt-in"
    assert findings == []
    rec = outcome["xo-linter"]
    assert rec["status"] == "unavailable"
    assert "BLUEI_TRUST_REPOSITORY_TOOLS" in rec.get("reason", "")


def test_external_xo_executes_with_optin(tmp_path, monkeypatch, trust):
    repo = tmp_path / "repo"
    _xo_repo(repo)
    ext_marker = tmp_path / "ext-marker"
    ext_bin = tmp_path / "ext-bin"
    ext_bin.mkdir()
    _make_tool(ext_bin / "xo", ext_marker, _xo_stdout(repo))
    monkeypatch.setenv("PATH", str(ext_bin))

    outcome: dict = {}
    findings = discover_xo_linter_findings(repo, repo / "run.log", outcome)

    assert ext_marker.exists(), "opted-in external xo should have executed"
    assert len(findings) == 1
    assert outcome["xo-linter"]["status"] == "ran"


# ── PATH entries pointing back into the repo are rejected ────────


def test_path_alias_into_repo_rejected_for_tsc(tmp_path, monkeypatch, no_trust):
    repo = tmp_path / "repo"
    _ts_repo(repo)
    marker = tmp_path / "marker"
    _make_tool(repo / "node_modules" / ".bin" / "tsc", marker, _tsc_stdout(repo))
    # PATH resolves tsc, but the resolved binary lives inside the repo.
    monkeypatch.setenv("PATH", str(repo / "node_modules" / ".bin"))

    outcome: dict = {}
    findings = discover_typescript_type_findings(repo, repo / "run.log", outcome)

    assert not marker.exists(), "PATH alias into scanned repo executed"
    assert findings == []
    assert outcome["typescript-types"]["status"] == "unavailable"


def test_path_alias_into_repo_rejected_for_xo(tmp_path, monkeypatch, no_trust):
    repo = tmp_path / "repo"
    _xo_repo(repo)
    marker = tmp_path / "marker"
    _make_tool(repo / "node_modules" / ".bin" / "xo", marker, _xo_stdout(repo))
    monkeypatch.setenv("PATH", str(repo / "node_modules" / ".bin"))

    outcome: dict = {}
    findings = discover_xo_linter_findings(repo, repo / "run.log", outcome)

    assert not marker.exists(), "PATH alias into scanned repo executed"
    assert findings == []
    assert outcome["xo-linter"]["status"] == "unavailable"


# ── ruff: containment gate, external default ─────────────────────


def test_ruff_path_alias_into_repo_rejected(
    tmp_path, monkeypatch, no_trust, fake_home
):
    repo = tmp_path / "repo"
    _py_repo(repo)
    marker = tmp_path / "marker"
    _make_tool(repo / "node_modules" / ".bin" / "ruff", marker, _ruff_stdout())
    monkeypatch.setenv("PATH", str(repo / "node_modules" / ".bin"))

    outcome: dict = {}
    findings = discover_python_linter_findings(repo, repo / "run.log", outcome)

    assert not marker.exists(), "PATH-resolved repo ruff executed without opt-in"
    assert findings == []
    rec = outcome["python-linter-ruff"]
    assert rec["status"] == "unavailable"
    assert rec.get("reason")


def test_external_ruff_respected_without_optin(
    tmp_path, monkeypatch, no_trust, fake_home
):
    repo = tmp_path / "repo"
    _py_repo(repo)
    ext_marker = tmp_path / "ext-marker"
    ext_bin = tmp_path / "ext-bin"
    ext_bin.mkdir()
    _make_tool(ext_bin / "ruff", ext_marker, _ruff_stdout())
    monkeypatch.setenv("PATH", str(ext_bin))

    outcome: dict = {}
    findings = discover_python_linter_findings(repo, repo / "run.log", outcome)

    assert ext_marker.exists(), "external PATH ruff should have executed"
    assert len(findings) == 1
    assert outcome["python-linter-ruff"]["status"] == "ran"
