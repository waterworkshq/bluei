#!/usr/bin/env python3
"""Tests for Phase H: deterministic chunking/compression metadata helpers.

Run with: python -m pytest tests/test_review_chunking.py -v
"""

import json
import shutil
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _isolated_tmp() -> Path:
    """Return a unique isolated temp directory, removing any pre-existing one."""
    base = Path(f"/tmp/qa_chunking_{uuid.uuid4().hex[:8]}")
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    return base


from qa_agent.models import (
    Repo,
    RepoConfig,
    ReviewMode,
    CompressionMode,
    FindingSource,
    FindingActionability,
    FindingSeverity,
    generate_id,
)
from qa_agent.review import (
    ReviewCycleEngine,
    GitHubReviewProvider,
    order_files_for_chunking,
    build_chunk_manifest,
    ChunkManifest,
)
from qa_agent.state import StateManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_repo(tmp_path: Path) -> Repo:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = RepoConfig(
        id="repo-test",
        name="test-repo",
        path=str(repo_path),
        language="typescript",
        review_care={
            "enabled": True,
            "mode": ReviewMode.AUTONOMOUS_REVIEW.value,
            "max_attempts": 3,
        },
    )
    return Repo(config=config)


def make_engine(repo: Repo, state: StateManager) -> ReviewCycleEngine:
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = state
    engine.provider = MagicMock(spec=GitHubReviewProvider)
    return engine


STUB_CANDIDATES = [
    {
        "repo": "test-repo",
        "path": "src/main.ts",
        "line": 10,
        "header": "outstanding-todo",
        "snippet": "# TODO: refactor this function",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.MEDIUM.value,
        "severity": FindingSeverity.LOW.value,
        "confidence": 0.7,
        "safe_to_autofix": False,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
    {
        "repo": "test-repo",
        "path": "src/utils.ts",
        "line": 42,
        "header": "excessively-long-line",
        "snippet": "x = function_call(arg1, arg2)",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.LOW.value,
        "severity": FindingSeverity.LOW.value,
        "confidence": 0.6,
        "safe_to_autofix": True,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
]


# ---------------------------------------------------------------------------
# Test: CompressionMode enum values
# ---------------------------------------------------------------------------

class TestCompressionModeEnum:
    def test_compression_mode_has_expected_values(self):
        assert CompressionMode.FULL_DIFF.value == "full_diff"
        assert CompressionMode.COMPRESSED.value == "compressed"
        assert CompressionMode.MULTI_PASS.value == "multi_pass"

    def test_compression_mode_is_string_enum(self):
        assert isinstance(CompressionMode.FULL_DIFF, str)
        assert CompressionMode.FULL_DIFF == "full_diff"


# ---------------------------------------------------------------------------
# Test: order_files_for_chunking — deterministic file ordering
# ---------------------------------------------------------------------------

class TestOrderFilesForChunking:
    def test_empty_list_returns_empty(self):
        assert order_files_for_chunking([]) == []

    def test_single_file_returns_same(self):
        files = ["src/main.ts"]
        result = order_files_for_chunking(files)
        assert result == ["src/main.ts"]

    def test_ordering_is_deterministic_same_input(self):
        files = ["src/a.ts", "src/b.ts", "src/c.ts"]
        result1 = order_files_for_chunking(files)
        result2 = order_files_for_chunking(files)
        assert result1 == result2

    def test_ordering_is_deterministic_repeated_calls(self):
        """Multiple calls with same files produce identical ordering."""
        files = ["src/main.ts", "lib/util.ts", "src/utils.ts"]
        for _ in range(5):
            assert order_files_for_chunking(files) == order_files_for_chunking(files)

    def test_ordering_respects_language_priority(self):
        """Files matching the repo's primary language sort first."""
        files = ["lib/utils.py", "src/main.ts", "src/handler.ts"]
        result = order_files_for_chunking(files, language="typescript")
        # TypeScript files should come before Python
        assert result[0] in ("src/main.ts", "src/handler.ts")
        assert result[-1] == "lib/utils.py"

    def test_ordering_uses_path_as_stable_tiebreaker(self):
        """Files with same priority/size are ordered by path ascending."""
        files = ["z.py", "a.py", "m.py"]
        result = order_files_for_chunking(files, language="python")
        # All same language, same size proxy (0 since files don't exist)
        # So order should be path asc: a, m, z
        assert result == sorted(files)

    def test_ordering_uses_size_proxy_when_files_exist(self, tmp_path: Path):
        """Existing files with real content are ordered by size desc."""
        # Create files of different sizes
        small = tmp_path / "small.py"
        large = tmp_path / "large.py"
        medium = tmp_path / "medium.py"
        small.write_text("x = 1\n")
        large.write_text("x = " + ", ".join([f"arg{i}" for i in range(50)]) + "\n")
        medium.write_text("x = " + ", ".join([f"arg{i}" for i in range(25)]) + "\n")

        files = [str(small), str(medium), str(large)]
        result = order_files_for_chunking(files, language="python")

        # large should be first (largest), small last (smallest)
        assert result[0] == str(large)
        assert result[-1] == str(small)

    def test_ordering_does_not_mutate_input(self):
        files = ["src/b.ts", "src/a.ts"]
        original = list(files)
        order_files_for_chunking(files)
        assert files == original

    def test_nonexistent_files_use_zero_size_proxy(self):
        """Non-existent paths fall back to 0 size, sorted by path asc."""
        files = ["zz.py", "aa.py", "mm.py"]
        result = order_files_for_chunking(files, language="python")
        # All non-existent, same size proxy (0), path asc tiebreak
        assert result == ["aa.py", "mm.py", "zz.py"]

    def test_mixed_existing_and_nonexistent(self, tmp_path: Path):
        """Files that exist are sized; non-existent get 0 and sort by path."""
        existing = tmp_path / "big.py"
        existing.write_text("x = " + ", ".join([f"v{i}" for i in range(100)]) + "\n")
        files = ["zz.py", str(existing), "aa.py"]
        result = order_files_for_chunking(files, language="python")
        # Existing file (big) should be first; among non-existent, path asc
        assert result[0] == str(existing)
        # Remaining two should be path asc
        assert result[1] == "aa.py"
        assert result[2] == "zz.py"


# ---------------------------------------------------------------------------
# Test: ChunkManifest structure and serialization
# ---------------------------------------------------------------------------

class TestChunkManifest:
    def test_default_manifest_single_chunk(self):
        manifest = ChunkManifest()
        assert manifest.mode == "full_diff"
        assert manifest.token_budget == 0
        assert manifest.total_files == 0
        assert manifest.total_chunks == 1
        assert manifest.chunks == []
        assert manifest.ordering == []

    def test_manifest_to_dict(self):
        manifest = ChunkManifest(
            mode="multi_pass",
            token_budget=100000,
            total_files=5,
            total_chunks=2,
            chunks=[["a.ts", "b.ts"], ["c.ts", "d.ts", "e.ts"]],
            ordering=["a.ts", "b.ts", "c.ts", "d.ts", "e.ts"],
        )
        d = manifest.to_dict()
        assert d["mode"] == "multi_pass"
        assert d["token_budget"] == 100000
        assert d["total_files"] == 5
        assert d["total_chunks"] == 2
        assert len(d["chunks"]) == 2
        assert d["chunks"][0] == ["a.ts", "b.ts"]

    def test_manifest_from_dict(self):
        data = {
            "mode": "compressed",
            "token_budget": 50000,
            "total_files": 3,
            "total_chunks": 1,
            "chunks": [["x.ts", "y.ts", "z.ts"]],
            "ordering": ["x.ts", "y.ts", "z.ts"],
        }
        manifest = ChunkManifest.from_dict(data)
        assert manifest.mode == "compressed"
        assert manifest.token_budget == 50000
        assert manifest.total_files == 3
        assert manifest.total_chunks == 1
        assert manifest.chunks[0] == ["x.ts", "y.ts", "z.ts"]

    def test_manifest_from_dict_unknown_fields_ignored(self):
        data = {
            "mode": "full_diff",
            "token_budget": 0,
            "total_files": 0,
            "total_chunks": 1,
            "chunks": [],
            "ordering": [],
            "unknown_field": "drop-me",
        }
        manifest = ChunkManifest.from_dict(data)
        assert not hasattr(manifest, "unknown_field")

    def test_manifest_from_dict_missing_fields_get_defaults(self):
        data = {"mode": "multi_pass"}
        manifest = ChunkManifest.from_dict(data)
        assert manifest.token_budget == 0
        assert manifest.total_files == 0
        assert manifest.total_chunks == 1


# ---------------------------------------------------------------------------
# Test: build_chunk_manifest helper
# ---------------------------------------------------------------------------

class TestBuildChunkManifest:
    def test_empty_files_returns_empty_chunks(self):
        manifest = build_chunk_manifest([])
        assert manifest.total_files == 0
        assert manifest.total_chunks == 0
        assert manifest.chunks == []
        assert manifest.ordering == []

    def test_single_file_single_chunk(self):
        manifest = build_chunk_manifest(["src/main.ts"])
        assert manifest.total_files == 1
        assert manifest.total_chunks == 1
        assert manifest.chunks == [["src/main.ts"]]
        assert manifest.ordering == ["src/main.ts"]

    def test_full_diff_mode_single_chunk(self):
        files = ["a.ts", "b.ts", "c.ts"]
        manifest = build_chunk_manifest(files, mode="full_diff")
        assert manifest.mode == "full_diff"
        assert manifest.total_files == 3
        assert manifest.total_chunks == 1
        assert manifest.chunks == [manifest.ordering]

    def test_compressed_mode_still_single_chunk(self):
        """Until real tokenization is wired, compressed mode uses single chunk."""
        files = ["a.ts", "b.ts"]
        manifest = build_chunk_manifest(files, mode="compressed")
        assert manifest.mode == "compressed"
        assert manifest.total_chunks == 1
        assert manifest.chunks[0] == manifest.ordering

    def test_multi_pass_mode_still_single_chunk(self):
        """Until real chunking is implemented, multi_pass uses single chunk."""
        files = ["a.ts", "b.ts", "c.ts"]
        manifest = build_chunk_manifest(files, mode="multi_pass")
        assert manifest.mode == "multi_pass"
        assert manifest.total_chunks == 1

    def test_token_budget_stored(self):
        manifest = build_chunk_manifest(["a.ts"], token_budget=50000)
        assert manifest.token_budget == 50000

    def test_ordering_is_deterministic_across_modes(self):
        files = ["z.ts", "a.ts", "m.ts"]
        modes = ["full_diff", "compressed", "multi_pass"]
        orderings = []
        for mode in modes:
            m = build_chunk_manifest(files, mode=mode)
            orderings.append(m.ordering)
        # All modes should produce the same deterministic ordering
        assert orderings[0] == orderings[1] == orderings[2]
        # Ordering is path-asc for non-existent files of same language
        assert orderings[0] == ["a.ts", "m.ts", "z.ts"]

    def test_ordering_with_language_priority(self, tmp_path: Path):
        """Python files sort below TypeScript when repo language is TypeScript."""
        py_file = tmp_path / "utils.py"
        py_file.write_text("x = 1\n")
        ts_file = tmp_path / "main.ts"
        ts_file.write_text("const x = 1;\n")

        manifest = build_chunk_manifest(
            [str(py_file), str(ts_file)],
            language="typescript",
        )
        # TypeScript file should come first
        assert manifest.ordering[0] == str(ts_file)
        assert manifest.ordering[-1] == str(py_file)


# ---------------------------------------------------------------------------
# Test: compression_mode persisted in ReviewRun artifact
# ---------------------------------------------------------------------------

class TestCompressionModeInReviewRun:
    def test_review_run_contains_compression_mode_field(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        assert len(runs) == 1
        run = runs[0]
        assert "compression_mode" in run
        assert run["compression_mode"] == CompressionMode.FULL_DIFF.value

    def test_review_run_contains_token_budget_field(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        assert len(runs) == 1
        run = runs[0]
        assert "token_budget" in run
        assert isinstance(run["token_budget"], int)

    def test_review_run_persists_compression_mode_and_token_budget(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        run_id = state.list_review_runs(repo.config.name)[0]["run_id"]
        loaded = state.load_review_run(repo.config.name, run_id)
        assert loaded["compression_mode"] == "full_diff"
        assert loaded["token_budget"] == 0

    def test_dry_run_does_not_persist(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=True)

        runs = state.list_review_runs(repo.config.name)
        assert len(runs) == 0


# ---------------------------------------------------------------------------
# Test: chunk manifest shape / stability via build_chunk_manifest
# ---------------------------------------------------------------------------

class TestChunkManifestStability:
    def test_manifest_stable_across_calls(self):
        files = ["src/z.ts", "src/a.ts", "src/m.ts"]
        m1 = build_chunk_manifest(files)
        m2 = build_chunk_manifest(files)
        assert m1.to_dict() == m2.to_dict()

    def test_manifest_structure_preserved_in_run_artifact(self):
        """Build a manifest and embed it in a run artifact dict."""
        manifest = build_chunk_manifest(["a.ts", "b.ts"], mode="full_diff")
        run_data = {
            "id": "run-1",
            "compression_mode": manifest.mode,
            "token_budget": manifest.token_budget,
            "chunk_manifest": manifest.to_dict(),
        }
        assert run_data["chunk_manifest"]["total_files"] == 2
        assert run_data["chunk_manifest"]["total_chunks"] == 1
        assert run_data["chunk_manifest"]["ordering"] == ["a.ts", "b.ts"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
