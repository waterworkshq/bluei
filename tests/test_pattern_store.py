import json
import threading
from pathlib import Path

import pytest

from bluei.engine import pattern_store as pattern_store_module
from bluei.engine.pattern_store import (
    DEACTIVATION_THRESHOLD,
    FixPattern,
    FixPatternStore,
    snippet_hash,
)


def make_pattern(**overrides):
    data = {
        "pattern_id": "",
        "rule": "broad-except",
        "language": "python",
        "file_path": "src/*.py",
        "before_snippet": "except:\n    pass",
        "after_snippet": "except Exception:\n    pass",
        "diff_patch": "--- a/src/file.py\n+++ b/src/file.py\n-except:\n+except Exception:\n",
        "confidence": 0.99,
        "success_count": 0,
        "failure_count": 0,
        "skip_count": 0,
        "source": "claude",
        "created_at": "2026-05-17T00:00:00+00:00",
        "last_used_at": None,
        "last_verified_at": None,
        "source_finding_ids": ["finding-1"],
    }
    data.update(overrides)
    return FixPattern(**data)


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "repos" / "repo-a" / "state" / "fix_patterns.jsonl"


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_append_new_pattern_writes_jsonl_with_correct_fields(store_path):
    store = FixPatternStore(store_path)

    pattern_id = store.append(make_pattern(confidence=0.8))

    records = read_jsonl(store_path)
    assert len(records) == 1
    record = records[0]
    assert pattern_id.startswith("fp-")
    assert len(pattern_id) == 15
    assert record["pattern_id"] == pattern_id
    assert record["confidence"] == 0.8
    assert record["success_count"] == 1
    assert record["rule"] == "broad-except"
    assert record["before_snippet"] == "except:\n pass"
    assert set(record) == {
        "pattern_id",
        "rule",
        "language",
        "file_path",
        "before_snippet",
        "after_snippet",
        "diff_patch",
        "confidence",
        "success_count",
        "failure_count",
        "skip_count",
        "source",
        "created_at",
        "last_used_at",
        "last_verified_at",
        "last_failed_at",
        "source_finding_ids",
        "framework_constraint",
        "file_pattern",
        "structural_hash",
        "excluded_paths",
        "imports_touched",
        "validation_commands_passed",
        "rule_family",
    }


def test_append_rebuilds_index_file(store_path):
    store = FixPatternStore(store_path)
    pattern = make_pattern()

    pattern_id = store.append(pattern)

    index_data = json.loads(store.index_path.read_text())
    before_hash = snippet_hash(pattern.before_snippet)
    assert index_data["version"] == 1
    assert index_data["by_rule"]["broad-except"][before_hash] == pattern_id
    assert index_data["active_count"] == 1
    assert index_data["total_count"] == 1


def test_lookup_by_rule_and_before_snippet_returns_pattern(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    found = store.lookup("broad-except", "    except:\n        pass")

    assert found is not None
    assert found.pattern_id == pattern_id


def test_lookup_with_no_match_returns_none(store_path):
    store = FixPatternStore(store_path)
    store.append(make_pattern())

    assert store.lookup("other-rule", "except:\n    pass") is None
    assert store.lookup("broad-except", "raise RuntimeError()") is None


def test_lookup_returns_none_for_deactivated_pattern(store_path):
    store = FixPatternStore(store_path)
    store.append(make_pattern())
    store.update_confidence(store.load_active()[0].pattern_id, -1.0)

    assert store.lookup("broad-except", "except:\n    pass") is None
    assert store.load_active() == []


def test_update_confidence_positive_delta(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern(confidence=0.5))

    store.update_confidence(pattern_id, 0.2)

    assert store.lookup("broad-except", "except:\n    pass").confidence == 0.7


def test_update_confidence_negative_delta_floors_at_zero(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.update_confidence(pattern_id, -9.0)

    records = read_jsonl(store_path)
    assert records[0]["confidence"] == 0.0
    assert store.load_active() == []


def test_record_replay_success_increments_success_count_and_timestamps(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.record_replay(pattern_id, success=True)

    record = read_jsonl(store_path)[0]
    assert record["success_count"] == 2
    assert record["last_verified_at"] is not None
    assert record["last_used_at"] is None
    assert record["last_failed_at"] is None


def test_record_replay_failure_increments_skip_count(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.record_replay(pattern_id, success=False)

    record = read_jsonl(store_path)[0]
    assert record["success_count"] == 1
    assert record["skip_count"] == 1
    assert record["last_used_at"] is not None
    assert record["last_verified_at"] is None


def test_append_deduplicates_same_rule_and_before_snippet(store_path):
    store = FixPatternStore(store_path)
    first_id = store.append(make_pattern())
    second_id = store.append(make_pattern(before_snippet="    except:\n        pass"))

    records = read_jsonl(store_path)
    assert second_id == first_id
    assert len(records) == 1
    assert records[0]["success_count"] == 2


def test_append_dedup_preserves_all_finding_ids(store_path):
    """Deduplicated appends must accumulate source_finding_ids."""
    store = FixPatternStore(store_path)
    first_id = store.append(make_pattern(source_finding_ids=["finding-1"]))
    second_id = store.append(
        make_pattern(
            before_snippet="    except:\n        pass",
            source_finding_ids=["finding-2", "finding-3"],
        )
    )

    assert second_id == first_id

    records = read_jsonl(store_path)
    assert records[0]["source_finding_ids"] == ["finding-1", "finding-2", "finding-3"]

    # Also verify lookup_by_finding works for all accumulated IDs
    assert store.lookup_by_finding("finding-1") is not None
    assert store.lookup_by_finding("finding-2") is not None
    assert store.lookup_by_finding("finding-3") is not None
    assert store.lookup_by_finding("finding-unknown") is None


def test_append_dedup_does_not_duplicate_existing_finding_ids(store_path):
    """Dedup path should not add a finding_id that is already present."""
    store = FixPatternStore(store_path)
    first_id = store.append(make_pattern(source_finding_ids=["finding-1"]))
    second_id = store.append(
        make_pattern(
            before_snippet="    except:\n        pass",
            source_finding_ids=["finding-1"],
        )
    )

    records = read_jsonl(store_path)
    assert records[0]["source_finding_ids"] == ["finding-1"]


def test_rotation_creates_bak_file(monkeypatch, store_path):
    monkeypatch.setattr(pattern_store_module, "PATTERN_STORE_MAX_BYTES", 100)
    store = FixPatternStore(store_path)

    store.append(make_pattern(diff_patch="x" * 200))

    assert store_path.with_suffix(".jsonl.bak").exists()


def test_rotation_preserves_active_patterns(monkeypatch, store_path):
    """Rotation must carry over active patterns; deactivated ones are pruned."""
    monkeypatch.setattr(pattern_store_module, "PATTERN_STORE_MAX_BYTES", 100)
    store = FixPatternStore(store_path)

    active_id = store.append(
        make_pattern(
            rule="active-rule", before_snippet="active snippet", confidence=0.9
        )
    )
    deactivated_id = store.append(
        make_pattern(
            rule="deactivated-rule",
            before_snippet="deactivated snippet",
            confidence=0.5,
        )
    )
    store.update_confidence(deactivated_id, -1.0)
    assert (
        store.load_active(deactivated_id) == []
        or store.get_pattern(deactivated_id).confidence < DEACTIVATION_THRESHOLD
    )

    # Trigger rotation by appending a large pattern
    store.append(
        make_pattern(
            rule="trigger-rule", before_snippet="trigger snippet", diff_patch="x" * 200
        )
    )

    assert store_path.with_suffix(".jsonl.bak").exists()

    # Active pattern must survive rotation
    found = store.lookup("active-rule", "active snippet")
    assert found is not None
    assert found.pattern_id == active_id

    # Deactivated pattern must be pruned (not in new file)
    records = read_jsonl(store_path)
    pattern_ids_in_file = {r["pattern_id"] for r in records}
    assert active_id in pattern_ids_in_file
    assert deactivated_id not in pattern_ids_in_file

    # Also verify via fresh load from disk
    fresh_store = FixPatternStore(store_path)
    assert fresh_store.lookup("active-rule", "active snippet") is not None
    assert fresh_store.get_pattern(deactivated_id) is None


def test_rotation_with_all_deactivated_prunes_them(monkeypatch, store_path):
    """Deactivated patterns are pruned on rotation; only active ones survive."""
    monkeypatch.setattr(pattern_store_module, "PATTERN_STORE_MAX_BYTES", 100)
    store = FixPatternStore(store_path)

    pid = store.append(
        make_pattern(rule="doomed", before_snippet="doomed", confidence=0.5)
    )
    store.update_confidence(pid, -1.0)

    # Trigger rotation — the newly appended pattern is active so the file
    # will exist, but the deactivated pattern must be gone from it.
    trigger_id = store.append(
        make_pattern(rule="trigger", before_snippet="trigger", diff_patch="x" * 200)
    )

    assert store_path.with_suffix(".jsonl.bak").exists()
    records = read_jsonl(store_path)
    pattern_ids_in_file = {r["pattern_id"] for r in records}
    assert trigger_id in pattern_ids_in_file
    assert pid not in pattern_ids_in_file


def test_active_pattern_cap_rejects_201st_append(store_path):
    store = FixPatternStore(store_path)

    for index in range(200):
        store.append(
            make_pattern(rule=f"rule-{index}", before_snippet=f"before {index}")
        )

    with pytest.raises(ValueError, match="active fix pattern cap reached"):
        store.append(make_pattern(rule="rule-201", before_snippet="before 201"))


def test_load_active_returns_only_patterns_at_or_above_threshold(store_path):
    store = FixPatternStore(store_path)
    active_id = store.append(make_pattern(rule="active", before_snippet="active"))
    inactive_id = store.append(
        make_pattern(rule="inactive", before_snippet="inactive", confidence=0.5)
    )
    store.update_confidence(inactive_id, -(0.5 - (DEACTIVATION_THRESHOLD - 0.01)))

    active = store.load_active()

    assert [pattern.pattern_id for pattern in active] == [active_id]


def test_load_active_filters_by_rule(store_path):
    store = FixPatternStore(store_path)
    first_id = store.append(make_pattern(rule="broad-except", before_snippet="one"))
    store.append(make_pattern(rule="trailing-whitespace", before_snippet="two"))

    active = store.load_active(rule="broad-except")

    assert [pattern.pattern_id for pattern in active] == [first_id]


def test_init_builds_index_from_existing_jsonl(store_path):
    original = FixPatternStore(store_path)
    pattern_id = original.append(make_pattern())

    loaded = FixPatternStore(store_path)

    assert loaded.lookup("broad-except", "except:\n    pass").pattern_id == pattern_id


def test_atomic_index_rebuild_remains_valid_after_concurrent_appends(store_path):
    store = FixPatternStore(store_path)

    def append_one(index):
        store.append(
            make_pattern(rule=f"rule-{index}", before_snippet=f"before {index}")
        )

    threads = [
        threading.Thread(target=append_one, args=(index,)) for index in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    index_data = json.loads(store.index_path.read_text())
    assert index_data["active_count"] == 20
    assert index_data["total_count"] == 20
    assert len(index_data["by_rule"]) == 20


# ── Merged from test_pattern_store_remaining.py ──

from unittest.mock import patch as _patch


def test_lookup_returns_none_when_pattern_id_missing_from_patterns_dict(store_path):
    store = FixPatternStore(store_path)
    h = snippet_hash("except:\n    pass")
    store._index["broad-except"] = {h: "ghost-id-not-in-dict"}
    assert store.lookup("broad-except", "except:\n    pass") is None


def test_lookup_returns_none_for_low_confidence_pattern_kept_in_index(store_path):
    store = FixPatternStore(store_path)
    pid = store.append(make_pattern())
    store._patterns[pid].confidence = 0.1
    store._index["broad-except"] = {snippet_hash("except:\n    pass"): pid}
    assert store.lookup("broad-except", "except:\n    pass") is None


def test_lookup_structural_returns_none_for_low_confidence_pattern(store_path):
    store = FixPatternStore(store_path)
    pid = store.append(make_pattern())
    store._patterns[pid].confidence = 0.1
    store._structural_index["broad-except"] = {"struct-abc": pid}
    with _patch(
        "bluei.engine.structural_hash.compute_structural_hash",
        return_value="struct-abc",
    ):
        result = store.lookup_structural("broad-except", "except:\n    pass", "python")
    assert result is None


def test_lookup_structural_returns_none_when_pattern_id_missing_from_dict(store_path):
    store = FixPatternStore(store_path)
    store._structural_index["broad-except"] = {"struct-xyz": "phantom-id"}
    with _patch(
        "bluei.engine.structural_hash.compute_structural_hash",
        return_value="struct-xyz",
    ):
        result = store.lookup_structural("broad-except", "except:\n    pass", "python")
    assert result is None


def test_update_confidence_returns_silently_for_unknown_pattern_id(store_path):
    store = FixPatternStore(store_path)
    store.update_confidence("nonexistent-fp-deadbeef", 0.5)
    assert not store_path.exists()


def test_record_replay_returns_silently_for_unknown_pattern_id(store_path):
    store = FixPatternStore(store_path)
    store.record_replay("nonexistent-fp-deadbeef", success=True)
    assert not store_path.exists()


def test_rotate_returns_early_when_store_file_does_not_exist(store_path):
    store = FixPatternStore(store_path)
    with store._locked():
        store._rotate_if_needed_unlocked()
    assert not store_path.exists()


def test_rotate_handles_oserror_while_counting_lines(store_path):
    store = FixPatternStore(store_path)
    store.append(make_pattern())
    original_open = Path.open

    def patched_open(self, *args, **kwargs):
        if self == store.store_path and args and args[0] == "r":
            raise OSError("mocked read failure")
        return original_open(self, *args, **kwargs)

    with _patch.object(Path, "open", patched_open):
        with store._locked():
            store._rotate_if_needed_unlocked()
    assert store.store_path.exists()


def test_rotate_handles_oserror_renaming_store_to_bak(store_path, monkeypatch):
    monkeypatch.setattr(pattern_store_module, "PATTERN_STORE_MAX_BYTES", 1)
    store = FixPatternStore(store_path)
    store.append(make_pattern())
    with _patch("os.replace", side_effect=OSError("mocked rename failure")):
        with store._locked():
            store._rotate_if_needed_unlocked()
    assert store.store_path.exists()


def test_carry_over_skips_blank_lines_and_malformed_json(store_path):
    store = FixPatternStore(store_path)
    bak_path = store_path.with_suffix(".jsonl.bak")
    bak_path.parent.mkdir(parents=True, exist_ok=True)
    valid_record = json.dumps(
        {
            "pattern_id": "fp-carry001",
            "rule": "test-rule",
            "language": "python",
            "file_path": "f.py",
            "before_snippet": "b",
            "after_snippet": "a",
            "diff_patch": "",
            "confidence": 0.9,
            "source_finding_ids": [],
        }
    )
    bak_path.write_text(
        "\n{not valid json\n\n" + valid_record + "\n",
        encoding="utf-8",
    )
    with store._locked():
        store._carry_over_active_patterns(bak_path)
    assert store_path.exists()
    lines = [l for l in store_path.read_text().strip().splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["pattern_id"] == "fp-carry001"


def test_carry_over_handles_oserror_reading_backup_file(store_path):
    store = FixPatternStore(store_path)
    bak_path = store_path.with_suffix(".jsonl.bak")
    bak_path.parent.mkdir(parents=True, exist_ok=True)
    bak_path.write_text('{"confidence": 0.9}', encoding="utf-8")
    original_open = Path.open

    def patched_open(self, *args, **kwargs):
        if self == bak_path:
            raise OSError("mocked bak read failure")
        return original_open(self, *args, **kwargs)

    with _patch.object(Path, "open", patched_open):
        with store._locked():
            store._carry_over_active_patterns(bak_path)
    assert not store_path.exists()
