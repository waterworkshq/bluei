#!/usr/bin/env python3
"""test_mnemo_client.py — Tests for mnemo_client module (Python-native engram)."""

import json
import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure workspace root is on sys.path
_workdir = os.environ.get("WORKSPACE_ROOT")
if _workdir:
    sys.path.insert(0, _workdir)
else:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.sandbox_local_runner.models import Finding

# Mock subprocess before importing mnemo_client
with patch("subprocess.run") as mock_run:
    with patch("subprocess.Popen"):
        from core.sandbox_local_runner import mnemo_client

        mnemo_client._mnemo_available_cache.clear()  # Reset cache

from core.sandbox_local_runner.mnemo_client import (
    MnemoClient,
    is_mnemo_available,
    MNEMO_MAX_CHARS,
    MNEMO_RECALL_LIMIT,
)


def _make_finding():
    return Finding(
        finding_id="fid-test-001",
        repo="test-repo",
        path="src/test.py",
        line=10,
        rule="RUF100",
        snippet="x = 1  # unused assignment",
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
    )


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


def test_get_context_for_finding_unavailable():
    """get_context_for_finding returns None when unavailable."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        f = _make_finding()
        result = client.get_context_for_finding(f)
    assert result is None


def test_recall_unavailable():
    """recall() returns None when mnemo is unavailable."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        f = _make_finding()
        result = client.recall(f)
    assert result is None


def test_seed_unavailable():
    """seed() returns False when mnemo is unavailable."""
    with patch.dict(os.environ, {"MNEMO_ENABLED": "0"}):
        mnemo_client._mnemo_available_cache.clear()
        mnemo_client._conn_cache.clear()
        client = MnemoClient(Path("/fake"))
        f = _make_finding()
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
    from core.sandbox_local_runner.mnemo_client import PatternMatch

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
    from core.sandbox_local_runner.mnemo_client import FileRelevance

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
    from core.sandbox_local_runner.mnemo_client import Dependency

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
