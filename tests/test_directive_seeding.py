#!/usr/bin/env python3
"""test_directive_seeding.py — Tests for directive-seeding phases 1–3."""

import json
import tempfile
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Ensure the workspace root (parent of core/) is on sys.path
import os

_workdir = os.environ.get("WORKSPACE_ROOT")
if _workdir:
    sys.path.insert(0, _workdir)
else:
    # Fallback: go up three levels from this file to reach workspace root
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from bluei.engine.models import Finding
from bluei.engine.state import (
    load_finding_record,
    update_finding_record,
    increment_fix_attempt,
)
from bluei.engine.utils import (
    load_lessons_for_finding,
    load_lessons_for_rule,
    append_lesson,
)
from bluei.engine.lifecycle import _should_use_mnemo


# ─── Finding dataclass tests ───────────────────────────────────────────────


def test_finding_from_dict_roundtrip_new_record(make_finding):
    """Roundtrip a Finding with all new fields set."""
    original = make_finding(
        finding_id="test-id-001",
        repo="test-repo",
        path="src/test.py",
        line=10,
        rule="test-rule",
        snippet="x = 1",
        confidence=0.9,
        fix_attempts=3,
        last_fix_error="ruff rc=1",
        last_fix_at="2026-03-25T00:00:00Z",
        fix_success=False,
    )
    d = original.as_dict()
    restored = Finding.from_dict(d)
    assert restored.finding_id == original.finding_id
    assert restored.fix_attempts == 3
    assert restored.last_fix_error == "ruff rc=1"
    assert restored.last_fix_at == "2026-03-25T00:00:00Z"
    assert restored.fix_success == False


def test_finding_from_dict_roundtrip_old_record():
    """Roundtrip a Finding with no new fields (old JSONL format)."""
    old_dict = {
        "finding_id": "old-id",
        "repo": "old-repo",
        "path": "old.py",
        "line": 5,
        "rule": "old-rule",
        "snippet": "y = 2",
        "confidence": 0.8,
        "quick_win": False,
        "safe_to_autofix": True,
    }
    f = Finding.from_dict(old_dict)
    assert f.fix_attempts == 0
    assert f.last_fix_error is None
    assert f.last_fix_at is None
    assert f.fix_success == False
    d = f.as_dict()
    assert d["fix_attempts"] == 0
    assert d["last_fix_error"] is None
    assert d["fix_success"] is False


def test_finding_from_dict_partial_record():
    """Partial record: only some new fields present."""
    partial = {
        "finding_id": "x",
        "repo": "r",
        "path": "p",
        "line": 1,
        "rule": "rule",
        "snippet": "s",
        "confidence": 0.9,
        "quick_win": True,
        "safe_to_autofix": True,
        "fix_attempts": 2,
    }
    f = Finding.from_dict(partial)
    assert f.fix_attempts == 2
    assert f.last_fix_error is None
    assert f.last_fix_at is None
    assert f.fix_success == False


def test_finding_as_dict_includes_defaults(make_finding):
    """as_dict now includes all fields (unified model uses asdict())."""
    f = make_finding(
        finding_id="test-id-001",
        repo="test-repo",
        path="src/test.py",
        line=10,
        rule="test-rule",
        snippet="x = 1",
        confidence=0.9,
    )
    d = f.as_dict()
    assert d["fix_attempts"] == 0
    assert d["last_fix_error"] is None
    assert d["last_fix_at"] is None
    assert d["fix_success"] is False


def test_finding_as_dict_includes_non_defaults(make_finding):
    """as_dict must write fields that have non-default values."""
    f = make_finding(
        finding_id="test-id-001",
        repo="test-repo",
        path="src/test.py",
        line=10,
        rule="test-rule",
        snippet="x = 1",
        confidence=0.9,
        fix_attempts=1,
        fix_success=True,
        last_fix_error="boom",
        last_fix_at=datetime.now(timezone.utc).isoformat(),
    )
    d = f.as_dict()
    assert d["fix_attempts"] == 1
    assert d["fix_success"] == True
    assert d["last_fix_error"] == "boom"
    assert "last_fix_at" in d


# ─── state.py function tests ────────────────────────────────────────────────


def test_load_finding_record_found():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        tmp.write(
            json.dumps(
                {
                    "finding_id": "fid-1",
                    "repo": "r",
                    "path": "p",
                    "line": 1,
                    "rule": "rule",
                    "snippet": "s",
                    "confidence": 0.9,
                    "quick_win": True,
                    "safe_to_autofix": True,
                }
            )
            + "\n"
        )
        tmp.write(
            json.dumps(
                {
                    "finding_id": "fid-2",
                    "repo": "r",
                    "path": "p",
                    "line": 2,
                    "rule": "rule2",
                    "snippet": "s",
                    "confidence": 0.8,
                    "quick_win": False,
                    "safe_to_autofix": False,
                }
            )
            + "\n"
        )
        path = Path(tmp.name)
    try:
        result = load_finding_record("fid-2", path)
        assert result is not None
        assert result["finding_id"] == "fid-2"
        assert result["line"] == 2
    finally:
        path.unlink()


def test_load_finding_record_not_found():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        tmp.write(
            json.dumps(
                {
                    "finding_id": "fid-1",
                    "repo": "r",
                    "path": "p",
                    "line": 1,
                    "rule": "rule",
                    "snippet": "s",
                    "confidence": 0.9,
                    "quick_win": True,
                    "safe_to_autofix": True,
                }
            )
            + "\n"
        )
        path = Path(tmp.name)
    try:
        result = load_finding_record("nonexistent", path)
        assert result is None
    finally:
        path.unlink()


def test_load_finding_record_missing_file():
    result = load_finding_record("x", Path("/tmp/does-not-exist-12345.jsonl"))
    assert result is None


def test_load_finding_record_skips_malformed():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        tmp.write("not valid json\n")
        tmp.write(
            json.dumps(
                {
                    "finding_id": "fid-good",
                    "repo": "r",
                    "path": "p",
                    "line": 1,
                    "rule": "rule",
                    "snippet": "s",
                    "confidence": 0.9,
                    "quick_win": True,
                    "safe_to_autofix": True,
                }
            )
            + "\n"
        )
        tmp.write("also not json\n")
        path = Path(tmp.name)
    try:
        result = load_finding_record("fid-good", path)
        assert result is not None
        assert result["finding_id"] == "fid-good"
    finally:
        path.unlink()


def test_update_finding_record():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        tmp.write(
            json.dumps(
                {
                    "finding_id": "fid-1",
                    "repo": "r",
                    "path": "p",
                    "line": 1,
                    "rule": "rule",
                    "snippet": "s",
                    "confidence": 0.9,
                    "quick_win": True,
                    "safe_to_autofix": True,
                    "fix_attempts": 1,
                }
            )
            + "\n"
        )
        tmp.write(
            json.dumps(
                {
                    "finding_id": "fid-2",
                    "repo": "r",
                    "path": "p",
                    "line": 2,
                    "rule": "rule2",
                    "snippet": "s",
                    "confidence": 0.8,
                    "quick_win": False,
                    "safe_to_autofix": False,
                }
            )
            + "\n"
        )
        path = Path(tmp.name)
    try:
        ok = update_finding_record(
            "fid-1", path, {"fix_success": True, "fix_attempts": 2}
        )
        assert ok is True
        lines = path.read_text().splitlines()
        rec1 = json.loads([l for l in lines if "fid-1" in l][0])
        assert rec1["fix_success"] is True
        assert rec1["fix_attempts"] == 2
        # fid-2 unchanged
        rec2 = json.loads([l for l in lines if "fid-2" in l][0])
        assert rec2["finding_id"] == "fid-2"
    finally:
        path.unlink()


def test_update_finding_record_not_found():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        tmp.write(
            json.dumps(
                {
                    "finding_id": "fid-1",
                    "repo": "r",
                    "path": "p",
                    "line": 1,
                    "rule": "rule",
                    "snippet": "s",
                    "confidence": 0.9,
                    "quick_win": True,
                    "safe_to_autofix": True,
                }
            )
            + "\n"
        )
        path = Path(tmp.name)
    try:
        ok = update_finding_record("nonexistent", path, {"fix_success": True})
        assert ok is False
        # File unchanged
        lines = path.read_text().splitlines()
        assert len(lines) == 1
    finally:
        path.unlink()


def test_update_finding_record_preserves_malformed():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        tmp.write("malformed line\n")
        tmp.write(
            json.dumps(
                {
                    "finding_id": "fid-1",
                    "repo": "r",
                    "path": "p",
                    "line": 1,
                    "rule": "rule",
                    "snippet": "s",
                    "confidence": 0.9,
                    "quick_win": True,
                    "safe_to_autofix": True,
                }
            )
            + "\n"
        )
        path = Path(tmp.name)
    try:
        ok = update_finding_record("fid-1", path, {"fix_success": True})
        assert ok is True
        lines = path.read_text().splitlines()
        assert "malformed line" in lines[0]
    finally:
        path.unlink()


def test_increment_fix_attempt():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        tmp.write(
            json.dumps(
                {
                    "finding_id": "inc-1",
                    "repo": "r",
                    "path": "p",
                    "line": 1,
                    "rule": "rule",
                    "snippet": "s",
                    "confidence": 0.9,
                    "quick_win": True,
                    "safe_to_autofix": True,
                }
            )
            + "\n"
        )
        path = Path(tmp.name)
    try:
        # First attempt
        increment_fix_attempt("inc-1", path, "error A")
        rec = load_finding_record("inc-1", path)
        assert rec["fix_attempts"] == 1
        assert rec["last_fix_error"] == "error A"
        assert rec["last_fix_at"] is not None

        # Second attempt
        increment_fix_attempt("inc-1", path, "error B")
        rec = load_finding_record("inc-1", path)
        assert rec["fix_attempts"] == 2
        assert rec["last_fix_error"] == "error B"

        # No-op if not found
        increment_fix_attempt("nonexistent", path, "error C")
    finally:
        path.unlink()


def test_increment_fix_attempt_truncates_long_error():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        tmp.write(
            json.dumps(
                {
                    "finding_id": "inc-2",
                    "repo": "r",
                    "path": "p",
                    "line": 1,
                    "rule": "rule",
                    "snippet": "s",
                    "confidence": 0.9,
                    "quick_win": True,
                    "safe_to_autofix": True,
                }
            )
            + "\n"
        )
        path = Path(tmp.name)
    try:
        long_error = "X" * 1000
        increment_fix_attempt("inc-2", path, long_error)
        rec = load_finding_record("inc-2", path)
        assert len(rec["last_fix_error"]) == 500
    finally:
        path.unlink()


def test_should_use_mnemo_bypasses_trivial_first_pass_quick_win():
    finding = Finding(
        finding_id="mnemo-trivial",
        repo="repo",
        path="src/test.py",
        line=1,
        rule="ruff-f401",
        snippet="import os",
        confidence=0.99,
        quick_win=True,
        safe_to_autofix=True,
    )
    ok, reason = _should_use_mnemo(finding, None, [])
    assert ok is False
    assert reason == "trivial-first-pass-quick-win"


def test_should_use_mnemo_for_retry_attempts():
    finding = Finding(
        finding_id="mnemo-retry",
        repo="repo",
        path="src/test.py",
        line=1,
        rule="ruff-f401",
        snippet="import os",
        confidence=0.99,
        quick_win=True,
        safe_to_autofix=True,
    )
    ok, reason = _should_use_mnemo(finding, {"fix_attempts": 2}, [])
    assert ok is True
    assert reason == "retry-attempts=2"


def test_should_use_mnemo_for_non_quick_win():
    finding = Finding(
        finding_id="mnemo-hard",
        repo="repo",
        path="src/test.py",
        line=1,
        rule="custom-complex",
        snippet="very complex multi-branch logic here",
        confidence=0.75,
        quick_win=False,
        safe_to_autofix=True,
    )
    ok, reason = _should_use_mnemo(finding, None, [])
    assert ok is True
    assert reason == "not-quick-win"


# ─── Phase 2: utils.py lesson-load function tests ─────────────────────────


def _write_lessons(content: str) -> Path:
    """Helper: write content to a temp LESSONS_LOG.md and return the Path."""
    import tempfile

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix="LESSONS_LOG.md", delete=False)
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


# --- append_lesson with finding_id ---


def test_append_lesson_with_finding_id():
    with tempfile.TemporaryDirectory() as td:
        lessons_file = Path(td) / "LESSONS_LOG.md"
        append_lesson(
            lessons_file,
            "fix-cycle",
            finding_id="abc123",
            what_broke="something broke",
            what_worked="something worked",
        )
        text = lessons_file.read_text()
        assert "finding_id: abc123" in text
        assert "fix-cycle" in text
        assert "something broke" in text
        assert "something worked" in text
        # Empty finding_id should NOT write the tag line
        append_lesson(
            lessons_file,
            "pr-cycle",
            finding_id="",
            what_changed="did a thing",
        )
        text2 = lessons_file.read_text()
        assert "finding_id:" not in text2.split("pr-cycle")[1]


# --- load_lessons_for_finding ---


def test_load_lessons_for_finding_empty():
    """Empty/non-existent file returns empty list."""
    result = load_lessons_for_finding("any", Path("/tmp/no-such-file-12345.md"))
    assert result == []


def test_load_lessons_for_finding_no_match():
    """File with entries but none matching finding_id returns empty list."""
    content = """\n## 2026-03-25 | pr-cycle\n- **Broke:** something\n## 2026-03-24 | fix-cycle\n- **Worked:** something\n"""
    p = _write_lessons(content)
    try:
        result = load_lessons_for_finding("xyz789", p)
        assert result == []
    finally:
        p.unlink()


def test_load_lessons_for_finding_match():
    """Single matching entry is returned correctly."""
    recent_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    content = f"""\n## {recent_date} | fix-cycle\nfinding_id: match-id-42\n- **Broke:** it crashed\n- **Changed:** updated dep\n"""
    p = _write_lessons(content)
    try:
        result = load_lessons_for_finding("match-id-42", p)
        assert len(result) == 1
        assert result[0]["date"] == recent_date
        assert result[0]["cycle_type"] == "fix-cycle"
        assert result[0]["finding_id"] == "match-id-42"
        assert result[0]["broke"] == "it crashed"
        assert result[0]["changed"] == "updated dep"
        assert result[0]["worked"] == ""
    finally:
        p.unlink()


def test_load_lessons_for_finding_multiple_entries_newest_first():
    """Multiple matching entries returned newest-first."""
    content = """\n## 2026-05-10 | pr-cycle\nfinding_id: fid-abc\n- **Worked:** old approach\n\n## 2026-05-15 | fix-cycle\nfinding_id: fid-other\n- **Worked:** other\n\n## 2026-05-19 | fix-cycle\nfinding_id: fid-abc\n- **Broke:** new failure\n- **Changed:** newer approach\n"""
    p = _write_lessons(content)
    try:
        result = load_lessons_for_finding("fid-abc", p)
        assert len(result) == 2
        # Newest first
        assert result[0]["date"] == "2026-05-19"
        assert result[0]["broke"] == "new failure"
        assert result[1]["date"] == "2026-05-10"
        assert result[1]["worked"] == "old approach"
    finally:
        p.unlink()


def test_load_lessons_for_finding_malformed():
    """Malformed lines are skipped without raising."""
    recent_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    content = f"""\n## {recent_date} | fix-cycle\nfinding_id: fid-mal\nnot a valid line\n- **Broke:** valid break\n- **Changed:** !! invalid syntax here !!\n"""
    p = _write_lessons(content)
    try:
        result = load_lessons_for_finding("fid-mal", p)
        assert len(result) == 1
        assert result[0]["broke"] == "valid break"
    finally:
        p.unlink()


def test_load_lessons_for_finding_no_finding_id_omitted():
    """Entry without finding_id tag is NOT returned (cannot be attributed)."""
    recent_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    content = f"""\n## {recent_date} | fix-cycle\n- **Worked:** untagged entry\n\n## {recent_date} | fix-cycle\nfinding_id: fid-123\n- **Worked:** tagged entry\n"""
    p = _write_lessons(content)
    try:
        result = load_lessons_for_finding("fid-123", p)
        assert len(result) == 1
        assert result[0]["worked"] == "tagged entry"
    finally:
        p.unlink()


# ─── V2: Noise guard, enriched entries, rotation, repo path, rule search ────


def test_cycle_entry_skipped_when_blocked_only():
    with tempfile.TemporaryDirectory() as td:
        lessons_file2 = Path(td) / "lessons2.md"
        append_lesson(lessons_file2, "issue-cycle")
        assert not lessons_file2.exists()


def test_cycle_entry_written_when_fixes_verified():
    with tempfile.TemporaryDirectory() as td:
        lessons_file = Path(td) / "lessons_log.md"
        append_lesson(
            lessons_file, "pr-cycle", what_changed="2 fixes verified; 1 PRs created"
        )
        text = lessons_file.read_text()
        assert "2 fixes verified" in text
        assert "PRs created" in text


def test_fix_cycle_includes_path_on_success():
    with tempfile.TemporaryDirectory() as td:
        lessons_file = Path(td) / "lessons_log.md"
        append_lesson(
            lessons_file,
            "fix",
            finding_id="abc123",
            what_changed="fix succeeded rule=ruff-b904 path=src/orders.py:42",
        )
        text = lessons_file.read_text()
        assert "path=src/orders.py:42" in text
        assert "ruff-b904" in text


def test_fix_cycle_includes_error_on_failure():
    with tempfile.TemporaryDirectory() as td:
        lessons_file = Path(td) / "lessons_log.md"
        append_lesson(
            lessons_file,
            "fix",
            finding_id="abc123",
            what_broke="fix failed rc=1 rule=ruff-b904 path=src/orders.py:42: TypeError: expected str",
        )
        text = lessons_file.read_text()
        assert "TypeError: expected str" in text
        assert "path=src/orders.py:42" in text


def test_rotation_trims_to_max_entries():
    import bluei.engine.utils as utils_mod

    original_max = utils_mod._MAX_LESSON_ENTRIES
    try:
        utils_mod._MAX_LESSON_ENTRIES = 5
        with tempfile.TemporaryDirectory() as td:
            lessons_file = Path(td) / "lessons_log.md"
            for i in range(8):
                append_lesson(
                    lessons_file, "fix", finding_id=f"fid-{i}", what_changed=f"fix {i}"
                )
            content = lessons_file.read_text()
            entries = [e for e in content.split("\n## ") if e.strip()]
            assert len(entries) == 5
            assert "fix 7" in content
            assert "fix 0" not in content
    finally:
        utils_mod._MAX_LESSON_ENTRIES = original_max


def test_rotation_skipped_when_under_limit():
    import bluei.engine.utils as utils_mod

    original_max = utils_mod._MAX_LESSON_ENTRIES
    try:
        utils_mod._MAX_LESSON_ENTRIES = 100
        with tempfile.TemporaryDirectory() as td:
            lessons_file = Path(td) / "lessons_log.md"
            for i in range(3):
                append_lesson(
                    lessons_file, "fix", finding_id=f"fid-{i}", what_changed=f"fix {i}"
                )
            content = lessons_file.read_text()
            entries = [e for e in content.split("\n## ") if e.strip()]
            assert len(entries) == 3
    finally:
        utils_mod._MAX_LESSON_ENTRIES = original_max


def test_rotation_preserves_entry_format():
    import bluei.engine.utils as utils_mod

    original_max = utils_mod._MAX_LESSON_ENTRIES
    try:
        utils_mod._MAX_LESSON_ENTRIES = 2
        with tempfile.TemporaryDirectory() as td:
            lessons_file = Path(td) / "lessons_log.md"
            append_lesson(lessons_file, "fix", finding_id="old", what_changed="old fix")
            append_lesson(
                lessons_file,
                "fix",
                finding_id="kept-1",
                what_broke="error A",
                what_changed="approach B",
            )
            append_lesson(
                lessons_file, "fix", finding_id="kept-2", what_changed="new fix"
            )
            results = load_lessons_for_finding("kept-1", lessons_file)
            assert len(results) == 1
            assert results[0]["finding_id"] == "kept-1"
            assert results[0]["broke"] == "error A"
    finally:
        utils_mod._MAX_LESSON_ENTRIES = original_max


def test_repo_lessons_path_uses_bluei_home():
    from bluei.engine.constants import repo_lessons_path, bluei_home

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "my-project"
        repo.mkdir()
        path = repo_lessons_path(repo)
        assert path.name == "lessons_log.md"
        assert str(bluei_home() / "repos") in str(path)
        assert "my-project" in str(path)


def test_bluei_repo_dir_slug_dedup():
    from bluei.engine.constants import (
        bluei_repo_dir,
        _load_bluei_registry,
        _slugify_repo_name,
    )
    import json

    with tempfile.TemporaryDirectory() as td:
        repo1 = Path(td) / "my-app"
        repo2 = Path(td) / "other" / "my-app"
        repo1.mkdir(parents=True)
        repo2.mkdir(parents=True)

        dir1 = bluei_repo_dir(repo1)
        dir2 = bluei_repo_dir(repo2)

        assert dir1.name == "my-app"
        assert dir2.name == "my-app-1"
        assert dir1 != dir2

        registry = _load_bluei_registry()
        assert str(repo1.resolve()) in registry["repos"]
        assert str(repo2.resolve()) in registry["repos"]


def test_bluei_repo_dir_idempotent():
    from bluei.engine.constants import bluei_repo_dir

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "unique-name"
        repo.mkdir()
        dir1 = bluei_repo_dir(repo)
        dir2 = bluei_repo_dir(repo)
        assert dir1 == dir2


def test_load_lessons_for_rule_returns_matching_entries():
    content = (
        "\n## 2026-04-08 | fix\nfinding_id: fid-a\n"
        "- **Changed:** fix succeeded rule=ruff-b904 path=src/a.py:1\n"
        "\n## 2026-04-08 | fix\nfinding_id: fid-b\n"
        "- **Changed:** fix succeeded rule=xo-complexity path=src/b.py:1\n"
        "\n## 2026-04-08 | fix\nfinding_id: fid-c\n"
        "- **Changed:** fix succeeded rule=ruff-b904 path=src/c.py:1\n"
    )
    p = _write_lessons(content)
    try:
        result = load_lessons_for_rule("ruff-b904", p)
        assert len(result) == 2
        assert all(
            "ruff-b904" in (e.get("changed", "") + e.get("broke", "")) for e in result
        )
    finally:
        p.unlink()


def test_load_lessons_for_rule_excludes_non_matching():
    content = (
        "\n## 2026-04-08 | fix\nfinding_id: fid-a\n"
        "- **Changed:** fix succeeded rule=ruff-b904\n"
        "\n## 2026-04-08 | fix\nfinding_id: fid-b\n"
        "- **Changed:** fix succeeded rule=xo-complexity\n"
    )
    p = _write_lessons(content)
    try:
        result = load_lessons_for_rule("ruff-b904", p)
        assert len(result) == 1
        assert result[0]["finding_id"] == "fid-a"
    finally:
        p.unlink()


def test_load_lessons_for_rule_respects_limit():
    entries = []
    for i in range(10):
        entries.append(
            f"\n## 2026-04-08 | fix\nfinding_id: fid-{i}\n- **Changed:** fix succeeded rule=ruff-b904 path=src/{i}.py:1\n"
        )
    p = _write_lessons("".join(entries))
    try:
        result = load_lessons_for_rule("ruff-b904", p, limit=3)
        assert len(result) == 3
    finally:
        p.unlink()


def test_load_lessons_for_rule_applies_decay():
    old_date = "2026-01-01"
    recent_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    content = (
        f"\n## {old_date} | fix\nfinding_id: fid-old\n"
        f"- **Changed:** fix succeeded rule=ruff-b904\n"
        f"\n## {recent_date} | fix\nfinding_id: fid-new\n"
        f"- **Changed:** fix succeeded rule=ruff-b904\n"
    )
    p = _write_lessons(content)
    try:
        result = load_lessons_for_rule("ruff-b904", p)
        assert len(result) == 1
        assert result[0]["finding_id"] == "fid-new"
    finally:
        p.unlink()


def test_load_lessons_for_rule_empty_file():
    result = load_lessons_for_rule("any", Path("/tmp/no-such-file-12345.md"))
    assert result == []


def test_prompt_renders_rule_history_section():
    from bluei.engine.prompts import render_claude_fix_prompt

    f = Finding(
        finding_id="test",
        repo="r",
        path="p.py",
        line=1,
        rule="ruff-b904",
        snippet="raise",
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=False,
    )
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        rule_history=[
            {
                "date": "2026-04-08",
                "cycle_type": "fix",
                "finding_id": "abc",
                "broke": "",
                "changed": "fix succeeded rule=ruff-b904",
                "worked": "",
            }
        ],
    )
    assert "## Similar fixes in this codebase" in prompt
    assert "ruff-b904" in prompt


def test_prompt_omits_rule_history_when_empty():
    from bluei.engine.prompts import render_claude_fix_prompt

    f = Finding(
        finding_id="test",
        repo="r",
        path="p.py",
        line=1,
        rule="ruff-b904",
        snippet="raise",
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=False,
    )
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        rule_history=[],
    )
    assert "## Similar fixes in this codebase" not in prompt


def test_prompt_rule_history_shows_finding_id_short():
    from bluei.engine.prompts import render_claude_fix_prompt

    f = Finding(
        finding_id="test",
        repo="r",
        path="p.py",
        line=1,
        rule="ruff-b904",
        snippet="raise",
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=False,
    )
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        rule_history=[
            {
                "date": "2026-04-08",
                "cycle_type": "fix",
                "finding_id": "abcdef1234567890",
                "broke": "",
                "changed": "fix ok",
                "worked": "",
            }
        ],
    )
    assert "[abcdef12]" in prompt


# ─── V2 Next: Registry GC, decay pruning, failure clustering ────


def test_registry_gc_removes_stale_entries():
    from bluei.engine.constants import (
        _cleanup_stale_registry,
        _load_bluei_registry,
        _save_bluei_registry,
    )
    import json

    stale_key = "/tmp/bluei-test-nonexistent-repo"
    stale_data = json.dumps(
        {
            "repos": {
                stale_key: {
                    "slug": "nonexistent",
                    "path": stale_key,
                    "registered_at": "",
                },
            }
        }
    )
    stale_registry = __import__("pathlib").Path.home() / ".bluei" / "registry.json"
    stale_registry.parent.mkdir(parents=True, exist_ok=True)
    stale_registry.write_text(stale_data, encoding="utf-8")
    try:
        _cleanup_stale_registry()
        registry = _load_bluei_registry()
        assert stale_key not in registry.get("repos", {})
    finally:
        stale_registry.unlink(missing_ok=True)


def test_rotation_evicts_old_entries():
    import bluei.engine.utils as utils_mod

    original_max = utils_mod._MAX_LESSON_ENTRIES
    original_decay = utils_mod._LESSON_DECAY_DAYS
    try:
        utils_mod._MAX_LESSON_ENTRIES = 100
        utils_mod._LESSON_DECAY_DAYS = 1
        old_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        recent_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as td:
            lessons_file = Path(td) / "lessons_log.md"
            old_content = (
                f"\n## {old_date} | fix\nfinding_id: old-id\n- **Changed:** old fix\n"
            )
            recent_content = f"\n## {recent_date} | fix\nfinding_id: recent-id\n- **Changed:** recent fix\n"
            lessons_file.write_text(old_content + recent_content, encoding="utf-8")
            content = lessons_file.read_text(encoding="utf-8")
            entries = [e for e in content.split("\n## ") if e.strip()]
            assert len(entries) == 2
            utils_mod._rotate_lessons_if_needed(lessons_file)
            content = lessons_file.read_text(encoding="utf-8")
            assert "recent fix" in content
            assert "old fix" not in content
    finally:
        utils_mod._MAX_LESSON_ENTRIES = original_max
        utils_mod._LESSON_DECAY_DAYS = original_decay


def test_rotation_keeps_recent_entries():
    import bluei.engine.utils as utils_mod

    original_max = utils_mod._MAX_LESSON_ENTRIES
    original_decay = utils_mod._LESSON_DECAY_DAYS
    try:
        utils_mod._MAX_LESSON_ENTRIES = 100
        utils_mod._LESSON_DECAY_DAYS = 60
        recent_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as td:
            lessons_file = Path(td) / "lessons_log.md"
            for i in range(3):
                append_lesson(
                    lessons_file, "fix", finding_id=f"fid-{i}", what_changed=f"fix {i}"
                )
            content = lessons_file.read_text(encoding="utf-8")
            entries = [e for e in content.split("\n## ") if e.strip()]
            assert len(entries) == 3
    finally:
        utils_mod._MAX_LESSON_ENTRIES = original_max
        utils_mod._LESSON_DECAY_DAYS = original_decay


def test_load_failure_clusters_empty_file():
    from bluei.engine.utils import load_failure_clusters_for_rule

    result = load_failure_clusters_for_rule("any", Path("/tmp/no-such-file-12345.md"))
    assert result is None


def test_load_failure_clusters_detects_pattern():
    from bluei.engine.utils import load_failure_clusters_for_rule

    content = []
    for i in range(5):
        content.append(
            f"\n## 2026-04-08 | fix\nfinding_id: fid-{i}\n"
            f"- **Broke:** fix failed rc=1 rule=ruff-b904 path=src/{i}.py:1: TypeError: expected str\n"
        )
    p = _write_lessons("".join(content))
    try:
        result = load_failure_clusters_for_rule("ruff-b904", p, min_count=3)
        assert result is not None
        assert "TypeError: expected str" in result
        assert "5/5" in result
    finally:
        p.unlink()


def test_load_failure_clusters_below_threshold():
    from bluei.engine.utils import load_failure_clusters_for_rule

    content = []
    for i in range(5):
        content.append(
            f"\n## 2026-04-08 | fix\nfinding_id: fid-{i}\n"
            f"- **Broke:** fix failed rc=1 rule=ruff-b904 path=src/{i}.py:1: error-{i}\n"
        )
    p = _write_lessons("".join(content))
    try:
        result = load_failure_clusters_for_rule(
            "ruff-b904", p, min_count=3, min_ratio=0.3
        )
        assert result is None
    finally:
        p.unlink()


def test_load_failure_clusters_respects_min_count():
    from bluei.engine.utils import load_failure_clusters_for_rule

    content = []
    for i in range(2):
        content.append(
            f"\n## 2026-04-08 | fix\nfinding_id: fid-{i}\n"
            f"- **Broke:** fix failed rc=1 rule=ruff-b904 path=src/{i}.py:1: SameError\n"
        )
    p = _write_lessons("".join(content))
    try:
        result = load_failure_clusters_for_rule("ruff-b904", p, min_count=3)
        assert result is None
    finally:
        p.unlink()


def test_prompt_renders_failure_clusters():
    from bluei.engine.prompts import render_claude_fix_prompt

    f = Finding(
        finding_id="test",
        repo="r",
        path="p.py",
        line=1,
        rule="ruff-b904",
        snippet="raise",
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=False,
    )
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        failure_clusters="- 5/5 failures: `TypeError: expected str`",
    )
    assert "## Failure patterns for this rule" in prompt
    assert "TypeError" in prompt


def test_prompt_omits_failure_clusters_when_none():
    from bluei.engine.prompts import render_claude_fix_prompt

    f = Finding(
        finding_id="test",
        repo="r",
        path="p.py",
        line=1,
        rule="ruff-b904",
        snippet="raise",
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=False,
    )
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
    )
    assert "## Failure patterns for this rule" not in prompt


# ─── Phase 3: Exponential Backoff Cooldown ─────────────────────────────────

import tempfile as _tempfile
from pathlib import Path as _Path

from bluei.engine.state import (
    get_effective_cooldown,
    mark_finding_activity,
    filter_findings_by_cooldown,
    MAX_COOLDOWN_SECONDS,
)
from bluei.engine.models import Finding


def test_effective_cooldown_no_activity():
    """No activity entry → returns base cooldown (no errors)."""
    state = {"finding_activity": {}}
    result = get_effective_cooldown("fid-x", state, base_cooldown_seconds=3600)
    assert result == 3600


def test_effective_cooldown_zero_failures():
    """failure_count=0 → returns base cooldown."""
    state = {
        "finding_activity": {
            "fid-1": {"failure_count": 0, "last_action": "fix-attempt"}
        }
    }
    result = get_effective_cooldown("fid-1", state, base_cooldown_seconds=3600)
    assert result == 3600


def test_effective_cooldown_one_failure():
    """failure_count=1 → base * 2^1 = base * 2."""
    state = {"finding_activity": {"fid-1": {"failure_count": 1}}}
    result = get_effective_cooldown("fid-1", state, base_cooldown_seconds=3600)
    assert result == 7200


def test_effective_cooldown_exponential():
    """failure_count=3 → base * 2^3."""
    state = {"finding_activity": {"fid-1": {"failure_count": 3}}}
    result = get_effective_cooldown("fid-1", state, base_cooldown_seconds=3600)
    assert result == 3600 * 8  # 28800


def test_effective_cooldown_capped_at_7_days():
    """failure_count high enough to exceed 7-day cap → capped."""
    state = {"finding_activity": {"fid-1": {"failure_count": 10}}}
    result = get_effective_cooldown("fid-1", state, base_cooldown_seconds=3600)
    assert result == MAX_COOLDOWN_SECONDS
    assert result == 7 * 24 * 60 * 60


def test_mark_finding_activity_with_failure_count():
    """mark_finding_activity stores failure_count and last_error."""
    state = {"finding_activity": {}}
    mark_finding_activity(
        state,
        ["fid-1"],
        "fix-attempt",
        failure_count=2,
        last_error="ruff failed",
    )
    entry = state["finding_activity"]["fid-1"]
    assert entry["failure_count"] == 2
    assert entry["last_error"] == "ruff failed"
    assert entry["last_action"] == "fix-attempt"
    assert "last_action_at" in entry


def test_mark_finding_activity_resets_failure_count():
    """Existing entry fields are preserved when failure_count is updated."""
    state = {
        "finding_activity": {
            "fid-old": {
                "last_action": "fix-attempt",
                "last_action_at": "2026-01-01T00:00:00Z",
                "failure_count": 5,
                "last_error": "old error",
            }
        }
    }
    mark_finding_activity(
        state,
        ["fid-old"],
        "fix-attempt",
        failure_count=0,
    )
    entry = state["finding_activity"]["fid-old"]
    assert entry["failure_count"] == 0
    # Previous fields preserved:
    assert entry["last_error"] == "old error"


def test_filter_finds_suppressed_with_extended_cooldown():
    """Findings with failure_count get extended cooldown via get_effective_cooldown."""
    from datetime import datetime, timezone, timedelta

    with _tempfile.TemporaryDirectory() as td:
        log_file = _Path(td) / "runner.log"

        # Use a recent timestamp so elapsed time is within the extended cooldown window
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()

        # State: finding has failure_count=1, making effective cooldown 2x
        state = {
            "finding_activity": {
                "fid-extended": {
                    "last_action": "fix-attempt",
                    "last_action_at": recent_time,
                    "failure_count": 1,
                }
            }
        }

        findings = [
            Finding(
                finding_id="fid-extended",
                repo="r",
                path="p.py",
                line=1,
                rule="R001",
                snippet="x=1",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            )
        ]

        # Base cooldown = 3600s; effective = 7200s (2h) due to failure_count=1.
        # Elapsed = 30 min = 1800s < 7200s → MUST be suppressed.
        allowed, suppressed = filter_findings_by_cooldown(
            findings, state, cooldown_seconds=3600, log_file=log_file
        )
        assert allowed == []
        assert len(suppressed) == 1
        assert suppressed[0].finding_id == "fid-extended"


# ─── Phase 4: Prompt injection tests ───────────────────────────────────────

from bluei.engine.prompts import render_claude_fix_prompt


def test_prompt_has_prior_context_when_history_exists(make_finding):
    """When fix_history is non-empty, prompt must contain ## Prior context section."""
    f = make_finding(
        finding_id="test-001",
        repo="test-repo",
        path="src/test.py",
        line=10,
        rule="ruff-b007",
        snippet="x = 1",
        confidence=0.9,
    )
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        fix_history=[
            {
                "date": "2026-03-24",
                "cycle_type": "fix-cycle",
                "finding_id": "test-001",
                "broke": "ruff rc=1",
                "changed": "",
                "worked": "",
            },
        ],
    )
    assert "## Prior context" in prompt
    assert "2026-03-24" in prompt
    assert "ruff rc=1" in prompt


def test_prompt_omits_prior_context_when_empty(make_finding):
    """When fix_history is [], prompt must NOT contain ## Prior context."""
    f = make_finding(
        finding_id="test-001",
        repo="test-repo",
        path="src/test.py",
        line=10,
        rule="ruff-b007",
        snippet="x = 1",
        confidence=0.9,
    )
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        fix_history=[],
    )
    assert "## Prior context" not in prompt


def test_prompt_omits_prior_context_when_none(make_finding):
    """When fix_history is None (default), prompt must NOT contain ## Prior context."""
    f = make_finding(
        finding_id="test-001",
        repo="test-repo",
        path="src/test.py",
        line=10,
        rule="ruff-b007",
        snippet="x = 1",
        confidence=0.9,
    )
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        # fix_history not passed → defaults to None
    )
    assert "## Prior context" not in prompt


def test_prompt_has_fix_history_when_attempts_gt_zero(make_finding):
    """When finding_record has fix_attempts > 0, prompt must contain ## Fix history."""
    f = make_finding(
        finding_id="test-001",
        repo="test-repo",
        path="src/test.py",
        line=10,
        rule="ruff-b007",
        snippet="x = 1",
        confidence=0.9,
    )
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        finding_record={
            "fix_attempts": 3,
            "last_fix_error": "ruff rc=1",
            "last_fix_at": "2026-03-25T00:00:00Z",
        },
    )
    assert "## Fix history" in prompt
    assert "Attempts: 3" in prompt
    assert "ruff rc=1" in prompt
    assert "known-difficult" in prompt


def test_prompt_omits_fix_history_when_attempts_zero(make_finding):
    """When finding_record has fix_attempts=0, ## Fix history must be absent."""
    f = make_finding(
        finding_id="test-001",
        repo="test-repo",
        path="src/test.py",
        line=10,
        rule="ruff-b007",
        snippet="x = 1",
        confidence=0.9,
    )
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        finding_record={"fix_attempts": 0, "last_fix_error": None},
    )
    assert "## Fix history" not in prompt


def test_prompt_omits_fix_history_when_record_none(make_finding):
    """When finding_record is None (default), ## Fix history must be absent."""
    f = make_finding(
        finding_id="test-001",
        repo="test-repo",
        path="src/test.py",
        line=10,
        rule="ruff-b007",
        snippet="x = 1",
        confidence=0.9,
    )
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
    )
    assert "## Fix history" not in prompt


def test_prompt_has_both_sections_when_both_provided(make_finding):
    """When both fix_history and finding_record are non-empty, both sections appear."""
    f = make_finding(
        finding_id="test-001",
        repo="test-repo",
        path="src/test.py",
        line=10,
        rule="ruff-b007",
        snippet="x = 1",
        confidence=0.9,
    )
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        fix_history=[
            {
                "date": "2026-03-24",
                "cycle_type": "fix-cycle",
                "finding_id": "test-001",
                "broke": "old error",
                "changed": "",
                "worked": "",
            }
        ],
        finding_record={"fix_attempts": 2, "last_fix_error": "recent error"},
    )
    assert "## Prior context" in prompt
    assert "## Fix history" in prompt
    assert "Attempts: 2" in prompt
    assert "old error" in prompt
    assert "recent error" in prompt


def run_tests():
    tests = [
        test_finding_from_dict_roundtrip_new_record,
        test_finding_from_dict_roundtrip_old_record,
        test_finding_from_dict_partial_record,
        test_finding_as_dict_includes_defaults,
        test_finding_as_dict_includes_non_defaults,
        test_load_finding_record_found,
        test_load_finding_record_not_found,
        test_load_finding_record_missing_file,
        test_load_finding_record_skips_malformed,
        test_update_finding_record,
        test_update_finding_record_not_found,
        test_update_finding_record_preserves_malformed,
        test_increment_fix_attempt,
        test_increment_fix_attempt_truncates_long_error,
        # Phase 2: append_lesson + load_lessons_for_finding
        test_append_lesson_with_finding_id,
        test_load_lessons_for_finding_empty,
        test_load_lessons_for_finding_no_match,
        test_load_lessons_for_finding_match,
        test_load_lessons_for_finding_multiple_entries_newest_first,
        test_load_lessons_for_finding_malformed,
        test_load_lessons_for_finding_no_finding_id_omitted,
        # V2: Noise guard
        test_cycle_entry_skipped_when_blocked_only,
        test_cycle_entry_written_when_fixes_verified,
        # V2: Enriched entries
        test_fix_cycle_includes_path_on_success,
        test_fix_cycle_includes_error_on_failure,
        # V2: Rotation
        test_rotation_trims_to_max_entries,
        test_rotation_skipped_when_under_limit,
        test_rotation_preserves_entry_format,
        # V2: Repo-scoped path (via ~/.bluei/)
        test_repo_lessons_path_uses_bluei_home,
        test_bluei_repo_dir_slug_dedup,
        test_bluei_repo_dir_idempotent,
        # V2: load_lessons_for_rule
        test_load_lessons_for_rule_returns_matching_entries,
        test_load_lessons_for_rule_excludes_non_matching,
        test_load_lessons_for_rule_respects_limit,
        test_load_lessons_for_rule_applies_decay,
        test_load_lessons_for_rule_empty_file,
        # V2: Prompt rule history
        test_prompt_renders_rule_history_section,
        test_prompt_omits_rule_history_when_empty,
        test_prompt_rule_history_shows_finding_id_short,
        # V2 Next: Registry GC
        test_registry_gc_removes_stale_entries,
        # V2 Next: Decay pruning
        test_rotation_evicts_old_entries,
        test_rotation_keeps_recent_entries,
        # V2 Next: Failure pattern clustering
        test_load_failure_clusters_empty_file,
        test_load_failure_clusters_detects_pattern,
        test_load_failure_clusters_below_threshold,
        test_load_failure_clusters_respects_min_count,
        test_prompt_renders_failure_clusters,
        test_prompt_omits_failure_clusters_when_none,
        # Phase 3: Exponential Backoff Cooldown
        test_effective_cooldown_no_activity,
        test_effective_cooldown_zero_failures,
        test_effective_cooldown_one_failure,
        test_effective_cooldown_exponential,
        test_effective_cooldown_capped_at_7_days,
        test_mark_finding_activity_with_failure_count,
        test_mark_finding_activity_resets_failure_count,
        test_filter_finds_suppressed_with_extended_cooldown,
        # Phase 4: Prompt injection
        test_prompt_has_prior_context_when_history_exists,
        test_prompt_omits_prior_context_when_empty,
        test_prompt_omits_prior_context_when_none,
        test_prompt_has_fix_history_when_attempts_gt_zero,
        test_prompt_omits_fix_history_when_attempts_zero,
        test_prompt_omits_fix_history_when_record_none,
        test_prompt_has_both_sections_when_both_provided,
    ]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed.append(t.__name__)
    print()
    if failed:
        print(f"FAILED: {len(failed)}/{len(tests)}")
        sys.exit(1)
    else:
        print(f"PASSED: all {len(tests)} tests")


if __name__ == "__main__":
    run_tests()
