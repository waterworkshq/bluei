import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from bluei.engine.composite_pattern import (
    CompositePattern,
    CompositePatternApplier,
    CompositePatternStep,
    CompositePatternStore,
)
from bluei.engine.cascade import CompositePatternCascadeStage


def _make_step(
    order=0,
    scope="finding_location",
    match="old",
    replacement="new",
    file_pattern=None,
    description="test step",
):
    return CompositePatternStep(
        order=order,
        description=description,
        match=match,
        replacement=replacement,
        scope=scope,
        file_pattern=file_pattern,
    )


def _make_pattern(rule="test-rule", steps=None, confidence=0.8, **kw):
    return CompositePattern(
        pattern_id=kw.get("pattern_id", "comp-test123"),
        rule=rule,
        language=kw.get("language", "python"),
        steps=steps or [_make_step()],
        confidence=confidence,
        source=kw.get("source", "test"),
    )


class TestCompositePatternStep:
    def test_to_dict_roundtrip(self):
        step = _make_step(order=1, scope="related_file", file_pattern="src/**/*.py")
        d = step.to_dict()
        restored = CompositePatternStep.from_dict(d)
        assert restored.order == 1
        assert restored.scope == "related_file"
        assert restored.file_pattern == "src/**/*.py"

    def test_to_dict_omits_none_file_pattern(self):
        step = _make_step(file_pattern=None)
        d = step.to_dict()
        assert "file_pattern" not in d

    def test_from_dict_missing_file_pattern(self):
        d = {
            "order": 0,
            "description": "x",
            "match": "a",
            "replacement": "b",
            "scope": "finding_location",
        }
        step = CompositePatternStep.from_dict(d)
        assert step.file_pattern is None

    def test_scope_values(self):
        for scope in ("finding_location", "file_header", "related_file"):
            step = _make_step(scope=scope)
            assert step.scope == scope

    def test_order_sorting(self):
        steps = [_make_step(order=2), _make_step(order=0), _make_step(order=1)]
        assert [s.order for s in sorted(steps, key=lambda s: s.order)] == [0, 1, 2]


class TestCompositePattern:
    def test_to_dict_has_type_composite(self):
        pat = _make_pattern()
        d = pat.to_dict()
        assert d["type"] == "composite"

    def test_roundtrip(self):
        pat = _make_pattern(
            steps=[_make_step(order=0, match="a", replacement="b")],
            confidence=0.75,
        )
        d = pat.to_dict()
        restored = CompositePattern.from_dict(d)
        assert restored.rule == "test-rule"
        assert len(restored.steps) == 1
        assert restored.steps[0].match == "a"
        assert restored.confidence == 0.75

    def test_default_values(self):
        pat = _make_pattern()
        assert pat.success_count == 0
        assert pat.failure_count == 0
        assert pat.source == "test"


class TestCompositePatternApplier:
    def test_single_step_finding_location(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("old code here")
        pat = _make_pattern(steps=[_make_step(match="old", replacement="new")])
        applier = CompositePatternApplier()
        ok, changes, err = applier.apply(pat, "src/main.py", worktree)
        assert ok is True
        assert err is None
        assert f.read_text() == "new code here"
        assert "src/main.py" in changes

    def test_multi_step_two_files(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        f1 = worktree / "src" / "a.py"
        f2 = worktree / "src" / "b.py"
        f1.parent.mkdir(parents=True, exist_ok=True)
        f1.write_text("import old_module")
        f2.parent.mkdir(parents=True, exist_ok=True)
        f2.write_text("from old_module import func")
        pat = _make_pattern(
            steps=[
                _make_step(
                    order=0,
                    scope="finding_location",
                    match="old_module",
                    replacement="new_module",
                ),
                _make_step(
                    order=1,
                    scope="related_file",
                    match="old_module",
                    replacement="new_module",
                    file_pattern="src/b.py",
                ),
            ]
        )
        applier = CompositePatternApplier()
        ok, changes, err = applier.apply(pat, "src/a.py", worktree)
        assert ok is True
        assert "new_module" in f1.read_text()
        assert "new_module" in f2.read_text()
        assert len(changes) == 2

    def test_file_header_scope(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("# Old License\nimport os\n")
        pat = _make_pattern(
            steps=[
                _make_step(
                    order=0,
                    scope="file_header",
                    match="Old License",
                    replacement="New License",
                ),
            ]
        )
        applier = CompositePatternApplier()
        ok, changes, err = applier.apply(pat, "src/main.py", worktree)
        assert ok is True
        content = f.read_text()
        assert "New License" in content

    def test_related_file_no_match_rolls_back(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("original content")
        pat = _make_pattern(
            steps=[
                _make_step(
                    order=0,
                    scope="finding_location",
                    match="original",
                    replacement="modified",
                ),
                _make_step(
                    order=1,
                    scope="related_file",
                    match="x",
                    replacement="y",
                    file_pattern="nonexistent/*.py",
                ),
            ]
        )
        applier = CompositePatternApplier()
        ok, changes, err = applier.apply(pat, "src/main.py", worktree)
        assert ok is False
        assert "no file matches" in (err or "")
        assert f.read_text() == "original content"

    def test_regex_error_rolls_back(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("original content")
        pat = _make_pattern(
            steps=[
                _make_step(order=0, match="[invalid", replacement="new"),
            ]
        )
        applier = CompositePatternApplier()
        ok, changes, err = applier.apply(pat, "src/main.py", worktree)
        assert ok is False
        assert "regex error" in (err or "")
        assert f.read_text() == "original content"

    def test_write_failure_rolls_back(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("original")
        pat = _make_pattern(
            steps=[_make_step(match="original", replacement="modified")]
        )
        applier = CompositePatternApplier()
        with patch.object(Path, "write_text", side_effect=PermissionError("denied")):
            ok, changes, err = applier.apply(pat, "src/main.py", worktree)
        assert ok is False

    def test_empty_file_header(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        f = worktree / "empty.py"
        f.write_text("")
        pat = _make_pattern(
            steps=[
                _make_step(
                    order=0, scope="file_header", match="^$", replacement="# header"
                ),
            ]
        )
        applier = CompositePatternApplier()
        ok, changes, err = applier.apply(pat, "empty.py", worktree)
        assert ok is True


class TestCompositePatternStore:
    def test_append_and_lookup(self, tmp_path):
        store = CompositePatternStore(tmp_path / "comp.jsonl")
        pat = _make_pattern(rule="ruff-b904")
        pid = store.append(pat)
        assert pid
        found = store.lookup("ruff-b904")
        assert found is not None
        assert found.pattern_id == pid

    def test_lookup_no_match(self, tmp_path):
        store = CompositePatternStore(tmp_path / "comp.jsonl")
        assert store.lookup("nonexistent") is None

    def test_lookup_low_confidence_skipped(self, tmp_path):
        store = CompositePatternStore(tmp_path / "comp.jsonl")
        pat = _make_pattern(rule="low-conf", confidence=0.1)
        store.append(pat)
        assert store.lookup("low-conf") is None

    def test_record_result_success(self, tmp_path):
        store = CompositePatternStore(tmp_path / "comp.jsonl")
        pat = _make_pattern(rule="tune-rule", confidence=0.5)
        pid = store.append(pat)
        store.record_result(pid, success=True)
        assert store.get(pid).confidence > 0.5
        assert store.get(pid).success_count == 1

    def test_record_result_failure(self, tmp_path):
        store = CompositePatternStore(tmp_path / "comp.jsonl")
        pat = _make_pattern(rule="fail-rule", confidence=0.8)
        pid = store.append(pat)
        store.record_result(pid, success=False)
        assert store.get(pid).confidence < 0.8
        assert store.get(pid).failure_count == 1

    def test_persistence(self, tmp_path):
        path = tmp_path / "comp.jsonl"
        store1 = CompositePatternStore(path)
        pat = _make_pattern(rule="persist-rule")
        pid = store1.append(pat)
        store2 = CompositePatternStore(path)
        found = store2.lookup("persist-rule")
        assert found is not None
        assert found.pattern_id == pid


class TestCompositePatternCascadeStage:
    def test_can_handle_no_pattern_store(self, make_finding):
        from bluei.engine.cascade import CascadeContext

        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=None)
        finding = make_finding(rule="test-rule", path="src/main.py", snippet="old code")
        assert stage.can_handle(finding, ctx) is False

    def test_attempt_no_store_path(self, make_finding):
        from bluei.engine.cascade import CascadeContext, CascadeResult
        from unittest.mock import MagicMock

        stage = CompositePatternCascadeStage()
        mock_store = MagicMock()
        del mock_store.store_path
        ctx = CascadeContext(pattern_store=mock_store)
        finding = make_finding(rule="test-rule", path="src/main.py", snippet="old code")
        result = stage.attempt(finding, Path("/tmp"), ctx)
        assert result.success is False

    def test_rollback_does_not_raise(self, make_finding, tmp_path):
        stage = CompositePatternCascadeStage()
        finding = make_finding(rule="test-rule", path="src/main.py", snippet="old code")
        stage.rollback(finding, tmp_path)


class TestExtractorCompositeIntegration:
    def test_multi_file_diff_extracts_composite(self, tmp_path):
        from bluei.engine.pattern_extractor import extract
        from bluei.engine.pattern_store import FixPatternStore
        from bluei.engine.models import Finding

        worktree = tmp_path / "repo"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        f1 = worktree / "src" / "a.py"
        f2 = worktree / "src" / "b.py"
        f1.parent.mkdir(parents=True, exist_ok=True)
        f2.parent.mkdir(parents=True, exist_ok=True)
        f1.write_text("import old_mod\npass\n")
        f2.write_text("from old_mod import func\npass\n")

        import subprocess

        subprocess.run(["git", "init"], cwd=str(worktree), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(worktree),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "test"],
            cwd=str(worktree),
            capture_output=True,
        )
        subprocess.run(["git", "add", "."], cwd=str(worktree), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=str(worktree), capture_output=True
        )

        f1.write_text("import new_mod\npass\n")
        f2.write_text("from new_mod import func\npass\n")

        finding = Finding(
            finding_id="f1",
            repo="test",
            path="src/a.py",
            line=1,
            rule="import-rename",
            snippet="import old_mod",
            confidence=0.9,
            quick_win=True,
            safe_to_autofix=True,
        )
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        log_file = tmp_path / "test.log"
        result = extract(worktree, finding, "autofix", store, log_file)
        assert result is not None
        assert result.startswith("comp-")

    def test_single_file_diff_still_regular_pattern(self, tmp_path):
        from bluei.engine.pattern_extractor import extract
        from bluei.engine.pattern_store import FixPatternStore
        from bluei.engine.models import Finding
        import subprocess

        worktree = tmp_path / "repo"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        f1 = worktree / "src" / "a.py"
        f1.parent.mkdir(parents=True)
        f1.write_text("except:\n    pass\n")

        subprocess.run(["git", "init"], cwd=str(worktree), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(worktree),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "test"],
            cwd=str(worktree),
            capture_output=True,
        )
        subprocess.run(["git", "add", "."], cwd=str(worktree), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=str(worktree), capture_output=True
        )

        f1.write_text("except Exception:\n    pass\n")

        finding = Finding(
            finding_id="f2",
            repo="test",
            path="src/a.py",
            line=1,
            rule="broad-except",
            snippet="except:",
            confidence=0.9,
            quick_win=True,
            safe_to_autofix=True,
        )
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        log_file = tmp_path / "test.log"
        result = extract(worktree, finding, "autofix", store, log_file)
        assert result is not None
        assert not result.startswith("comp-")

    def test_no_changes_diff_returns_none(self, tmp_path):
        from bluei.engine.pattern_extractor import extract
        from bluei.engine.pattern_store import FixPatternStore
        from bluei.engine.models import Finding
        import subprocess

        worktree = tmp_path / "repo"
        worktree.mkdir()
        f1 = worktree / "src" / "a.py"
        f1.parent.mkdir(parents=True)
        f1.write_text("unchanged\n")

        subprocess.run(["git", "init"], cwd=str(worktree), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(worktree),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "test"],
            cwd=str(worktree),
            capture_output=True,
        )
        subprocess.run(["git", "add", "."], cwd=str(worktree), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=str(worktree), capture_output=True
        )

        finding = Finding(
            finding_id="f3",
            repo="test",
            path="src/a.py",
            line=1,
            rule="noop",
            snippet="unchanged",
            confidence=0.9,
            quick_win=True,
            safe_to_autofix=True,
        )
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        log_file = tmp_path / "test.log"
        result = extract(worktree, finding, "autofix", store, log_file)
        assert result is None


class TestCompositeSharedLibrary:
    def test_shared_library_stores_composite(self, tmp_path):
        from bluei.engine.shared_pattern_library import SharedPatternLibrary
        from bluei.engine.composite_pattern import (
            CompositePattern,
            CompositePatternStep,
        )

        lib = SharedPatternLibrary(tmp_path / "shared")
        pat = CompositePattern(
            pattern_id="comp-shared1",
            rule="ruff-b904",
            language="python",
            steps=[
                _make_step(match="raise Error", replacement="raise Error from cause")
            ],
            confidence=0.85,
        )
        lib.publish_composite(
            pattern=pat,
            repo_id="repo-a",
            language="python",
        )
        results = lib.lookup_composite("ruff-b904", "python")
        assert results is not None
        assert len(results) >= 1


# ── Merged from test_composite_pattern_remaining.py ──

from unittest.mock import MagicMock


class TestSnapshotReadFailure:
    def test_read_failure_on_snapshot_rolls_back(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("original")
        pat = _make_pattern(
            steps=[_make_step(match="original", replacement="modified")]
        )
        applier = CompositePatternApplier()
        call_count = [0]
        original_read = Path.read_text

        def flaky_read(self, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise PermissionError("nope")
            return original_read(self, *a, **kw)

        with patch.object(Path, "read_text", flaky_read):
            ok, changes, err = applier.apply(pat, "src/main.py", worktree)
        assert ok is False
        assert "cannot read" in (err or "")
        assert f.read_text() == "original"


class TestResolveTargetEdgeCases:
    def test_related_file_scope_no_file_pattern(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        (worktree / "src").mkdir()
        (worktree / "src" / "a.py").write_text("content")
        step = _make_step(scope="related_file", file_pattern=None)
        applier = CompositePatternApplier()
        result = applier._resolve_target(step, "src/a.py", worktree)
        assert result is None

    def test_related_file_scope_path_traversal_dotdot(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        step = _make_step(scope="related_file", file_pattern="../etc/passwd")
        applier = CompositePatternApplier()
        result = applier._resolve_target(step, "src/a.py", worktree)
        assert result is None

    def test_related_file_scope_absolute_path(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        step = _make_step(scope="related_file", file_pattern="/etc/passwd")
        applier = CompositePatternApplier()
        result = applier._resolve_target(step, "src/a.py", worktree)
        assert result is None

    def test_related_file_symlink_outside_worktree(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("secret")
        link = worktree / "evil.py"
        link.symlink_to(outside / "secret.py")
        step = _make_step(scope="related_file", file_pattern="*.py")
        applier = CompositePatternApplier()
        result = applier._resolve_target(step, "src/a.py", worktree)
        assert result is None

    def test_unknown_scope_returns_none(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        step = _make_step(scope="mystery_scope")
        applier = CompositePatternApplier()
        result = applier._resolve_target(step, "src/a.py", worktree)
        assert result is None


class TestApplyStepEdgeCases:
    def test_pattern_exceeds_max_length(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("content")
        long_match = "x" * 2001
        pat = _make_pattern(steps=[_make_step(match=long_match, replacement="new")])
        applier = CompositePatternApplier()
        ok, changes, err = applier.apply(pat, "src/main.py", worktree)
        assert ok is False
        assert "regex error" in (err or "")
        assert "pattern too long" in (err or "")

    def test_file_header_empty_lines_fallthrough(self):
        applier = CompositePatternApplier()
        step = _make_step(scope="file_header", match="old", replacement="new")
        mock_content = MagicMock()
        mock_content.split.return_value = []
        with patch("bluei.engine.composite_pattern.re.sub", return_value="replaced"):
            result = applier._apply_step(step, mock_content, "test.py")
        assert result == "replaced"


class TestRollbackGitCheckoutFails:
    def test_rollback_handles_git_checkout_exception(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        f = worktree / "test.txt"
        f.write_text("modified")
        applier = CompositePatternApplier()
        snapshots = {"test.txt": "original"}
        with patch(
            "bluei.engine.composite_pattern.run_capture",
            side_effect=OSError("git broke"),
        ):
            applier._rollback(snapshots, worktree)
        assert f.read_text() == "original"


class TestStoreRebuildEdgeCases:
    def test_record_result_unknown_pattern_id(self, tmp_path):
        store = CompositePatternStore(tmp_path / "comp.jsonl")
        pat = _make_pattern(rule="real-rule")
        store.append(pat)
        store.record_result("nonexistent-id", success=True)
        assert store.get(pat.pattern_id) is not None
        assert store.get(pat.pattern_id).success_count == 0

    def test_rebuild_skips_blank_lines(self, tmp_path):
        path = tmp_path / "comp.jsonl"
        pat = _make_pattern(rule="blank-test")
        record = pat.to_dict()
        record["type"] = "composite"
        lines = ["\n", json.dumps(record) + "\n", "   \n", "\n"]
        path.write_text("".join(lines))
        store = CompositePatternStore(path)
        assert store.lookup("blank-test") is not None

    def test_rebuild_skips_invalid_json(self, tmp_path):
        path = tmp_path / "comp.jsonl"
        pat = _make_pattern(rule="valid-rule")
        record = pat.to_dict()
        record["type"] = "composite"
        lines = ["not json at all\n", json.dumps(record) + "\n", "{broken json\n"]
        path.write_text("".join(lines))
        store = CompositePatternStore(path)
        assert store.lookup("valid-rule") is not None

    def test_rebuild_skips_non_composite_type(self, tmp_path):
        path = tmp_path / "comp.jsonl"
        pat = _make_pattern(rule="good-rule")
        good_record = pat.to_dict()
        good_record["type"] = "composite"
        bad_record = {
            "type": "single",
            "rule": "bad-rule",
            "pattern_id": "s1",
            "language": "python",
            "match": "a",
            "replacement": "b",
        }
        lines = [json.dumps(bad_record) + "\n", json.dumps(good_record) + "\n"]
        path.write_text("".join(lines))
        store = CompositePatternStore(path)
        assert store.lookup("good-rule") is not None
