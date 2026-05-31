import threading

import pytest

from bluei.engine.shared_pattern_library import SharedPatternLibrary
from bluei.engine.pattern_store import FixPattern, FixPatternStore


def _make_pattern(**overrides):
    data = {
        "pattern_id": "",
        "rule": "broad-except",
        "language": "python",
        "file_path": "src/*.py",
        "before_snippet": "except:\n    pass",
        "after_snippet": "except Exception:\n    pass",
        "diff_patch": "--- a\n+++ b\n-except:\n+except Exception:\n",
        "confidence": 0.9,
        "success_count": 0,
        "failure_count": 0,
        "skip_count": 0,
        "source": "autofix",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_used_at": None,
        "last_verified_at": None,
        "source_finding_ids": [],
    }
    data.update(overrides)
    return FixPattern(**data)


class TestConcurrentPublishSharedPatternLibrary:
    def test_all_publishes_succeed_no_data_loss(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        num_threads = 20
        barrier = threading.Barrier(num_threads)
        results = [None] * num_threads
        errors = []

        def publish_fn(idx):
            try:
                barrier.wait(timeout=5)
                pid = lib.publish(
                    rule=f"rule-{idx}",
                    language="python",
                    before_snippet=f"x = {idx} + 1",
                    after_snippet=f"x = {idx} - 1",
                    confidence=0.9,
                    repo_id=f"repo-{idx}",
                )
                results[idx] = pid
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=publish_fn, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        non_empty = [r for r in results if r]
        assert len(non_empty) == num_threads

        stats = lib.load_stats()
        assert stats["total_patterns"] == num_threads

    def test_same_rule_concurrent_publish_merges_repos(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        num_threads = 10
        barrier = threading.Barrier(num_threads)
        errors = []

        def publish_fn(idx):
            try:
                barrier.wait(timeout=5)
                lib.publish(
                    rule="shared-rule",
                    language="python",
                    before_snippet="result = a + b",
                    after_snippet="result = a - b",
                    confidence=0.9,
                    repo_id=f"repo-{idx}",
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=publish_fn, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        stats = lib.load_stats()
        assert stats["total_patterns"] == 1


class TestConcurrentAppendFixPatternStore:
    def test_all_appends_succeed_no_corruption(self, tmp_path):
        store = FixPatternStore(tmp_path / "store.jsonl")
        num_threads = 20
        barrier = threading.Barrier(num_threads)
        results = [None] * num_threads
        errors = []

        def append_fn(idx):
            try:
                barrier.wait(timeout=5)
                pid = store.append(
                    _make_pattern(
                        rule=f"rule-{idx}",
                        before_snippet=f"snippet {idx}",
                    )
                )
                results[idx] = pid
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=append_fn, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        non_empty = [r for r in results if r]
        assert len(non_empty) == num_threads
        assert len(store.load_active()) == num_threads

    def test_concurrent_dedup_does_not_create_duplicates(self, tmp_path):
        store = FixPatternStore(tmp_path / "store.jsonl")
        num_threads = 10
        barrier = threading.Barrier(num_threads)
        results = [None] * num_threads
        errors = []

        def append_fn(idx):
            try:
                barrier.wait(timeout=5)
                pid = store.append(
                    _make_pattern(
                        rule="same-rule",
                        before_snippet="except:\n    pass",
                        source_finding_ids=[f"finding-{idx}"],
                    )
                )
                results[idx] = pid
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=append_fn, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        unique_ids = set(results)
        assert len(unique_ids) == 1


class TestConcurrentLookupWhilePublishing:
    def test_lookup_returns_consistent_results(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="seed-rule",
            language="python",
            before_snippet="seed = a + b",
            after_snippet="seed = a - b",
            confidence=0.9,
            repo_id="seed-repo",
            success_count=5,
        )

        num_readers = 10
        num_writers = 10
        start_barrier = threading.Barrier(num_readers + num_writers)
        lookup_results = [None] * num_readers
        errors = []

        def writer_fn(idx):
            try:
                start_barrier.wait(timeout=5)
                lib.publish(
                    rule="writer-rule",
                    language="python",
                    before_snippet=f"w = {idx} + 1",
                    after_snippet=f"w = {idx} - 1",
                    confidence=0.9,
                    repo_id=f"writer-repo-{idx}",
                )
            except Exception as exc:
                errors.append(exc)

        def reader_fn(idx):
            try:
                start_barrier.wait(timeout=5)
                result = lib.lookup("seed-rule", "seed = a + b", "python")
                lookup_results[idx] = result
            except Exception as exc:
                errors.append(exc)

        threads = (
            [threading.Thread(target=writer_fn, args=(i,)) for i in range(num_writers)]
            + [threading.Thread(target=reader_fn, args=(i,)) for i in range(num_readers)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        for result in lookup_results:
            if result is not None:
                assert result.confidence >= 0.0
                assert result.confidence <= 1.0
                assert isinstance(result.source_repos, set)

    def test_store_lookup_while_appending_no_crash(self, tmp_path):
        store = FixPatternStore(tmp_path / "store.jsonl")
        store.append(_make_pattern(rule="lookup-rule", before_snippet="target snippet"))

        num_readers = 10
        num_writers = 10
        start_barrier = threading.Barrier(num_readers + num_writers)
        errors = []

        def writer_fn(idx):
            try:
                start_barrier.wait(timeout=5)
                store.append(_make_pattern(rule=f"w-rule-{idx}", before_snippet=f"w-snippet-{idx}"))
            except Exception as exc:
                errors.append(exc)

        def reader_fn(_idx):
            try:
                start_barrier.wait(timeout=5)
                store.lookup("lookup-rule", "target snippet")
            except Exception as exc:
                errors.append(exc)

        threads = (
            [threading.Thread(target=writer_fn, args=(i,)) for i in range(num_writers)]
            + [threading.Thread(target=reader_fn, args=(i,)) for i in range(num_readers)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


class TestConcurrentPublishAndLookup:
    def test_mixed_operations_no_crash(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        iterations = 50
        barrier = threading.Barrier(2)
        errors = []

        def publisher():
            try:
                barrier.wait(timeout=5)
                for i in range(iterations):
                    lib.publish(
                        rule="mix-rule",
                        language="python",
                        before_snippet=f"val = {i} + 1",
                        after_snippet=f"val = {i} - 1",
                        confidence=0.9,
                        repo_id=f"mix-repo-{i % 3}",
                    )
            except Exception as exc:
                errors.append(exc)

        def looker():
            try:
                barrier.wait(timeout=5)
                for _ in range(iterations):
                    lib.lookup("mix-rule", "val = 1 + 1", "python")
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=publisher)
        t2 = threading.Thread(target=looker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == []


class TestConcurrentUpdateConfidence:
    def test_final_confidence_is_consistent(self, tmp_path):
        store = FixPatternStore(tmp_path / "store.jsonl")
        pid = store.append(_make_pattern(rule="conf-rule", before_snippet="conf snippet", confidence=0.5))

        num_threads = 10
        delta = 0.05
        barrier = threading.Barrier(num_threads)
        errors = []

        def update_fn(_idx):
            try:
                barrier.wait(timeout=5)
                store.update_confidence(pid, delta)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=update_fn, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        final = store.get_pattern(pid)
        assert final is not None
        expected_min = 0.5 + delta * num_threads
        expected_max = min(1.0, expected_min)
        assert expected_min - 0.01 <= final.confidence <= expected_max + 0.01

    def test_concurrent_positive_and_negative_deltas(self, tmp_path):
        store = FixPatternStore(tmp_path / "store.jsonl")
        pid = store.append(_make_pattern(rule="bidir-rule", before_snippet="bidir snippet", confidence=0.5))

        num_pos = 10
        num_neg = 10
        barrier = threading.Barrier(num_pos + num_neg)
        errors = []

        def pos_fn(_idx):
            try:
                barrier.wait(timeout=5)
                store.update_confidence(pid, 0.1)
            except Exception as exc:
                errors.append(exc)

        def neg_fn(_idx):
            try:
                barrier.wait(timeout=5)
                store.update_confidence(pid, -0.1)
            except Exception as exc:
                errors.append(exc)

        threads = (
            [threading.Thread(target=pos_fn, args=(i,)) for i in range(num_pos)]
            + [threading.Thread(target=neg_fn, args=(i,)) for i in range(num_neg)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        final = store.get_pattern(pid)
        assert final is not None
        assert 0.0 <= final.confidence <= 1.0
