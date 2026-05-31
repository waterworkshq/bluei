#!/usr/bin/env python3
"""test_mnemo_client.py — Tests for mnemo_client module (Python-native engram)."""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure workspace root is on sys.path
_workdir = os.environ.get("WORKSPACE_ROOT")
if _workdir:
    sys.path.insert(0, _workdir)
else:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from bluei.engine.models import Finding

# Mock subprocess before importing mnemo_client
with patch("subprocess.run") as mock_run:
    with patch("subprocess.Popen"):
        from bluei.engine import mnemo_client

        mnemo_client._mnemo_available_cache.clear()  # Reset cache

from bluei.engine.mnemo_client import (
    MnemoClient,
    is_mnemo_available,
    MNEMO_MAX_CHARS,
    MNEMO_RECALL_LIMIT,
    PatternMatch,
    FileRelevance,
    CallGraphEntry,
    Dependency,
    _get_conn,
    _project_for,
)


def _old_finding(make_finding):
    """Wrap conftest make_finding with the original _make_finding defaults."""
    return make_finding(
        finding_id="fid-test-001",
        path="src/test.py",
        line=10,
        rule="RUF100",
        snippet="x = 1  # unused assignment",
        confidence=0.9,
    )


def _sqlite_finding(make_finding, **overrides):
    """Wrap conftest make_finding with SQLite-specific defaults."""
    defaults = {
        "finding_id": "f001",
        "rule": "ruff-c408",
        "path": "src/lib/foo.py",
        "line": 42,
        "confidence": 0.72,
        "snippet": "dict()",
    }
    defaults.update(overrides)
    return make_finding(**defaults)


def test_is_mnemo_available_cache():
    """is_mnemo_available caches result after first call."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        assert is_mnemo_available(Path("/fake")) is False
        # Second call returns cached value (no re-check)
        assert is_mnemo_available(Path("/fake")) is False


def test_is_mnemo_available_is_per_repo():
    """Availability cache is scoped per repo, not global across all repos."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
        mnemo_client._mnemo_available_cache.clear()
        with patch.object(Path, "exists", side_effect=[False, True]):
            assert is_mnemo_available(Path("/fake-a")) is False
            assert is_mnemo_available(Path("/fake-b")) is True


def test_project_name_loaded_from_mnemo_config():
    """MnemoClient should use the configured project name, not the absolute repo path."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / ".mnemo").mkdir()
        (repo / ".mnemo" / "config.json").write_text(
            json.dumps({"project": {"name": "demo-project"}}),
            encoding="utf-8",
        )
        client = MnemoClient(repo)
        assert client._project == "demo-project"


def test_mnemo_client_unavailable():
    """MnemoClient reports unavailable when MNEMO_ENABLED=0."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        assert client.is_available() is False


def test_find_relevant_files_unavailable():
    """find_relevant_files returns empty list when unavailable."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        result = client.find_relevant_files("test", limit=5)
    assert result == []


def test_search_patterns_unavailable():
    """search_patterns returns empty list when unavailable."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        result = client.search_patterns("test", limit=5)
    assert result == []


def test_get_callers_unavailable():
    """get_callers returns empty list when unavailable."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        result = client.get_callers("test_func", limit=5)
    assert result == []


def test_get_callees_unavailable():
    """get_callees returns empty list when unavailable."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        result = client.get_callees("test_func", limit=5)
    assert result == []


def test_get_dependencies_unavailable():
    """get_dependencies returns empty list when unavailable."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        result = client.get_dependencies("src/test.py", limit=5)
    assert result == []


def test_get_symbols_for_file_unavailable():
    """get_symbols_for_file returns empty list when unavailable."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        result = client.get_symbols_for_file("src/test.py", limit=5)
    assert result == []


def test_get_context_for_finding_unavailable(make_finding):
    """get_context_for_finding returns None when unavailable."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        f = _old_finding(make_finding)
        result = client.get_context_for_finding(f)
    assert result is None


def test_recall_unavailable(make_finding):
    """recall() returns None when mnemo is unavailable."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        f = _old_finding(make_finding)
        result = client.recall(f)
    assert result is None


def test_seed_unavailable(make_finding):
    """seed() returns False when mnemo is unavailable."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        f = _old_finding(make_finding)
        result = client.seed(f, "SUCCESS", None, None)
    assert result is False


def test_extract_search_terms():
    """_extract_search_terms returns relevant terms from finding."""
    f = Finding(
        finding_id="t1",
        repo="repo",
        path="test.py",
        line=1,
        rule="ruff-b904",
        snippet='raise JsonableError(_("Error message")) from err',
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=False,
    )
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        terms = client._extract_search_terms(f)
    # Must include rule name
    assert "ruff-b904" in terms
    # Must include rule-specific keywords
    assert "raise from err" in terms
    # Must include class name
    assert "JsonableError" in terms


def test_pattern_match_format_brief():
    """PatternMatch.format_brief() formats correctly."""
    from bluei.engine.mnemo_client import PatternMatch

    pm = PatternMatch(
        pattern_type="function",
        content="def my_func(): pass",
        file_path="src/test.py",
        line_number=42,
        frequency=5,
    )
    assert "function" in pm.format_brief()
    assert "42" in pm.format_brief()
    assert "test.py" in pm.format_brief()


def test_file_relevance_format():
    """FileRelevance.format() formats correctly."""
    from bluei.engine.mnemo_client import FileRelevance

    fr = FileRelevance(
        file_path="src/utils.py",
        match_count=10,
        pattern_types=["function", "class"],
    )
    output = fr.format()
    assert "src/utils.py" in output
    assert "10" in output


def test_dependency_format():
    """Dependency.format() formats correctly."""
    from bluei.engine.mnemo_client import Dependency

    d = Dependency(source_file="test.py", imported_module="os", imported_name="path")
    assert "import os.path" in d.format()

    d2 = Dependency(source_file="test.py", imported_module="os", imported_name=None)
    assert "import os" in d2.format()


def run_tests():
    tests = [
        test_is_mnemo_available_cache,
        test_is_mnemo_available_is_per_repo,
        test_project_name_loaded_from_mnemo_config,
        test_mnemo_client_unavailable,
        test_find_relevant_files_unavailable,
        test_search_patterns_unavailable,
        test_get_callers_unavailable,
        test_get_callees_unavailable,
        test_get_dependencies_unavailable,
        test_get_symbols_for_file_unavailable,
        test_get_context_for_finding_unavailable,
        test_recall_unavailable,
        test_seed_unavailable,
        test_extract_search_terms,
        test_pattern_match_format_brief,
        test_file_relevance_format,
        test_dependency_format,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS: {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {t.__name__} — {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)


# --- Phase 9: SQLite-backed mnemo tests ---


def _sqlite_finding(
    make_finding,
    finding_id="f001",
    rule="ruff-c408",
    path="src/lib/foo.py",
    line=42,
    confidence=0.72,
    quick_win=True,
    safe_to_autofix=True,
    snippet="dict()",
    repo="test-repo",
    **overrides,
) -> Finding:
    defaults = {
        "finding_id": finding_id,
        "repo": repo,
        "path": path,
        "line": line,
        "rule": rule,
        "snippet": snippet,
        "confidence": confidence,
        "quick_win": quick_win,
        "safe_to_autofix": safe_to_autofix,
    }
    defaults.update(overrides)
    return Finding(**defaults)


def _create_engram_db(
    repo_path: Path, project_name: str = "test-project"
) -> sqlite3.Connection:
    mnemo_dir = repo_path / ".mnemo" / "db"
    mnemo_dir.mkdir(parents=True, exist_ok=True)
    config_dir = repo_path / ".mnemo"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps({"project": {"name": project_name}}), encoding="utf-8"
    )
    db_path = mnemo_dir / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS engram_patterns (
            project TEXT, pattern_type TEXT, content TEXT,
            file_path TEXT, line_number INTEGER, frequency INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS engram_calls (
            project TEXT, caller_file TEXT, caller_symbol TEXT,
            callee_file TEXT, callee_symbol TEXT,
            line_number INTEGER, is_async INTEGER, confidence REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS engram_dependencies (
            project TEXT, source_file TEXT, imported_module TEXT, imported_name TEXT
        )
    """)
    conn.commit()
    return conn


def _seed_patterns(conn: sqlite3.Connection, project: str, rows: list):
    for r in rows:
        conn.execute(
            "INSERT INTO engram_patterns (project, pattern_type, content, file_path, line_number, frequency) VALUES (?,?,?,?,?,?)",
            (project, *r),
        )
    conn.commit()


def _seed_calls(conn: sqlite3.Connection, project: str, rows: list):
    for r in rows:
        conn.execute(
            "INSERT INTO engram_calls (project, caller_file, caller_symbol, callee_file, callee_symbol, line_number, is_async, confidence) VALUES (?,?,?,?,?,?,?,?)",
            (project, *r),
        )
    conn.commit()


def _seed_deps(conn: sqlite3.Connection, project: str, rows: list):
    for r in rows:
        conn.execute(
            "INSERT INTO engram_dependencies (project, source_file, imported_module, imported_name) VALUES (?,?,?,?)",
            (project, *r),
        )
    conn.commit()


@pytest.fixture(autouse=True)
def _clear_mnemo_caches():
    from bluei.engine import mnemo_client

    mnemo_client._mnemo_available_cache.clear()
    mnemo_client._conn_cache.clear()
    yield
    mnemo_client._mnemo_available_cache.clear()
    mnemo_client._conn_cache.clear()


class TestMnemoClientSQLite:
    """Tests for MnemoClient with a real SQLite engram DB."""

    def test_is_available_with_db(self, tmp_path: Path) -> None:
        _create_engram_db(tmp_path)
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            assert client.is_available() is True

    def test_is_available_no_db(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            assert client.is_available() is False

    def test_get_conn_returns_connection(self, tmp_path: Path) -> None:
        _create_engram_db(tmp_path)
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            conn = _get_conn(tmp_path)
            assert conn is not None
            assert isinstance(conn, sqlite3.Connection)

    def test_get_conn_caches(self, tmp_path: Path) -> None:
        _create_engram_db(tmp_path)
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            conn1 = _get_conn(tmp_path)
            conn2 = _get_conn(tmp_path)
            assert conn1 is conn2

    def test_get_conn_no_db_returns_none(self, tmp_path: Path) -> None:
        assert _get_conn(tmp_path) is None

    def test_get_conn_exception_returns_none(self, tmp_path: Path) -> None:
        _create_engram_db(tmp_path)
        with patch("sqlite3.connect", side_effect=Exception("boom")):
            assert _get_conn(tmp_path) is None

    def test_project_for_no_config_uses_dirname(self, tmp_path: Path) -> None:
        assert _project_for(tmp_path) == tmp_path.name

    def test_project_for_invalid_config_json(self, tmp_path: Path) -> None:
        mnemo_dir = tmp_path / ".mnemo"
        mnemo_dir.mkdir(parents=True, exist_ok=True)
        (mnemo_dir / "config.json").write_text("not json", encoding="utf-8")
        assert _project_for(tmp_path) == tmp_path.name

    def test_project_for_empty_name(self, tmp_path: Path) -> None:
        mnemo_dir = tmp_path / ".mnemo"
        mnemo_dir.mkdir(parents=True, exist_ok=True)
        (mnemo_dir / "config.json").write_text(
            json.dumps({"project": {"name": ""}}), encoding="utf-8"
        )
        assert _project_for(tmp_path) == tmp_path.name

    def test_project_for_non_string_name(self, tmp_path: Path) -> None:
        mnemo_dir = tmp_path / ".mnemo"
        mnemo_dir.mkdir(parents=True, exist_ok=True)
        (mnemo_dir / "config.json").write_text(
            json.dumps({"project": {"name": 123}}), encoding="utf-8"
        )
        assert _project_for(tmp_path) == tmp_path.name

    def test_find_relevant_files_with_data(self, tmp_path: Path) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        _seed_patterns(
            conn,
            "test-project",
            [
                ("function", "def foo()", "src/a.py", 10, 3),
                ("function", "def bar()", "src/b.py", 20, 1),
            ],
        )
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            results = client.find_relevant_files("foo")
        assert len(results) >= 1
        assert any("a.py" in r.file_path for r in results)

    def test_find_relevant_files_empty(self, tmp_path: Path) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            results = client.find_relevant_files("nonexistent")
        assert results == []

    def test_find_relevant_files_for_finding(self, tmp_path: Path, make_finding) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        _seed_patterns(
            conn,
            "test-project",
            [
                ("function", "def foo()", "src/lib/foo.py", 10, 5),
                ("function", "def foo()", "src/lib/bar.py", 5, 2),
                ("class", "class Foo", "src/lib/foo.py", 1, 1),
            ],
        )
        conn.close()
        mnemo_client._conn_cache.clear()

        finding = _sqlite_finding(
            make_finding, path="src/lib/foo.py", rule="ruff-c408", snippet="dict()"
        )
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            results = client.find_relevant_files_for_finding(finding)
        assert len(results) >= 1
        assert results[0].file_path == "src/lib/foo.py"

    def test_find_relevant_files_for_finding_empty(self, tmp_path: Path, make_finding) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            results = client.find_relevant_files_for_finding(
                _sqlite_finding(make_finding)
            )
        assert results == []

    def test_search_patterns_with_data(self, tmp_path: Path) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        _seed_patterns(
            conn,
            "test-project",
            [
                ("function", "def my_func()", "src/a.py", 10, 5),
                ("class", "class MyClass", "src/b.py", 1, 3),
            ],
        )
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            results = client.search_patterns("my_func")
        assert len(results) >= 1
        assert results[0].pattern_type == "function"

    def test_search_patterns_with_type_filter(self, tmp_path: Path) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        _seed_patterns(
            conn,
            "test-project",
            [
                ("function", "def my_func()", "src/a.py", 10, 5),
                ("class", "class MyFunc", "src/b.py", 1, 3),
            ],
        )
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            results = client.search_patterns("MyFunc", pattern_types=["class"])
        assert len(results) == 1
        assert results[0].pattern_type == "class"

    def test_search_patterns_no_results(self, tmp_path: Path) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            assert client.search_patterns("nothing") == []

    def test_get_callers_with_data(self, tmp_path: Path) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        _seed_calls(
            conn,
            "test-project",
            [
                ("src/a.py", "caller_a", "src/b.py", "target_func", 10, 0, 0.9),
                ("src/c.py", "caller_c", "src/d.py", "target_func", 20, 1, 0.8),
            ],
        )
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            results = client.get_callers("target_func")
        assert len(results) == 2
        assert results[0].caller_symbol == "caller_a"

    def test_get_callers_empty(self, tmp_path: Path) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            assert client.get_callers("nonexistent") == []

    def test_get_callees_with_data(self, tmp_path: Path) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        _seed_calls(
            conn,
            "test-project",
            [
                ("src/a.py", "source_func", "src/b.py", "callee_b", 10, 0, 0.9),
            ],
        )
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            results = client.get_callees("source_func")
        assert len(results) == 1
        assert results[0].callee_symbol == "callee_b"

    def test_get_callees_empty(self, tmp_path: Path) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            assert client.get_callees("nonexistent") == []

    def test_get_dependencies_with_data(self, tmp_path: Path) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        _seed_deps(
            conn,
            "test-project",
            [
                ("src/a.py", "os", "path"),
                ("src/a.py", "json", None),
            ],
        )
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            results = client.get_dependencies("src/a.py")
        assert len(results) == 2

    def test_get_dependencies_empty(self, tmp_path: Path) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            assert client.get_dependencies("nonexistent.py") == []

    def test_get_symbols_for_file_with_data(self, tmp_path: Path) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        _seed_patterns(
            conn,
            "test-project",
            [
                ("function", "def my_func()", "src/a.py", 10, 1),
                ("class", "class MyClass", "src/a.py", 1, 1),
                ("decorator", "@property", "src/a.py", 5, 1),
            ],
        )
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            results = client.get_symbols_for_file("src/a.py")
        assert len(results) == 3

    def test_get_symbols_for_file_empty(self, tmp_path: Path) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            assert client.get_symbols_for_file("nonexistent.py") == []

    def test_get_context_for_finding_full(self, tmp_path: Path, make_finding) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        _seed_patterns(
            conn,
            "test-project",
            [
                ("function", "dict()", "src/lib/foo.py", 42, 5),
                ("function", "dict literal", "src/lib/foo.py", 30, 2),
            ],
        )
        _seed_calls(
            conn,
            "test-project",
            [
                ("src/other.py", "caller_fn", "src/lib/foo.py", "dict", 5, 0, 0.9),
            ],
        )
        _seed_deps(
            conn,
            "test-project",
            [
                ("src/lib/foo.py", "os", "path"),
            ],
        )
        conn.close()
        mnemo_client._conn_cache.clear()

        finding = _sqlite_finding(
            make_finding, path="src/lib/foo.py", rule="ruff-c408", snippet="dict()"
        )
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            ctx = client.get_context_for_finding(finding)
        assert ctx is not None
        assert "Relevant files" in ctx or "Patterns" in ctx or "Dependencies" in ctx

    def test_get_context_for_finding_no_data(self, tmp_path: Path, make_finding) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        conn.close()
        mnemo_client._conn_cache.clear()

        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            ctx = client.get_context_for_finding(_sqlite_finding(make_finding))
        assert ctx is None

    def test_recall_fast_path(self, tmp_path: Path, make_finding) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        _seed_patterns(
            conn,
            "test-project",
            [
                ("function", "dict()", "src/lib/foo.py", 42, 5),
            ],
        )
        _seed_deps(
            conn,
            "test-project",
            [
                ("src/lib/foo.py", "os", "path"),
            ],
        )
        conn.close()
        mnemo_client._conn_cache.clear()

        finding = _sqlite_finding(
            make_finding, path="src/lib/foo.py", rule="ruff-c408", snippet="dict()"
        )
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            result = client.recall(finding)
        assert result is not None

    def test_recall_cli_fallback(self, tmp_path: Path, make_finding) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        conn.close()
        mnemo_client._conn_cache.clear()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "cli output"

        finding = _sqlite_finding(make_finding)
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            with patch("subprocess.run", return_value=mock_result) as mock_run:
                result = client.recall(finding)
        assert result == "cli output"
        mock_run.assert_called()

    def test_recall_cli_fallback_failure(self, tmp_path: Path, make_finding) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        conn.close()
        mnemo_client._conn_cache.clear()

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        finding = _sqlite_finding(make_finding)
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            with patch("subprocess.run", return_value=mock_result):
                result = client.recall(finding)
        assert result is None

    def test_recall_cli_fallback_exception(self, tmp_path: Path, make_finding) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        conn.close()
        mnemo_client._conn_cache.clear()

        finding = _sqlite_finding(make_finding)
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            with patch("subprocess.run", side_effect=Exception("boom")):
                result = client.recall(finding)
        assert result is None

    def test_seed_success(self, tmp_path: Path, make_finding) -> None:
        _create_engram_db(tmp_path)
        mnemo_client._conn_cache.clear()

        mock_result = MagicMock()
        mock_result.returncode = 0

        finding = _sqlite_finding(make_finding)
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            with patch("subprocess.run", return_value=mock_result):
                client = MnemoClient(tmp_path)
                ok = client.seed(finding, "SUCCESS", None, None)
        assert ok is True

    def test_seed_with_error_and_changes(self, tmp_path: Path, make_finding) -> None:
        _create_engram_db(tmp_path)
        mnemo_client._conn_cache.clear()

        mock_result = MagicMock()
        mock_result.returncode = 0

        finding = _sqlite_finding(make_finding)
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            with patch("subprocess.run", return_value=mock_result) as mock_run:
                client = MnemoClient(tmp_path)
                ok = client.seed(finding, "FAILED", "some error", "some changes")
        assert ok is True
        all_content = " ".join(
            " ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list
        )
        assert "some error" in all_content
        assert "some changes" in all_content

    def test_seed_subprocess_failure(self, tmp_path: Path, make_finding) -> None:
        _create_engram_db(tmp_path)
        mnemo_client._conn_cache.clear()

        mock_result = MagicMock()
        mock_result.returncode = 1

        finding = _sqlite_finding(make_finding)
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            with patch("subprocess.run", return_value=mock_result):
                client = MnemoClient(tmp_path)
                ok = client.seed(finding, "FAILED", "err", None)
        assert ok is False

    def test_seed_subprocess_exception(self, tmp_path: Path, make_finding) -> None:
        _create_engram_db(tmp_path)
        mnemo_client._conn_cache.clear()

        finding = _sqlite_finding(make_finding)
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            with patch("subprocess.run", side_effect=Exception("boom")):
                client = MnemoClient(tmp_path)
                ok = client.seed(finding, "FAILED", None, None)
        assert ok is False

    def test_find_relevant_files_for_finding_skips_empty_terms(
        self, tmp_path: Path, make_finding
    ) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        _seed_patterns(
            conn,
            "test-project",
            [
                ("function", "def init()", "src/init.py", 1, 1),
            ],
        )
        conn.close()
        mnemo_client._conn_cache.clear()

        finding = _sqlite_finding(make_finding, path="a.py", rule="", snippet="")
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            results = client.find_relevant_files_for_finding(finding)
        assert isinstance(results, list)

    def test_get_context_for_finding_with_callers_and_callees(
        self, tmp_path: Path, make_finding
    ) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        _seed_patterns(
            conn,
            "test-project",
            [
                ("function", "my_func()", "src/lib/foo.py", 10, 5),
            ],
        )
        _seed_calls(
            conn,
            "test-project",
            [
                ("src/other.py", "caller_fn", "src/lib/foo.py", "my_func", 5, 0, 0.9),
                ("src/lib/foo.py", "my_func", "src/util.py", "helper_fn", 12, 1, 0.8),
            ],
        )
        _seed_deps(
            conn,
            "test-project",
            [
                ("src/lib/foo.py", "os", "path"),
            ],
        )
        conn.close()
        mnemo_client._conn_cache.clear()

        finding = _sqlite_finding(
            make_finding, path="src/lib/foo.py", rule="ruff-c408", snippet="dict()"
        )
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            ctx = client.get_context_for_finding(finding)
        assert ctx is not None
        assert "Call graph" in ctx
        assert "helper_fn" in ctx

    def test_get_context_for_finding_same_file_patterns(self, tmp_path: Path, make_finding) -> None:
        conn = _create_engram_db(tmp_path, "test-project")
        _seed_patterns(
            conn,
            "test-project",
            [
                ("function", "ruff-c408 violation dict()", "src/lib/foo.py", 42, 5),
                (
                    "function",
                    "ruff-c408 violation dict() call",
                    "src/lib/foo.py",
                    80,
                    2,
                ),
            ],
        )
        conn.close()
        mnemo_client._conn_cache.clear()

        finding = _sqlite_finding(
            make_finding, path="src/lib/foo.py", rule="ruff-c408", snippet="dict()"
        )
        with patch.dict(os.environ, {"MNEMO_ENABLED": "1"}):
            client = MnemoClient(tmp_path)
            ctx = client.get_context_for_finding(finding)
        assert ctx is not None
        assert "Other ruff-c408" in ctx

    def test_find_relevant_files_for_finding_disabled(self, tmp_path: Path, make_finding) -> None:
        with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
            mnemo_client._conn_cache.clear()
            mnemo_client._mnemo_available_cache.clear()
            client = MnemoClient(tmp_path)
            result = client.find_relevant_files_for_finding(
                _sqlite_finding(make_finding)
            )
        assert result == []


class TestDataclassFormatsSQLite:
    """Additional dataclass format tests for SQLite-backed models."""

    def test_call_graph_entry_format_callers(self) -> None:
        entry = CallGraphEntry(
            caller_file="a.py",
            caller_symbol="foo",
            callee_file="b.py",
            callee_symbol="bar",
            line_number=10,
            is_async=False,
        )
        result = entry.format_callers()
        assert "foo" in result
        assert "a.py:10" in result

    def test_call_graph_entry_format_callees_with_file(self) -> None:
        entry = CallGraphEntry(
            caller_file="a.py",
            caller_symbol="foo",
            callee_file="b.py",
            callee_symbol="bar",
            line_number=10,
            is_async=False,
        )
        result = entry.format_callees()
        assert "bar" in result
        assert "b.py" in result

    def test_call_graph_entry_format_callees_no_file(self) -> None:
        entry = CallGraphEntry(
            caller_file="a.py",
            caller_symbol="foo",
            callee_file=None,
            callee_symbol="bar",
            line_number=10,
            is_async=False,
        )
        result = entry.format_callees()
        assert "bar" in result
        assert "b.py" not in result

    def test_file_relevance_format_many_types(self) -> None:
        types = [f"type{i}" for i in range(10)]
        fr = FileRelevance("a.py", 5, types)
        output = fr.format()
        assert "a.py" in output


class TestExtractSearchTermsSQLite:
    """Additional search term extraction tests."""

    def test_extracts_class_names(self, tmp_path: Path, make_finding) -> None:
        with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
            client = MnemoClient(tmp_path)
            f = _sqlite_finding(make_finding, snippet='MyClass.some_method("arg")')
            terms = client._extract_search_terms(f)
        assert any("MyClass" in t for t in terms)

    def test_extracts_function_calls(self, tmp_path: Path, make_finding) -> None:
        with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
            client = MnemoClient(tmp_path)
            f = _sqlite_finding(make_finding, snippet="result = process_data(value)")
            terms = client._extract_search_terms(f)
        assert any("process_data" in t for t in terms)

    def test_rule_keywords(self, tmp_path: Path, make_finding) -> None:
        with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
            client = MnemoClient(tmp_path)
            f = _sqlite_finding(
                make_finding, rule="ruff-s311", snippet="random.randint()"
            )
            terms = client._extract_search_terms(f)
        assert "random generator" in terms
        assert "secrets module" in terms

    def test_deduplication(self, tmp_path: Path, make_finding) -> None:
        with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
            client = MnemoClient(tmp_path)
            f = _sqlite_finding(make_finding, rule="test", snippet="Test test test")
            terms = client._extract_search_terms(f)
        lower_seen = set()
        for t in terms:
            assert t.lower() not in lower_seen
            lower_seen.add(t.lower())

    def test_max_five_terms(self, tmp_path: Path, make_finding) -> None:
        with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
            client = MnemoClient(tmp_path)
            f = _sqlite_finding(
                make_finding,
                rule="rule1",
                snippet="Foo Bar Baz Qux Quux Corge Grault",
            )
            terms = client._extract_search_terms(f)
        assert len(terms) <= 5
