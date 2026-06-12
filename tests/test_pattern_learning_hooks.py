import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

from bluei.engine import lifecycle
from bluei.engine.context_fix import apply_contextual_fix
from bluei.engine.pattern_store import FixPatternStore


def run(cmd, cwd):
    subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def commit_file(repo, relative_path, content):
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    run(["git", "add", relative_path], repo)
    run(["git", "commit", "-m", f"add {relative_path}"], repo)
    return path


def claude_kwargs(make_finding_fn, tmp_path, worktree, log_file, **overrides):
    from bluei.engine.lifecycle import ClaudeFixRequest

    kwargs = {
        "worktree_path": worktree,
        "finding": make_finding_fn(
            finding_id="finding-1",
            repo="repo",
            rule="claude-rule",
            path="src/example.py",
            snippet="return amount + discount",
            confidence=0.9,
            safe_to_autofix=False,
        ),
        "baseline_checks": {},
        "target_checks": {},
        "claude_cmd_template": "echo fixed",
        "max_files_changed": 5,
        "max_loc_diff": 200,
        "log_file": log_file,
    }
    kwargs.update(overrides)
    return ClaudeFixRequest(**kwargs)


def test_apply_claude_fix_with_no_pattern_store_keeps_backward_compat(
    tmp_path, monkeypatch, make_finding
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    log_file = tmp_path / "run.log"

    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok"),
    )

    rc, output, _ = lifecycle.apply_claude_fix(
        claude_kwargs(make_finding, tmp_path, worktree, log_file)
    )

    assert rc == 0
    assert output == "ok"
    assert "pattern-extract-hook" not in log_file.read_text(encoding="utf-8")


def test_apply_autofix_with_no_pattern_store_keeps_backward_compat(
    tmp_path, git_repo, make_finding
):
    commit_file(
        git_repo,
        "src/example.py",
        "def price(amount, discount):\n    return amount + discount\n",
    )
    log_file = tmp_path / "run.log"

    finding = make_finding(
        finding_id="finding-1",
        repo="repo",
        rule="discount-math-sign",
        path="src/example.py",
        snippet="return amount + discount",
        confidence=0.9,
    )
    assert lifecycle.apply_autofix(git_repo, finding, log_file) is True
    assert "return amount - discount" in (git_repo / "src/example.py").read_text(
        encoding="utf-8"
    )
    assert "pattern-extract-hook" not in log_file.read_text(encoding="utf-8")


def test_apply_claude_fix_success_extracts_with_correct_args(
    tmp_path, monkeypatch, make_finding
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    log_file = tmp_path / "run.log"
    store_path = tmp_path / "state" / "fix_patterns.jsonl"
    calls = []

    def fake_extract(worktree_path, finding, fix_source, store, log_path):
        calls.append((worktree_path, finding, fix_source, store, log_path))
        return "fp-test"

    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok"),
    )
    monkeypatch.setattr("bluei.engine.pattern_extractor.extract", fake_extract)

    rc, _, _ = lifecycle.apply_claude_fix(
        claude_kwargs(
            make_finding, tmp_path, worktree, log_file, pattern_store_path=store_path
        )
    )

    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0] == worktree
    assert calls[0][1].rule == "claude-rule"
    assert calls[0][2] == "claude"
    assert isinstance(calls[0][3], FixPatternStore)
    assert calls[0][3].store_path == store_path
    assert calls[0][4] == log_file


def test_apply_autofix_success_extracts_with_correct_args(
    tmp_path, git_repo, monkeypatch, make_finding
):
    commit_file(
        git_repo,
        "src/example.py",
        "def price(amount, discount):\n    return amount + discount\n",
    )
    log_file = tmp_path / "run.log"
    store_path = tmp_path / "state" / "fix_patterns.jsonl"
    calls = []

    def fake_extract(worktree_path, finding, fix_source, store, log_path):
        calls.append((worktree_path, finding, fix_source, store, log_path))
        return "fp-test"

    monkeypatch.setattr("bluei.engine.pattern_extractor.extract", fake_extract)

    finding = make_finding(
        finding_id="finding-1",
        repo="repo",
        rule="discount-math-sign",
        path="src/example.py",
        snippet="return amount + discount",
        confidence=0.9,
    )
    assert lifecycle.apply_autofix(
        git_repo, finding, log_file, pattern_store_path=store_path
    )
    assert calls[0][0] == git_repo
    assert calls[0][1].rule == "discount-math-sign"
    assert calls[0][2] == "recipe"
    assert isinstance(calls[0][3], FixPatternStore)
    assert calls[0][3].store_path == store_path
    assert calls[0][4] == log_file


def test_apply_claude_fix_failure_does_not_extract(tmp_path, monkeypatch, make_finding):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    log_file = tmp_path / "run.log"
    calls = []

    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="failed"),
    )
    monkeypatch.setattr(
        lifecycle, "_extract_fix_pattern", lambda *args, **kwargs: calls.append(args)
    )

    rc, _, _ = lifecycle.apply_claude_fix(
        claude_kwargs(
            make_finding,
            tmp_path,
            worktree,
            log_file,
            pattern_store_path=tmp_path / "patterns.jsonl",
        )
    )

    assert rc == 1
    assert calls == []


def test_apply_autofix_failure_does_not_extract(
    tmp_path, git_repo, monkeypatch, make_finding
):
    log_file = tmp_path / "run.log"
    calls = []
    monkeypatch.setattr(
        lifecycle, "_extract_fix_pattern", lambda *args, **kwargs: calls.append(args)
    )

    finding = make_finding(
        finding_id="finding-1",
        repo="repo",
        rule="discount-math-sign",
        path="missing.py",
        snippet="return amount + discount",
        confidence=0.9,
    )
    result = lifecycle.apply_autofix(
        git_repo,
        finding,
        log_file,
        pattern_store_path=tmp_path / "patterns.jsonl",
    )

    assert result is False
    assert calls == []


def test_extraction_exception_does_not_block_success(
    tmp_path, git_repo, monkeypatch, make_finding
):
    commit_file(
        git_repo,
        "src/example.py",
        "def price(amount, discount):\n    return amount + discount\n",
    )
    log_file = tmp_path / "run.log"

    def boom(*args, **kwargs):
        raise RuntimeError("extract failed")

    monkeypatch.setattr("bluei.engine.pattern_extractor.extract", boom)

    finding = make_finding(
        finding_id="finding-1",
        repo="repo",
        rule="discount-math-sign",
        path="src/example.py",
        snippet="return amount + discount",
        confidence=0.9,
    )
    result = lifecycle.apply_autofix(
        git_repo,
        finding,
        log_file,
        pattern_store_path=tmp_path / "state" / "fix_patterns.jsonl",
    )

    assert result is True
    assert (
        "pattern-extract-hook: source=recipe finding_id=finding-1 error=RuntimeError"
        in log_file.read_text(encoding="utf-8")
    )


def test_apply_contextual_fix_propagates_pattern_store_path_to_autofix(
    tmp_path, make_finding
):
    finding = make_finding(
        finding_id="finding-1",
        repo="repo",
        rule="unknown-rule",
        path="src/example.py",
        snippet="return amount + discount",
        confidence=0.9,
    )
    log_file = tmp_path / "run.log"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store_path = tmp_path / "state" / "fix_patterns.jsonl"

    from unittest.mock import patch

    with patch("bluei.engine.lifecycle.apply_autofix") as mock_autofix:
        mock_autofix.return_value = True
        result = apply_contextual_fix(
            repo_path=tmp_path,
            finding=finding,
            log_file=log_file,
            worktree_path=worktree,
            pattern_store_path=store_path,
        )

    assert result is True
    assert mock_autofix.call_args.kwargs["pattern_store_path"] == store_path


def test_apply_contextual_fix_propagates_pattern_store_path_to_claude(
    tmp_path, make_finding
):
    finding = make_finding(
        finding_id="finding-1",
        repo="repo",
        rule="ruff-b904",
        path="zerver/middleware.py",
        snippet="return amount + discount",
        confidence=0.9,
        safe_to_autofix=False,
    )
    log_file = tmp_path / "run.log"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store_path = tmp_path / "state" / "fix_patterns.jsonl"

    from unittest.mock import patch

    with patch("bluei.engine.lifecycle.apply_claude_fix") as mock_claude:
        mock_claude.return_value = (0, "output", "prompt_file")
        result = apply_contextual_fix(
            repo_path=tmp_path,
            finding=finding,
            log_file=log_file,
            worktree_path=worktree,
            pattern_store_path=store_path,
        )

    assert result is True
    assert mock_claude.call_args[0][0].pattern_store_path == store_path


def test_integration_apply_autofix_writes_pattern_to_store_jsonl(
    tmp_path, git_repo, make_finding
):
    commit_file(
        git_repo,
        "src/example.py",
        "def price(amount, discount):\n    return amount + discount\n",
    )
    log_file = tmp_path / "run.log"
    store_path = tmp_path / "state" / "fix_patterns.jsonl"

    finding = make_finding(
        finding_id="finding-1",
        repo="repo",
        rule="discount-math-sign",
        path="src/example.py",
        snippet="return amount + discount",
        confidence=0.9,
    )
    result = lifecycle.apply_autofix(
        git_repo, finding, log_file, pattern_store_path=store_path
    )

    assert result is True
    records = [
        json.loads(line) for line in store_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["rule"] == "discount-math-sign"
    assert records[0]["source"] == "recipe"
    assert (
        records[0]["before_snippet"]
        == "def price(amount, discount):\n return amount + discount"
    )
    assert (
        records[0]["after_snippet"]
        == "def price(amount, discount):\n return amount - discount"
    )
