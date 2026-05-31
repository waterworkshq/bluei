import json
import os

import pytest

from bluei.engine.shared_pattern_library import SharedPatternLibrary
from bluei.engine.pattern_store import FixPatternStore, FixPattern


def _valid_pattern_dict(**overrides):
    d = {
        "pattern_id": "fp-abc123",
        "rule": "test-rule",
        "language": "python",
        "file_path": "test.py",
        "before_snippet": "x = 1 + 2",
        "after_snippet": "x = 1 - 2",
        "diff_patch": "--- a\n+++ b",
        "confidence": 0.9,
        "success_count": 5,
        "failure_count": 0,
        "skip_count": 0,
        "source": "autofix",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_used_at": None,
        "last_verified_at": None,
        "source_finding_ids": [],
        "framework_constraint": None,
        "file_pattern": "**/*",
        "structural_hash": "abcd1234efgh5678",
    }
    d.update(overrides)
    return d


def _valid_shared_dict(**overrides):
    d = {
        "pattern_id": "xp-test-rule-abcd1234",
        "rule": "test-rule",
        "language": "python",
        "structural_hash": "abcd1234efgh5678",
        "before_snippet": "x = 1 + 2",
        "after_snippet": "x = 1 - 2",
        "confidence": 0.9,
        "source_repos": ["repo-a"],
        "success_count": 5,
        "failure_count": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_seen_at": "2026-01-01T00:00:00+00:00",
    }
    d.update(overrides)
    return d


def _write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestEmptyJSONL:
    def test_fix_pattern_store_empty_file(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        store_file.write_text("", encoding="utf-8")

        store = FixPatternStore(store_file)

        assert store.load_active() == []
        assert len(store._patterns) == 0

    def test_shared_pattern_library_empty_file(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        lang_file.write_text("", encoding="utf-8")

        stats = lib.load_stats()
        assert stats["total_patterns"] == 0

    def test_fix_pattern_store_whitespace_only_file(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        store_file.write_text("   \n\n  \n", encoding="utf-8")

        store = FixPatternStore(store_file)

        assert store.load_active() == []


class TestMixedValidMalformed:
    def test_fix_pattern_store_one_valid_one_malformed(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        valid = json.dumps(_valid_pattern_dict())
        _write_lines(store_file, [valid, "not json at all {{{"])

        store = FixPatternStore(store_file)

        active = store.load_active()
        assert len(active) == 1
        assert active[0].pattern_id == "fp-abc123"

    def test_shared_library_one_valid_one_malformed(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        valid = json.dumps(_valid_shared_dict())
        _write_lines(lang_file, [valid, "}}}broken json{{{"])

        stats = lib.load_stats()
        assert stats["total_patterns"] == 1

    def test_fix_pattern_store_valid_in_middle_of_garbage(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        valid = json.dumps(_valid_pattern_dict(pattern_id="fp-mid"))
        _write_lines(store_file, ["garbage line 1", valid, "garbage line 2"])

        store = FixPatternStore(store_file)

        active = store.load_active()
        assert len(active) == 1
        assert active[0].pattern_id == "fp-mid"


class TestOnlyMalformedLines:
    def test_fix_pattern_store_all_malformed(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        _write_lines(store_file, ["not json", "{broken", "}}}", "abc def", ""])

        store = FixPatternStore(store_file)

        assert store.load_active() == []

    def test_shared_library_all_malformed(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        _write_lines(lang_file, ["bad line 1", "bad line 2", "bad line 3"])

        stats = lib.load_stats()
        assert stats["total_patterns"] == 0


class TestMissingRequiredFields:
    def test_fix_pattern_store_missing_pattern_id(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        d = _valid_pattern_dict()
        del d["pattern_id"]
        _write_lines(store_file, [json.dumps(d)])

        with pytest.raises(KeyError):
            FixPatternStore(store_file)

    def test_fix_pattern_store_missing_rule(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        d = _valid_pattern_dict()
        del d["rule"]
        _write_lines(store_file, [json.dumps(d)])

        with pytest.raises(KeyError):
            FixPatternStore(store_file)

    def test_fix_pattern_store_missing_before_snippet(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        d = _valid_pattern_dict()
        del d["before_snippet"]
        _write_lines(store_file, [json.dumps(d)])

        with pytest.raises(KeyError):
            FixPatternStore(store_file)

    def test_shared_library_missing_pattern_id_skipped_by_load(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        d = _valid_shared_dict()
        del d["pattern_id"]
        _write_lines(lang_file, [json.dumps(d)])

        with lib._lock:
            records = lib._load_language("python")
        assert len(records) == 0

    def test_shared_library_missing_rule_skipped_by_load(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        d = _valid_shared_dict()
        del d["rule"]
        _write_lines(lang_file, [json.dumps(d)])

        with lib._lock:
            records = lib._load_language("python")
        assert len(records) == 0

    def test_shared_library_missing_structural_hash_skipped_by_load(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        d = _valid_shared_dict()
        del d["structural_hash"]
        _write_lines(lang_file, [json.dumps(d)])

        with lib._lock:
            records = lib._load_language("python")
        assert len(records) == 0


class TestWrongTypes:
    def test_fix_pattern_store_confidence_as_string(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        d = _valid_pattern_dict(confidence="0.9")
        _write_lines(store_file, [json.dumps(d)])

        store = FixPatternStore(store_file)
        active = store.load_active()
        assert len(active) == 1
        assert isinstance(active[0].confidence, float)

    def test_fix_pattern_store_success_count_as_string(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        d = _valid_pattern_dict(success_count="10")
        _write_lines(store_file, [json.dumps(d)])

        store = FixPatternStore(store_file)
        active = store.load_active()
        assert len(active) == 1
        assert isinstance(active[0].success_count, int)

    def test_shared_library_confidence_as_string(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        d = _valid_shared_dict(confidence="0.9")
        _write_lines(lang_file, [json.dumps(d)])

        stats = lib.load_stats()
        assert stats["total_patterns"] == 1

    def test_fix_pattern_store_confidence_unparseable_string(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        d = _valid_pattern_dict(confidence="not-a-number")
        _write_lines(store_file, [json.dumps(d)])

        with pytest.raises((ValueError, TypeError)):
            FixPatternStore(store_file)


class TestBinaryGarbageData:
    def test_fix_pattern_store_binary_data(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        store_file.write_bytes(b"\x00\x01\x02\xff\xfe\xfd")

        with pytest.raises(UnicodeDecodeError):
            FixPatternStore(store_file)

    def test_shared_library_binary_data(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        lang_file.write_bytes(b"\x80\x81\x82\x83\x84\x85")

        with pytest.raises(UnicodeDecodeError):
            lib.load_stats()

        with pytest.raises(UnicodeDecodeError):
            with lib._lock:
                lib._load_language("python")

    def test_fix_pattern_store_mixed_binary_and_valid(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        content = b'\xc3\x28' + b"\n" + json.dumps(_valid_pattern_dict()).encode("utf-8") + b"\n"
        store_file.write_bytes(content)

        with pytest.raises(UnicodeDecodeError):
            FixPatternStore(store_file)


class TestVeryLongLines:
    def test_fix_pattern_store_long_line_no_crash(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        long_line = "x" * 1_000_000
        valid = json.dumps(_valid_pattern_dict(pattern_id="fp-long"))
        _write_lines(store_file, [long_line, valid])

        store = FixPatternStore(store_file)

        active = store.load_active()
        assert len(active) == 1
        assert active[0].pattern_id == "fp-long"

    def test_shared_library_long_line_no_crash(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        long_line = "y" * 1_000_000
        valid = json.dumps(_valid_shared_dict())
        _write_lines(lang_file, [valid, long_line])

        with lib._lock:
            records = lib._load_language("python")
        assert len(records) == 1


class TestTruncatedJSON:
    def test_fix_pattern_store_truncated_json_at_end(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        valid = json.dumps(_valid_pattern_dict())
        truncated = valid[: len(valid) // 2]
        _write_lines(store_file, [valid, truncated])

        store = FixPatternStore(store_file)

        active = store.load_active()
        assert len(active) == 1

    def test_shared_library_truncated_json_at_end(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        valid = json.dumps(_valid_shared_dict())
        truncated = valid[: len(valid) // 2]
        _write_lines(lang_file, [valid, truncated])

        stats = lib.load_stats()
        assert stats["total_patterns"] == 1

    def test_fix_pattern_store_truncated_json_only(self, tmp_path):
        store_file = tmp_path / "patterns.jsonl"
        full = json.dumps(_valid_pattern_dict())
        store_file.write_text(full[: len(full) // 2], encoding="utf-8")

        store = FixPatternStore(store_file)

        assert store.load_active() == []

    def test_shared_library_truncated_json_only(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        full = json.dumps(_valid_shared_dict())
        lang_file.write_text(full[: len(full) // 2], encoding="utf-8")

        stats = lib.load_stats()
        assert stats["total_patterns"] == 0
