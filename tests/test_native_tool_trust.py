"""Executable-boundary tests for repository-tool trust in native linters.

bluei.engine.linters must never execute tsc/xo binaries planted inside the
scanned repository's node_modules/.bin unless BLUEI_TRUST_REPOSITORY_TOOLS=1
(same contract as the plugin wrapper in bluei/app/plugins.py), while still
using external PATH executables that resolve outside the scanned repo.

All tests use real on-disk executables and real subprocess boundaries — no
mocks of shutil.which/subprocess — so a planted binary that runs leaves a
marker file behind and fails the test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bluei.engine.linters import (
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


def test_external_xo_used_when_local_not_trusted(tmp_path, monkeypatch, no_trust):
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

    assert ext_marker.exists(), "external PATH xo should have executed"
    assert not local_marker.exists(), "repo-local xo must not run without opt-in"
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
