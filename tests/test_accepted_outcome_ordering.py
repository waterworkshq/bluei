"""A04 acceptance-boundary ordering for the Claude fix engine.

Invariants:
- ``apply_claude_fix`` records attempts and failure observations only.
  A bare provider rc0 emits NO positive learning (no ``fix_success`` flag,
  no success lesson, no pattern extraction).
- ``record_accepted_fix`` performs the positive emissions, once per call,
  and only when the caller invokes it at final acceptance.
- The no-learning sinks (``None`` ``findings_file`` / ``lessons_file`` /
  ``pattern_store_path``) stay inert on both paths.

The provider transport is monkeypatched (``lifecycle.subprocess.run``) —
no real model provider is ever invoked.
"""

import json
from pathlib import Path
from types import SimpleNamespace

from bluei.engine import lifecycle
from bluei.engine.lifecycle import ClaudeFixRequest


def _seed_findings_file(tmp_path: Path) -> Path:
    findings_file = tmp_path / "findings.jsonl"
    findings_file.write_text(
        json.dumps({"finding_id": "finding-1", "rule": "claude-rule"}) + "\n",
        encoding="utf-8",
    )
    return findings_file


def _request(tmp_path, make_finding, **overrides):
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    kwargs = {
        "worktree_path": worktree,
        "finding": make_finding(
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
        "log_file": tmp_path / "run.log",
    }
    kwargs.update(overrides)
    return ClaudeFixRequest(**kwargs)


def _stub_transport(monkeypatch, returncode, stdout):
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=returncode, stdout=stdout),
    )


def _finding_record(findings_file: Path):
    return json.loads(findings_file.read_text(encoding="utf-8").splitlines()[0])


def test_rc0_alone_records_attempt_but_no_positive_emissions(
    tmp_path, monkeypatch, make_finding
):
    findings_file = _seed_findings_file(tmp_path)
    lessons_file = tmp_path / "lessons.log"
    req = _request(
        tmp_path,
        make_finding,
        findings_file=findings_file,
        lessons_file=lessons_file,
        pattern_store_path=tmp_path / "state" / "fix_patterns.jsonl",
    )
    extract_calls = []
    monkeypatch.setattr(
        lifecycle,
        "_extract_fix_pattern",
        lambda *args, **kwargs: extract_calls.append(args),
    )
    _stub_transport(monkeypatch, 0, "ok")

    rc, _, _ = lifecycle.apply_claude_fix(req)

    assert rc == 0
    record = _finding_record(findings_file)
    assert record["fix_attempts"] == 1
    assert "fix_success" not in record
    assert "last_fix_error" not in record
    assert not lessons_file.exists()
    assert extract_calls == []
    assert "pattern-extract-hook" not in req.log_file.read_text(encoding="utf-8")


def test_failure_observations_are_retained(tmp_path, monkeypatch, make_finding):
    findings_file = _seed_findings_file(tmp_path)
    lessons_file = tmp_path / "lessons.log"
    req = _request(
        tmp_path,
        make_finding,
        findings_file=findings_file,
        lessons_file=lessons_file,
        pattern_store_path=tmp_path / "state" / "fix_patterns.jsonl",
    )
    extract_calls = []
    monkeypatch.setattr(
        lifecycle,
        "_extract_fix_pattern",
        lambda *args, **kwargs: extract_calls.append(args),
    )
    _stub_transport(monkeypatch, 1, "boom: syntax error")

    rc, output, _ = lifecycle.apply_claude_fix(req)

    assert rc == 1
    record = _finding_record(findings_file)
    assert record["fix_attempts"] == 1
    assert record["last_fix_error"] == "boom: syntax error"
    assert "fix_success" not in record
    lessons = lessons_file.read_text(encoding="utf-8")
    assert "finding_id: finding-1" in lessons
    assert "**Broke:** fix failed rc=1" in lessons
    assert "Changed" not in lessons
    assert extract_calls == []


def test_accepted_helper_writes_positives_once_after_attempt(
    tmp_path, monkeypatch, make_finding
):
    findings_file = _seed_findings_file(tmp_path)
    lessons_file = tmp_path / "lessons.log"
    req = _request(
        tmp_path,
        make_finding,
        findings_file=findings_file,
        lessons_file=lessons_file,
        pattern_store_path=tmp_path / "state" / "fix_patterns.jsonl",
    )
    extract_calls = []
    monkeypatch.setattr(
        lifecycle,
        "_extract_fix_pattern",
        lambda *args, **kwargs: extract_calls.append(args),
    )
    _stub_transport(monkeypatch, 0, "ok")

    lifecycle.apply_claude_fix(req)
    lifecycle.record_accepted_fix(req)

    record = _finding_record(findings_file)
    assert record["fix_success"] is True
    assert record["fix_attempts"] == 1
    lessons = lessons_file.read_text(encoding="utf-8")
    assert lessons.count("**Changed:**") == 1
    assert "fix succeeded rule=claude-rule path=src/example.py:42" in lessons
    assert len(extract_calls) == 1
    assert extract_calls[0][2] == "claude"


def test_accepted_helper_is_inert_when_all_learning_sinks_are_none(
    tmp_path, monkeypatch, make_finding
):
    req = _request(
        tmp_path, make_finding, pattern_store_path=None  # --no-learning shape
    )
    extract_calls = []
    monkeypatch.setattr(
        lifecycle,
        "_extract_fix_pattern",
        lambda *args, **kwargs: extract_calls.append(args),
    )

    lifecycle.record_accepted_fix(req)

    assert extract_calls == []
    assert not (tmp_path / "lessons.log").exists()
    assert not (tmp_path / "findings.jsonl").exists()
