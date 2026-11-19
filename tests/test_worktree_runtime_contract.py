"""Stage 2 — worktree runtime contract with a real venv and real subprocesses.

The contract, stated honestly:

- Runtime identity: an operator-declared absolute venv interpreter
  (``<source>/.venv/bin/python``, a real POSIX symlink venv built with
  stdlib ``venv.EnvBuilder(with_pip=False)`` — no dependency installs,
  ever) keeps ``sys.prefix`` pinned to the source venv while the process
  cwd is the linked worktree. Nothing is hydrated into the worktree.
- Import origin: ``python -m <module>`` from the worktree imports the
  worktree's module (marker ``candidate``), not the source's
  (``original``). An absolute interpreter proves runtime identity only —
  never import provenance — so origin is proven by an explicit declared
  origin-gate command, not by availability alone. A ``.pth`` that
  preloads the source module into ``sys.modules`` makes that gate return
  1: executed-failure evidence that denies qualification (no passing
  companion).
- Inference honesty: ``infer_baseline_checks`` on a tests/pyproject repo
  selects ``[absvenv, -m, pytest, -q]``; in a pip-less venv pytest is
  missing-module unqualified evidence. The remedy is an operator-declared
  baseline, never a dependency install.
- Review propagation: ``WorktreeMixin._run_validation`` forwards
  ``environment_repo=Path(config.path)``, denying a host ``pytest`` for a
  worktree that declares no venv of its own. No commit boundary involved.

Self-contained fixtures (no cross-test imports), separate from the public
ordinary-repair corpus. POSIX ``bin`` layout only.
"""

import venv
from pathlib import Path

from bluei.engine.validation import (
    RC_UNAVAILABLE,
    run_named_checks,
    run_validation_gate,
)

ORIGIN_GATE = (
    "import os\n"
    "import candidate_probe\n"
    "\n"
    "origin = os.path.abspath(candidate_probe.__file__)\n"
    "expected = os.path.abspath(os.path.join(os.getcwd(), 'candidate_probe.py'))\n"
    "print('MARKER=' + candidate_probe.MARKER)\n"
    "print('ORIGIN=' + origin)\n"
    "raise SystemExit(0 if origin == expected else 1)\n"
)


def _write_probe(directory: Path, marker: str) -> None:
    (directory / "candidate_probe.py").write_text(
        "import sys\n"
        "\n"
        f"MARKER = {marker!r}\n"
        "\n"
        'if __name__ == "__main__":\n'
        '    print("MARKER=" + MARKER)\n'
        '    print("PREFIX=" + sys.prefix)\n'
        '    print("FILE=" + str(__file__))\n'
    )


def _build_pipless_venv(source: Path) -> Path:
    """Real POSIX symlink venv: no pip, no network, no installs."""
    builder = venv.EnvBuilder(
        with_pip=False, system_site_packages=False, symlinks=True
    )
    builder.create(str(source / ".venv"))
    return source / ".venv"


def _site_packages(venv_dir: Path) -> Path:
    matches = list(venv_dir.glob("lib/python*/site-packages"))
    assert len(matches) == 1, f"expected one site-packages, got {matches}"
    return matches[0]


def _kv(output: str) -> dict:
    return dict(
        line.split("=", 1) for line in output.splitlines() if "=" in line
    )


def test_inferred_venv_pytest_is_unqualified_without_any_install(
    tmp_path, monkeypatch
):
    """Inference selects the abs venv ``-m pytest``; in a pip-less venv that
    is missing-module unqualified evidence — the fix is an operator-declared
    baseline, not a dependency install.

    The test harness exports PYTHONPATH (deps:feature) so the parent pytest
    process can import the product; a child interpreter would inherit it and
    see harness deps even inside an isolated venv. That is a harness artifact,
    not a runtime guarantee, so this test strips it (plus the user site) for
    the child run only — the parent process keeps its already-imported
    modules. Product ``run_capture`` behavior is untouched: children simply
    inherit the test-scoped environment."""
    from bluei.app.models import LanguageInfo
    from bluei.app.onboarding.inference import infer_baseline_checks

    source = tmp_path / "source"
    source.mkdir()
    (source / "tests").mkdir()
    (source / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    venv_dir = _build_pipless_venv(source)
    py = str(venv_dir / "bin" / "python")

    checks = infer_baseline_checks(source, LanguageInfo(name="python"))
    pytest_cmd = next(c for c in checks if c[1:] == ["-m", "pytest", "-q"])
    assert pytest_cmd[0] == py  # explicit absolute venv interpreter

    # Child-only isolation: no harness PYTHONPATH, no user site. Without
    # this the -m probe would resolve harness pytest and mask missing-module.
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")

    result = run_named_checks(
        worktree,
        {"inferred_pytest": pytest_cmd},
        tmp_path / "log.txt",
        "baseline",
        environment_repo=source,
    )["inferred_pytest"]

    assert result["available"] is False
    assert result["unavailable_reason"] == "missing-module"
    assert result["rc"] == RC_UNAVAILABLE
    assert result["fingerprint"] == ""  # unavailable: not executed evidence
    # No install papered over the gap. This is an install claim only — it
    # is NOT a general "the venv imports nothing" proof (harness env vars,
    # .pth files etc. can still inject; the origin-gate test covers that).
    site = _site_packages(venv_dir)
    assert not list(site.glob("pytest*")) and not list(site.glob("_pytest*"))


def test_declared_module_probe_pins_runtime_identity_and_import_origin(
    tmp_path,
):
    """Operator-declared ``[absvenv, -m, candidate_probe]``: same absolute
    interpreter everywhere, sys.prefix stays the source venv, the module
    imported is the one under the execution cwd, and no environment is
    hydrated into the worktree."""
    source = tmp_path / "source"
    source.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_probe(source, "original")
    _write_probe(worktree, "candidate")

    venv_dir = _build_pipless_venv(source)
    probe_cmd = [str(venv_dir / "bin" / "python"), "-m", "candidate_probe"]
    log = tmp_path / "log.txt"

    in_source = run_named_checks(
        source, {"probe": probe_cmd}, log, "baseline", environment_repo=source
    )["probe"]
    assert in_source["available"] is True
    assert in_source["rc"] == 0
    assert "MARKER=original" in in_source["output"]

    in_worktree = run_named_checks(
        worktree, {"probe": probe_cmd}, log, "baseline", environment_repo=source
    )["probe"]
    assert in_worktree["available"] is True
    assert in_worktree["rc"] == 0
    kv = _kv(in_worktree["output"])
    assert kv["MARKER"] == "candidate"  # worktree module, not the source's
    assert kv["PREFIX"] == str(venv_dir)  # runtime identity: source venv
    assert Path(kv["FILE"]).resolve().parent == worktree.resolve()
    assert not (worktree / ".venv").exists()  # no environment hydration


def test_pth_shadowed_import_origin_fails_declared_origin_gate(tmp_path):
    """A ``.pth`` preloading the source module shadows the worktree import.
    The declared origin gate executes (available), returns 1 with the source
    path as ORIGIN, and the validation gate stays unqualified — availability
    alone never proves import provenance, and there is no passing companion."""
    source = tmp_path / "source"
    source.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_probe(source, "original")
    _write_probe(worktree, "candidate")

    venv_dir = _build_pipless_venv(source)
    py = str(venv_dir / "bin" / "python")
    (_site_packages(venv_dir) / "zz_shadow.pth").write_text(
        f"import sys; sys.path.append({str(source)!r}); "
        "import candidate_probe"
    )
    (worktree / "origin_gate.py").write_text(ORIGIN_GATE)

    gate = run_validation_gate(
        repo_path=worktree,
        worktree_path=worktree,
        checks={"origin_gate": [py, "-m", "origin_gate"]},
        log_file=tmp_path / "log.txt",
    )

    record = gate["baseline_results"]["origin_gate"]
    assert record["available"] is True  # launched — proves nothing about origin
    assert record["rc"] == 1  # honest origin assertion failed
    kv = _kv(record["output"])
    assert kv["MARKER"] == "original"  # shadow observed: source module won
    assert Path(kv["ORIGIN"]).resolve().parent == source.resolve()
    assert gate["passed"] is False
    assert gate["qualified"] is False  # no passing companion
    assert gate["checks_passed"] == 0
    assert gate["target_failures"] == ["origin_gate"]


def test_review_run_validation_propagates_environment_repo(tmp_path, monkeypatch):
    """``WorktreeMixin._run_validation`` forwards the source repo as
    ``environment_repo``: a bare host ``pytest`` is refused for a worktree
    that declares no venv of its own, and the foreign runtime never
    launches. No commit boundary is involved."""
    from bluei.app.models import Repo, RepoConfig
    from bluei.app.state import StateManager
    from bluei.review.cycle import ReviewCycleEngine

    source = tmp_path / "source"
    source.mkdir()
    (source / ".venv" / "bin").mkdir(parents=True)  # declared runtime, empty
    marker = tmp_path / "host-pytest-ran"
    host_bin = tmp_path / "hostbin"
    host_bin.mkdir()
    host_pytest = host_bin / "pytest"
    host_pytest.write_text(f"#!/bin/sh\nprintf x > '{marker}'\nexit 0\n")
    host_pytest.chmod(0o755)
    monkeypatch.setenv("PATH", str(host_bin))

    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = Repo(
        config=RepoConfig(
            id="repo-test",
            name="owner/test-repo",
            path=str(source),
            language="python",
            baseline_checks=[["pytest", "-q"]],
        )
    )
    engine.state = StateManager(tmp_path / "repos")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    result = engine._run_validation(worktree)

    assert result["ok"] is False
    assert result["reason"] == "validation-failed-or-unavailable"
    record = result["results"][0]
    # The worktree itself declares no venv: only the propagated
    # environment_repo (the source repo) can deny the host tool here.
    assert record["available"] is False
    assert record["returncode"] == RC_UNAVAILABLE
    assert not marker.is_file()  # foreign runtime never launched
