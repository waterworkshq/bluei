"""Focused tests for alpha.1 Slice 1 — ReplayOutcome enum + envelope fields.

Covers:
- ReplayOutcome.HIT / MISS / FAILURE storage mapping (ADR-0004)
- record_replay(success=bool) backward-compat delegation
- FixPattern.last_failed_at + excluded_paths additive fields
- from_dict additive loading (old records without the new fields)
- excluded_paths enforcement in lookup / lookup_structural / lookup_fuzzy
- add_excluded_path / remove_excluded_path round-trip
"""

import json

import pytest

from bluei.engine.pattern_store import (
    FixPattern,
    FixPatternStore,
    ReplayOutcome,
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


def _reload(store_path):
    return FixPatternStore(store_path)


# ── ReplayOutcome enum ───────────────────────────────────────────────────────


def test_replay_outcome_enum_has_three_members():
    assert {o.name for o in ReplayOutcome} == {"HIT", "MISS", "FAILURE"}


def test_replay_outcome_values_are_distinct_strings():
    values = {o.value for o in ReplayOutcome}
    assert len(values) == 3


# ── ReplayOutcome storage mapping ───────────────────────────────────────────


def test_record_replay_outcome_hit_increments_success_and_stamps_verified(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.record_replay_outcome(pattern_id, ReplayOutcome.HIT)

    record = read_jsonl(store_path)[0]
    assert record["success_count"] == 2
    assert record["last_verified_at"] is not None
    assert record["last_used_at"] is None
    assert record["last_failed_at"] is None


def test_record_replay_outcome_miss_increments_skip_and_stamps_used(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.record_replay_outcome(pattern_id, ReplayOutcome.MISS)

    record = read_jsonl(store_path)[0]
    assert record["skip_count"] == 1
    assert record["last_used_at"] is not None
    assert record["last_verified_at"] is None
    assert record["last_failed_at"] is None


def test_record_replay_outcome_failure_increments_failure_and_stamps_failed(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.record_replay_outcome(pattern_id, ReplayOutcome.FAILURE)

    record = read_jsonl(store_path)[0]
    assert record["failure_count"] == 1
    assert record["last_failed_at"] is not None
    assert record["last_verified_at"] is None
    assert record["last_used_at"] is None


def test_record_replay_outcome_three_outcomes_in_sequence_independent(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.record_replay_outcome(pattern_id, ReplayOutcome.HIT)
    store.record_replay_outcome(pattern_id, ReplayOutcome.MISS)
    store.record_replay_outcome(pattern_id, ReplayOutcome.FAILURE)

    record = read_jsonl(store_path)[0]
    assert record["success_count"] == 2
    assert record["skip_count"] == 1
    assert record["failure_count"] == 1
    assert record["last_verified_at"] is not None
    assert record["last_used_at"] is not None
    assert record["last_failed_at"] is not None


def test_record_replay_outcome_silently_noops_for_unknown_pattern_id(store_path):
    store = FixPatternStore(store_path)
    store.record_replay_outcome("nonexistent-fp-deadbeef", ReplayOutcome.HIT)
    assert not store_path.exists()


# ── record_replay(bool) backward-compat delegation ──────────────────────────


def test_record_replay_bool_true_delegates_to_hit(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.record_replay(pattern_id, success=True)

    record = read_jsonl(store_path)[0]
    assert record["success_count"] == 2
    assert record["last_verified_at"] is not None
    assert record["last_used_at"] is None


def test_record_replay_bool_false_delegates_to_miss(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.record_replay(pattern_id, success=False)

    record = read_jsonl(store_path)[0]
    assert record["skip_count"] == 1
    assert record["last_used_at"] is not None
    assert record["last_verified_at"] is None


def test_record_replay_positional_true_works_without_keyword(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.record_replay(pattern_id, True)

    record = read_jsonl(store_path)[0]
    assert record["success_count"] == 2


def test_record_replay_uses_keyword_callers_compatible_with_pattern_replay(store_path):
    """The pattern_replay.py:289/296 callers use ``success=`` keyword. This
    slice's backward-compat shim must keep that contract working."""
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.record_replay(pattern_id, success=False)
    store.record_replay(pattern_id, success=True)

    record = read_jsonl(store_path)[0]
    assert record["skip_count"] == 1
    assert record["success_count"] == 2


# ── FixPattern envelope fields ───────────────────────────────────────────────


def test_fixpattern_defaults_for_new_fields():
    p = FixPattern(
        pattern_id="fp-x",
        rule="r",
        language="py",
        file_path="f",
        before_snippet="b",
        after_snippet="a",
        diff_patch="",
    )
    assert p.last_failed_at is None
    assert p.excluded_paths == []


def test_fixpattern_to_dict_includes_new_fields():
    p = make_pattern(pattern_id="fp-test")
    d = p.to_dict()
    assert "last_failed_at" in d
    assert "excluded_paths" in d
    assert d["last_failed_at"] is None
    assert d["excluded_paths"] == []


def test_fixpattern_from_dict_preserves_new_fields():
    data = {
        "pattern_id": "fp-test",
        "rule": "r",
        "language": "py",
        "file_path": "f",
        "before_snippet": "b",
        "after_snippet": "a",
        "diff_patch": "",
        "last_failed_at": "2026-06-26T00:00:00+00:00",
        "excluded_paths": ["tests/**", "vendor/**"],
    }
    p = FixPattern.from_dict(data)
    assert p.last_failed_at == "2026-06-26T00:00:00+00:00"
    assert p.excluded_paths == ["tests/**", "vendor/**"]


def test_fixpattern_from_dict_loads_old_records_without_new_fields():
    """Release constraint: old ``fix_patterns.jsonl`` records load unchanged."""
    data = {
        "pattern_id": "fp-legacy",
        "rule": "legacy-rule",
        "language": "python",
        "file_path": "src/old.py",
        "before_snippet": "x",
        "after_snippet": "y",
        "diff_patch": "",
        "confidence": 0.9,
        "success_count": 3,
        "failure_count": 1,
        "skip_count": 2,
        "source": "autofix",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_used_at": "2026-05-01T00:00:00+00:00",
        "last_verified_at": "2026-04-15T00:00:00+00:00",
        "source_finding_ids": ["f1"],
        "framework_constraint": None,
        "file_pattern": "**/*",
        "structural_hash": None,
    }
    p = FixPattern.from_dict(data)
    assert p.pattern_id == "fp-legacy"
    assert p.last_failed_at is None
    assert p.excluded_paths == []


def test_persisted_record_roundtrips_through_from_dict(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.add_excluded_path(pattern_id, "tests/**")
    store.record_replay_outcome(pattern_id, ReplayOutcome.FAILURE)

    raw = read_jsonl(store_path)[0]
    restored = FixPattern.from_dict(raw)
    assert restored.last_failed_at is not None
    assert restored.excluded_paths == ["tests/**"]


def test_rebuild_from_disk_preserves_excluded_paths(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.add_excluded_path(pattern_id, "tests/**")
    store.add_excluded_path(pattern_id, "vendor/**")

    fresh = _reload(store_path)
    p = fresh.get_pattern(pattern_id)
    assert p is not None
    assert set(p.excluded_paths) == {"tests/**", "vendor/**"}


# ── excluded_paths lookup enforcement ───────────────────────────────────────


def test_lookup_returns_none_when_target_matches_excluded_paths(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())
    store.add_excluded_path(pattern_id, "tests/**")

    assert (
        store.lookup("broad-except", "except:\n    pass", target_path="tests/foo.py")
        is None
    )


def test_lookup_returns_pattern_when_target_does_not_match_excluded(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())
    store.add_excluded_path(pattern_id, "tests/**")

    found = store.lookup("broad-except", "except:\n    pass", target_path="src/foo.py")
    assert found is not None
    assert found.pattern_id == pattern_id


def test_lookup_without_target_path_does_not_enforce_excluded_paths(store_path):
    """When target_path is None (legacy callers), excluded_paths must not
    filter candidates — otherwise T0.3 must wire it everywhere atomically."""
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())
    store.add_excluded_path(pattern_id, "tests/**")

    found = store.lookup("broad-except", "except:\n    pass")
    assert found is not None
    assert found.pattern_id == pattern_id


def test_lookup_with_no_excluded_paths_returns_pattern(store_path):
    store = FixPatternStore(store_path)
    store.append(make_pattern())

    assert (
        store.lookup("broad-except", "except:\n    pass", target_path="tests/foo.py")
        is not None
    )


def test_lookup_structural_returns_none_when_target_matches_excluded(store_path):
    from unittest.mock import patch as _patch

    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())
    store.add_excluded_path(pattern_id, "tests/**")

    with _patch(
        "bluei.engine.structural_hash.compute_structural_hash",
        return_value="struct-marker",
    ):
        store._structural_index["broad-except"]["struct-marker"] = pattern_id
        result = store.lookup_structural(
            "broad-except", "except:\n    pass", "python", target_path="tests/x.py"
        )
    assert result is None


def test_lookup_structural_returns_pattern_when_target_not_excluded(store_path):
    from unittest.mock import patch as _patch

    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())
    store.add_excluded_path(pattern_id, "tests/**")

    with _patch(
        "bluei.engine.structural_hash.compute_structural_hash",
        return_value="struct-marker",
    ):
        store._structural_index["broad-except"]["struct-marker"] = pattern_id
        result = store.lookup_structural(
            "broad-except", "except:\n    pass", "python", target_path="src/x.py"
        )
    assert result is not None


def test_lookup_fuzzy_skips_candidates_matching_excluded_paths(store_path):
    from unittest.mock import patch as _patch

    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())
    store.add_excluded_path(pattern_id, "tests/**")

    with _patch(
        "bluei.engine.structural_hash.fuzzy_structural_match", return_value=0.99
    ):
        result = store.lookup_fuzzy(
            "broad-except",
            "except:\n    pass",
            "python",
            threshold=0.75,
            target_path="tests/foo.py",
        )
    assert result is None


def test_lookup_fuzzy_returns_pattern_when_target_not_excluded(store_path):
    from unittest.mock import patch as _patch

    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())
    store.add_excluded_path(pattern_id, "tests/**")

    with _patch(
        "bluei.engine.structural_hash.fuzzy_structural_match", return_value=0.99
    ):
        result = store.lookup_fuzzy(
            "broad-except",
            "except:\n    pass",
            "python",
            threshold=0.75,
            target_path="src/foo.py",
        )
    assert result is not None
    assert result.pattern_id == pattern_id


# ── add_excluded_path / remove_excluded_path ────────────────────────────────


def test_add_excluded_path_persists_and_rebuilds_index(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    changed = store.add_excluded_path(pattern_id, "tests/**")

    assert changed is True
    p = store.get_pattern(pattern_id)
    assert p is not None
    assert p.excluded_paths == ["tests/**"]
    raw = read_jsonl(store_path)[0]
    assert raw["excluded_paths"] == ["tests/**"]


def test_add_excluded_path_noop_when_already_present(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())
    store.add_excluded_path(pattern_id, "tests/**")

    changed = store.add_excluded_path(pattern_id, "tests/**")

    assert changed is False
    p = store.get_pattern(pattern_id)
    assert p is not None
    assert p.excluded_paths == ["tests/**"]


def test_add_excluded_path_returns_false_for_unknown_pattern(store_path):
    store = FixPatternStore(store_path)
    assert store.add_excluded_path("nonexistent-fp-deadbeef", "tests/**") is False


def test_remove_excluded_path_persists(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())
    store.add_excluded_path(pattern_id, "tests/**")
    store.add_excluded_path(pattern_id, "vendor/**")

    changed = store.remove_excluded_path(pattern_id, "tests/**")

    assert changed is True
    p = store.get_pattern(pattern_id)
    assert p is not None
    assert p.excluded_paths == ["vendor/**"]
    raw = read_jsonl(store_path)[0]
    assert raw["excluded_paths"] == ["vendor/**"]


def test_remove_excluded_path_noop_when_absent(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())
    store.add_excluded_path(pattern_id, "tests/**")

    changed = store.remove_excluded_path(pattern_id, "vendor/**")

    assert changed is False
    p = store.get_pattern(pattern_id)
    assert p is not None
    assert p.excluded_paths == ["tests/**"]


def test_remove_excluded_path_returns_false_for_unknown_pattern(store_path):
    store = FixPatternStore(store_path)
    assert store.remove_excluded_path("nonexistent-fp-deadbeef", "tests/**") is False


def test_add_remove_roundtrip_with_reload(store_path):
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())

    store.add_excluded_path(pattern_id, "tests/**")
    store.add_excluded_path(pattern_id, "vendor/**")
    store.remove_excluded_path(pattern_id, "tests/**")

    fresh = _reload(store_path)
    p = fresh.get_pattern(pattern_id)
    assert p is not None
    assert p.excluded_paths == ["vendor/**"]


def test_excluded_paths_globstar_matches_nested_paths(store_path):
    """``**`` in excluded_paths matches across path segments (parity with file_pattern)."""
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())
    store.add_excluded_path(pattern_id, "vendor/**")
    # ** matches a nested path
    assert (
        store.lookup(
            "broad-except", "except:\n    pass", target_path="vendor/pkg/mod.py"
        )
        is None
    )
    # ** does not exclude an unrelated top-level dir
    assert (
        store.lookup("broad-except", "except:\n    pass", target_path="src/app.py")
        is not None
    )


def test_excluded_paths_bare_globstar_matches_anything(store_path):
    """A bare ``**`` excludes every path."""
    store = FixPatternStore(store_path)
    pattern_id = store.append(make_pattern())
    store.add_excluded_path(pattern_id, "**")
    assert (
        store.lookup(
            "broad-except", "except:\n    pass", target_path="any/deep/path.py"
        )
        is None
    )
