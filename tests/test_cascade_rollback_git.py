import json
import subprocess

import pytest

from bluei.engine.cascade import (
    CascadeContext,
    CascadeResult,
    CascadeStage,
    DeterministicCascade,
    LinterFixStage,
    PatternReplayCascadeStage,
    CompositePatternCascadeStage,
)


def _commit_file(repo, rel_path, content, msg="initial"):
    fp = repo / rel_path
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel_path], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", msg], cwd=repo, capture_output=True, check=True
    )


def _cascade_finding(make_finding, **overrides):
    """Wrap conftest make_finding with cascade-specific defaults."""
    defaults = {
        "finding_id": "f-1",
        "repo": "demo",
        "rule": "ruff-e501",
        "path": "src/app.py",
        "snippet": "line too long",
        "confidence": 0.85,
        "quick_win": False,
        "safe_to_autofix": False,
        "line": 1,
    }
    defaults.update(overrides)
    return make_finding(**defaults)


class _MutateAndFailValidation(CascadeStage):
    name = "mutate-fail-validation"
    tier = 1

    def can_handle(self, finding, context):
        return True

    def attempt(self, finding, worktree, context):
        fp = worktree / finding.path
        if fp.exists():
            fp.write_text("CORRUPTED CONTENT\n", encoding="utf-8")
        return CascadeResult(
            success=True,
            stage_name=self.name,
            changes_made=[finding.path],
            validation_passed=False,
            latency_ms=1,
        )

    def rollback(self, finding, worktree):
        from bluei.engine.utils import run_capture

        fp = worktree / finding.path
        if fp.exists():
            run_capture(["git", "checkout", "--", str(fp)], cwd=worktree, timeout=10)


class _AlwaysSucceedStage(CascadeStage):
    name = "always-succeed"
    tier = 2

    def can_handle(self, finding, context):
        return True

    def attempt(self, finding, worktree, context):
        return CascadeResult(
            success=True,
            stage_name=self.name,
            changes_made=[finding.path],
            validation_passed=True,
            latency_ms=1,
        )


class _NoOpMutate(CascadeStage):
    name = "no-op-mutate"
    tier = 1

    def __init__(self):
        self.mutated = False

    def can_handle(self, finding, context):
        return True

    def attempt(self, finding, worktree, context):
        fp = worktree / finding.path
        if fp.exists():
            fp.write_text("CHANGED\n", encoding="utf-8")
            self.mutated = True
        return CascadeResult(
            success=True,
            stage_name=self.name,
            changes_made=[finding.path],
            validation_passed=False,
            latency_ms=1,
        )

    def rollback(self, finding, worktree):
        from bluei.engine.utils import run_capture

        fp = worktree / finding.path
        if fp.exists():
            run_capture(["git", "checkout", "--", str(fp)], cwd=worktree, timeout=10)


def test_linter_fix_stage_rollback_restores_file(git_repo, make_finding):
    original = "x = 1\ny = 2\n"
    _commit_file(git_repo, "src/app.py", original)

    stage = LinterFixStage(
        name="test-linter",
        command=["ruff", "check", "--fix", "{file}"],
        languages=["python"],
    )

    target = git_repo / "src" / "app.py"
    target.write_text("MUTATED LINE\n", encoding="utf-8")
    assert target.read_text() != original

    stage.rollback(_cascade_finding(make_finding, path="src/app.py"), git_repo)
    assert target.read_text() == original


def test_pattern_replay_stage_rollback_restores_file(git_repo, make_finding):
    original = "import os\nimport sys\n\ndef main():\n    pass\n"
    _commit_file(git_repo, "src/app.py", original)

    stage = PatternReplayCascadeStage()

    target = git_repo / "src" / "app.py"
    target.write_text("REPLAY APPLIED CHANGES\n", encoding="utf-8")
    assert target.read_text() != original

    stage.rollback(_cascade_finding(make_finding, path="src/app.py"), git_repo)
    assert target.read_text() == original


def test_composite_pattern_stage_rollback_restores_file(git_repo, make_finding):
    original = "class Foo:\n    pass\n"
    _commit_file(git_repo, "src/app.py", original)

    stage = CompositePatternCascadeStage()

    target = git_repo / "src" / "app.py"
    target.write_text("COMPOSITE APPLIED\n", encoding="utf-8")
    assert target.read_text() != original

    stage.rollback(_cascade_finding(make_finding, path="src/app.py"), git_repo)
    assert target.read_text() == original


def test_full_cascade_validation_fail_triggers_rollback(git_repo, make_finding):
    original = "def hello():\n    print('hello')\n"
    _commit_file(git_repo, "src/app.py", original)

    log = git_repo / "log.txt"
    log.write_text("")

    bad_stage = _MutateAndFailValidation()
    good_stage = _AlwaysSucceedStage()
    cascade = DeterministicCascade([bad_stage, good_stage])

    ctx = CascadeContext(log_file=log)
    result = cascade.execute(
        _cascade_finding(make_finding, path="src/app.py"), git_repo, ctx
    )

    assert result.success
    assert result.stage_name == "always-succeed"

    target = git_repo / "src/app.py"
    assert target.read_text() == original


def test_rollback_on_unmodified_file_no_crash(git_repo, make_finding):
    original = "x = 42\n"
    _commit_file(git_repo, "src/app.py", original)

    stage = LinterFixStage(
        name="test-linter",
        command=["ruff", "{file}"],
        languages=["python"],
    )

    stage.rollback(_cascade_finding(make_finding, path="src/app.py"), git_repo)

    target = git_repo / "src/app.py"
    assert target.read_text() == original


def test_rollback_no_crash_on_nonexistent_file(git_repo, make_finding):
    _commit_file(git_repo, "README.md", "# hello\n")

    stage = LinterFixStage(
        name="test-linter",
        command=["ruff", "{file}"],
        languages=["python"],
    )

    stage.rollback(_cascade_finding(make_finding, path="nonexistent.py"), git_repo)


def test_cascade_rollback_then_succeed_preserves_file(git_repo, make_finding):
    original = "a = 1\nb = 2\nc = 3\n"
    _commit_file(git_repo, "src/app.py", original)

    log = git_repo / "log.txt"
    log.write_text("")

    mutate_stage = _NoOpMutate()
    cascade = DeterministicCascade([mutate_stage])

    ctx = CascadeContext(log_file=log)
    result = cascade.execute(
        _cascade_finding(make_finding, path="src/app.py"), git_repo, ctx
    )

    assert not result.success
    assert mutate_stage.mutated

    target = git_repo / "src/app.py"
    assert target.read_text() == original


def test_linter_rollback_multiple_files(git_repo, make_finding):
    orig_a = "x = 1\n"
    orig_b = "y = 2\n"
    _commit_file(git_repo, "src/a.py", orig_a)
    _commit_file(git_repo, "src/b.py", orig_b)

    stage = LinterFixStage(
        name="test-linter",
        command=["ruff", "{file}"],
        languages=["python"],
    )

    (git_repo / "src" / "a.py").write_text("CHANGED A\n", encoding="utf-8")
    (git_repo / "src" / "b.py").write_text("CHANGED B\n", encoding="utf-8")

    stage.rollback(_cascade_finding(make_finding, path="src/a.py"), git_repo)
    stage.rollback(_cascade_finding(make_finding, path="src/b.py"), git_repo)

    assert (git_repo / "src" / "a.py").read_text() == orig_a
    assert (git_repo / "src" / "b.py").read_text() == orig_b


def test_cascade_telemetry_on_rollback(git_repo, make_finding):
    original = "x = 1\n"
    _commit_file(git_repo, "src/app.py", original)

    log = git_repo / "log.txt"
    log.write_text("")

    bad = _MutateAndFailValidation()
    good = _AlwaysSucceedStage()
    cascade = DeterministicCascade([bad, good])
    ctx = CascadeContext(log_file=log)
    result = cascade.execute(
        _cascade_finding(make_finding, path="src/app.py"), git_repo, ctx
    )

    log_text = log.read_text()
    assert "cascade-telemetry:" in log_text

    for line in log_text.splitlines():
        if "cascade-telemetry:" in line:
            data = json.loads(line.split("cascade-telemetry: ", 1)[1])
            assert "stages_failed" in data
            assert data["success"] is True
            break


def test_rollback_after_git_still_works_with_new_commits(git_repo, make_finding):
    original = "x = 1\n"
    _commit_file(git_repo, "src/app.py", original)

    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "empty commit"],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )

    target = git_repo / "src/app.py"
    target.write_text("MODIFIED\n", encoding="utf-8")

    stage = LinterFixStage(
        name="test-linter",
        command=["ruff", "{file}"],
        languages=["python"],
    )
    stage.rollback(_cascade_finding(make_finding, path="src/app.py"), git_repo)

    assert target.read_text() == original
