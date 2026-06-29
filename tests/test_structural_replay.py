"""Tests for AST-aware Structural Replay (ADR-0019, Slice 1)."""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple
from unittest.mock import MagicMock

import pytest

from bluei.engine.pattern_store import FixPattern, ReplayOutcome
from bluei.engine.structural_replay import (
    StructuralReplayResult,
    apply_structural_replay,
)


def _make_pattern(
    *,
    before: str,
    after: str,
    rule: str = "ruff-c408",
    language: str = "python",
    imports_touched: List[str] | None = None,
    pattern_id: str = "fp-test-001",
    confidence: float = 0.95,
) -> FixPattern:
    return FixPattern(
        pattern_id=pattern_id,
        rule=rule,
        language=language,
        file_path="src/app.py",
        before_snippet=before,
        after_snippet=after,
        diff_patch="",
        confidence=confidence,
        imports_touched=list(imports_touched or []),
    )


def test_ac_p1_1_rename_tolerant_apply() -> None:
    pattern = _make_pattern(
        before='raise ValueError("bad")',
        after='raise ValueError("bad") from err',
    )
    file_text = "def f():\n    raise CustomError(custom_msg)\n"

    result = apply_structural_replay(file_text, pattern, "python", target_line=2)

    assert result is not None
    assert isinstance(result, StructuralReplayResult)
    assert "from err" in result.new_source
    assert "CustomError" in result.new_source
    assert "custom_msg" in result.new_source
    assert "ValueError" not in result.new_source


def test_ac_p1_2_evidence_recording(tmp_path: Path) -> None:
    from bluei.engine.models import Finding
    from bluei.engine import pattern_replay as pr

    worktree = tmp_path / "repo"
    (worktree / "src").mkdir(parents=True)
    target_file = worktree / "src" / "app.py"
    target_file.write_text("def f():\n    raise CustomError(custom_msg)\n")

    finding = Finding(
        finding_id="f1",
        repo="r",
        path="src/app.py",
        line=2,
        rule="ruff-c408",
        snippet="raise CustomError(custom_msg)",
        confidence=0.95,
        quick_win=True,
        safe_to_autofix=True,
    )

    pattern = _make_pattern(
        before='raise ValueError("bad")',
        after='raise ValueError("bad") from err',
    )

    store = MagicMock()
    store.lookup.return_value = pattern
    store.lookup_structural.return_value = None
    store.lookup_fuzzy.return_value = None

    log_file = tmp_path / "log.txt"

    baseline_checks: dict = {}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pr, "_run_baseline_validation", lambda *a, **k: (True, ""))

        ok, pid = pr._try_replay_inner(
            worktree_path=worktree,
            finding=finding,
            store=store,
            baseline_checks=baseline_checks,
            log_file=log_file,
            record_outcome=True,
        )

    assert ok is True
    assert pid == "fp-test-001"
    store.record_replay_outcome.assert_called_once_with(
        "fp-test-001", ReplayOutcome.HIT
    )
    final_text = target_file.read_text(encoding="utf-8")
    assert "from err" in final_text


def test_ac_p1_3_binding_completeness_gate() -> None:
    pattern = _make_pattern(
        before="return self.ctx.value",
        after="return self.ctx.resolved()",
    )
    file_text = "class C:\n    def m(self):\n        return ctx.value\n"

    result = apply_structural_replay(file_text, pattern, "python", target_line=3)

    assert result is None


def test_ac_p1_4_confidence_gate_skips_structural(tmp_path: Path) -> None:
    from bluei.engine.models import Finding
    from bluei.engine import pattern_replay as pr

    worktree = tmp_path / "repo"
    (worktree / "src").mkdir(parents=True)
    target_file = worktree / "src" / "app.py"
    target_file.write_text("def f():\n    raise CustomError(custom_msg)\n")

    finding = Finding(
        finding_id="f1",
        repo="r",
        path="src/app.py",
        line=2,
        rule="ruff-c408",
        snippet="raise CustomError(custom_msg)",
        confidence=0.95,
        quick_win=True,
        safe_to_autofix=True,
    )

    pattern = _make_pattern(
        before='raise ValueError("bad")',
        after='raise ValueError("bad") from err',
        confidence=0.6,
    )

    store = MagicMock()
    store.lookup.return_value = pattern
    store.lookup_structural.return_value = None
    store.lookup_fuzzy.return_value = None

    log_file = tmp_path / "log.txt"

    calls: List[Tuple] = []

    def _fake_apply(*args, **kwargs):
        calls.append(args)
        return None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "bluei.engine.structural_replay.apply_structural_replay", _fake_apply
        )
        ok, pid = pr._try_replay_inner(
            worktree_path=worktree,
            finding=finding,
            store=store,
            baseline_checks={},
            log_file=log_file,
            record_outcome=True,
        )

    assert ok is False
    assert calls == []


def test_ac_p1_5_ast_cascade_unchanged() -> None:
    import os
    import subprocess

    env = dict(os.environ)
    env["PYTHONPATH"] = "."

    result = subprocess.run(
        [
            ".venv/bin/python",
            "-m",
            "pytest",
            "tests/test_cascade.py",
            "tests/test_composite_cascade_stage.py",
            "-q",
            "--no-header",
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=".",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_non_python_language_returns_none() -> None:
    pattern = _make_pattern(
        before="let x = 1", after="let x = 2", language="typescript"
    )
    result = apply_structural_replay("let x = 1", pattern, "typescript")
    assert result is None


def test_malformed_before_snippet_returns_none() -> None:
    pattern = _make_pattern(before="def (", after="pass")
    result = apply_structural_replay(
        "def f():\n    pass\n", pattern, "python", target_line=1
    )
    assert result is None


def test_malformed_after_snippet_returns_none() -> None:
    pattern = _make_pattern(before="x = 1", after="def (")
    result = apply_structural_replay("x = 1\n", pattern, "python", target_line=1)
    assert result is None


def test_target_line_zero_whole_file_scan() -> None:
    pattern = _make_pattern(
        before='raise ValueError("bad")',
        after='raise ValueError("bad") from err',
    )
    file_text = "x = 1\nraise CustomError(custom_msg)\ny = 2\n"
    result = apply_structural_replay(file_text, pattern, "python", target_line=0)
    assert result is not None
    assert "from err" in result.new_source


def test_imports_added_populated_for_new_import() -> None:
    pattern = _make_pattern(
        before="os_path = 'x'",
        after="os_path = os.sep\nimport os",
        imports_touched=["import os"],
    )
    file_text = "def f():\n    os_path = 'x'\n"
    result = apply_structural_replay(file_text, pattern, "python", target_line=2)
    assert result is not None
    assert "import os" in result.imports_added
    assert "import os" in result.new_source


def test_structural_mismatch_returns_none() -> None:
    pattern = _make_pattern(
        before='raise ValueError("bad")',
        after='raise ValueError("bad") from err',
    )
    file_text = "def f():\n    return 1 + 2\n"
    result = apply_structural_replay(file_text, pattern, "python", target_line=2)
    assert result is None


def test_inconsistent_binding_returns_none() -> None:
    pattern = _make_pattern(
        before="x = x + 1",
        after="x = x + 2",
    )
    file_text = "a = b + 1\n"
    result = apply_structural_replay(file_text, pattern, "python", target_line=1)
    assert result is None


def test_comments_preserved_after_structural_replay() -> None:
    pattern = _make_pattern(
        before='raise ValueError("bad")',
        after='raise ValueError("bad") from err',
    )
    file_text = (
        "# License header\n"
        "def f():  # type: ignore\n"
        '    """Doc."""\n'
        "    raise CustomError(custom_msg)\n"
        "# trailing comment\n"
    )
    result = apply_structural_replay(file_text, pattern, "python", target_line=4)
    assert result is not None
    assert "# License header" in result.new_source
    assert "# type: ignore" in result.new_source
    assert '"""Doc."""' in result.new_source
    assert "# trailing comment" in result.new_source


def test_indentation_preserved_inside_class_method() -> None:
    pattern = _make_pattern(
        before='raise ValueError("bad")',
        after='raise ValueError("bad") from err',
    )
    file_text = "class C:\n    def m(self):\n        raise CustomError(custom_msg)\n"
    result = apply_structural_replay(file_text, pattern, "python", target_line=3)
    assert result is not None
    assert "        raise CustomError" in result.new_source


def test_large_file_returns_none() -> None:
    pattern = _make_pattern(before="x = 1", after="x = 2")
    big_file = "x = 1\n" * 50000
    result = apply_structural_replay(big_file, pattern, "python", target_line=1)
    assert result is None
