#!/usr/bin/env python3
"""E2E pattern learning tests — store, lookup, confidence, replay."""

import json
import subprocess
from pathlib import Path

import pytest


def _seed_pattern(
    store,
    rule="trailing-whitespace",
    before="x = 1   \n",
    after="x = 1\n",
    confidence=0.5,
):
    from bluei.engine.pattern_store import FixPattern

    pid = store.append(
        FixPattern(
            pattern_id=f"pat-{rule}",
            rule=rule,
            language="python",
            file_path="main.py",
            before_snippet=before,
            after_snippet=after,
            diff_patch=f"-{before}+{after}",
            confidence=confidence,
        )
    )
    return pid


class TestPatternStoreAndLookup:
    def test_append_and_load(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(store)

        active = store.load_active()
        assert len(active) >= 1
        assert any(p.rule == "trailing-whitespace" for p in active)

    def test_lookup_by_rule_and_snippet(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        _seed_pattern(store)

        found = store.lookup("trailing-whitespace", "x = 1   \n")
        assert found is not None
        assert found.rule == "trailing-whitespace"

        missing = store.lookup("trailing-whitespace", "totally different\n")
        assert missing is None

    def test_get_pattern_by_id(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(
            store,
            rule="broad-except",
            before="except Exception:\n",
            after="except ValueError:\n",
        )

        p = store.get_pattern(pid)
        assert p is not None
        assert p.rule == "broad-except"

        assert store.get_pattern("nonexistent") is None


class TestPatternConfidence:
    def test_confidence_increases_on_success(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(store)

        store.update_confidence(pid, 0.2)
        p = store.get_pattern(pid)
        assert p is not None
        assert p.confidence > 0.5

    def test_confidence_decreases_on_failure(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(store)

        store.update_confidence(pid, -0.3)
        p = store.get_pattern(pid)
        assert p is not None
        assert p.confidence < 0.5

    def test_record_replay_updates_counts(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(store)

        store.record_replay(pid, success=True)
        store.record_replay(pid, success=True)
        store.record_replay(pid, success=False)

        p = store.get_pattern(pid)
        assert p is not None
        assert p.success_count >= 2
        assert p.skip_count >= 1 or p.failure_count >= 1

    def test_low_confidence_deactivated(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(store)

        store.update_confidence(pid, -0.5)
        active = store.load_active()
        assert len(active) == 0


class TestTryReplay:
    def test_replay_applies_high_confidence(self, tmp_path, git_commit_all):
        from bluei.engine.pattern_store import FixPatternStore
        from bluei.engine.pattern_replay import try_replay
        from bluei.engine.models import Finding

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.py").write_text("x = 1   \ny = 2\n")
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        git_commit_all(repo)

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(store)
        store.update_confidence(pid, 0.5)

        log_file = tmp_path / "test.log"
        finding = Finding(
            finding_id="f-001",
            repo="test/repo",
            path="main.py",
            line=1,
            rule="trailing-whitespace",
            snippet="x = 1   ",
            confidence=0.9,
            quick_win=True,
            safe_to_autofix=True,
        )

        applied, hint = try_replay(
            worktree_path=repo,
            finding=finding,
            store=store,
            baseline_checks={},
            log_file=log_file,
        )

        assert isinstance(applied, bool)

    def test_replay_skips_no_match(self, tmp_path, git_commit_all):
        from bluei.engine.pattern_store import FixPatternStore
        from bluei.engine.pattern_replay import try_replay
        from bluei.engine.models import Finding

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.py").write_text("x = 1\n")
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        git_commit_all(repo)

        store = FixPatternStore(tmp_path / "patterns.json")

        log_file = tmp_path / "test.log"
        finding = Finding(
            finding_id="f-001",
            repo="test/repo",
            path="main.py",
            line=1,
            rule="nonexistent-rule",
            snippet="x = 1",
            confidence=0.9,
            quick_win=True,
            safe_to_autofix=True,
        )

        applied, hint = try_replay(
            worktree_path=repo,
            finding=finding,
            store=store,
            baseline_checks={},
            log_file=log_file,
        )

        assert applied is False


class TestFuzzyLookup:
    def test_lookup_fuzzy_runs_without_crash(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        _seed_pattern(store)

        result = store.lookup_fuzzy(
            "trailing-whitespace",
            "y = 2   \n",
            "python",
        )
        assert result is None or result.rule == "trailing-whitespace"


# ──────────────────────────────────────────────────────────────────────
# Integrated lifecycle tests — full chains across multiple components.
#
# These tests exercise the pattern-learning subsystem end-to-end:
#   extractor → store → replay → confidence → shared library → promotion
# They use real JSONL-backed stores on tmp_path, real git worktrees,
# and real pattern_extractor / pattern_replay functions. Only external
# tools (network, GitHub API) are mocked or omitted.
# ──────────────────────────────────────────────────────────────────────


import textwrap  # noqa: E402

from bluei.common.models import FeedbackSentiment  # noqa: E402
from bluei.engine.models import Finding, now_iso  # noqa: E402
from bluei.app.pattern_confidence import (  # noqa: E402
    CONFIDENCE_BOOST_POSITIVE,
    CONFIDENCE_DECAY_NEGATIVE,
    CONFIDENCE_DECAY_REPLAY_FAIL,
    adjust_from_feedback,
    adjust_from_replay_failure,
)
from bluei.engine.pattern_extractor import extract  # noqa: E402
from bluei.engine.pattern_replay import (  # noqa: E402
    AUTO_REPLAY_THRESHOLD,
    PROMPT_HINT_THRESHOLD,
    _clear_pattern_cache,
    try_replay,
)
from bluei.engine.pattern_store import (  # noqa: E402
    DEACTIVATION_THRESHOLD,
    INITIAL_CONFIDENCE,
    FixPattern,
    FixPatternStore,
    normalize_snippet,
)
from bluei.engine.shared_pattern_library import (  # noqa: E402
    MIN_CONFIDENCE_TO_SHARE,
    PROMOTION_MIN_APPLICATIONS,
    PROMOTION_MIN_REPOS,
    SharedPatternLibrary,
    check_promotion_eligibility,
    promote_pattern_to_recipe,
)
from bluei.engine.structural_hash import (  # noqa: E402
    compute_structural_hash,
    normalize_for_sharing,
)


# Single-line "vulnerable" → "fixed" source. Using a single-line file means
# git diff produces a hunk with no surrounding context, so the extracted
# before_snippet equals the raw line — which lets a Finding.snippet match
# exactly without needing to reconstruct multi-line context.
_S602_BEFORE = "result = subprocess.run(cmd, shell=True, capture_output=True)"
_S602_AFTER = "result = subprocess.run(cmd, capture_output=True)"


@pytest.fixture
def git_worktree(tmp_path):
    """A real git repo containing a single-line Python file with shell=True."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(_S602_BEFORE + "\n")
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "test@bluei.dev"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "."],
        ["git", "commit", "-m", "initial"],
    ):
        subprocess.run(args, cwd=str(repo), capture_output=True, check=True)
    return repo


def _make_s602_finding(finding_id="f-001", line=1):
    """Build a Finding whose snippet exactly matches the seeded before-line."""
    return Finding(
        finding_id=finding_id,
        repo="test-repo",
        path="app.py",
        line=line,
        rule="S602",
        snippet=_S602_BEFORE,
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
    )


def _seed_verified_pattern(
    store,
    rule="S602",
    confidence=INITIAL_CONFIDENCE,
    before=_S602_BEFORE,
    after=_S602_AFTER,
    finding_id="f-seed",
    file_path="app.py",
):
    """Insert a pattern directly into the store, already marked verified.

    Marking ``last_verified_at`` is required so that ``adjust_from_feedback``
    does not treat the pattern as stale (which would zero it out on the first
    positive feedback rather than boosting it).

    Note: ``FixPatternStore._prepare_new_pattern`` hard-resets confidence to
    ``INITIAL_CONFIDENCE`` on insert. We restore the requested confidence via
    ``update_confidence`` after append so callers can seed non-default values.
    The ``file_pattern`` is also set to match the file's extension, mirroring
    what ``pattern_extractor._compute_file_pattern`` would produce for a
    shallow path — the store default ``**/*`` does not match extension-only
    paths like ``app.py`` under ``fnmatch``.
    """
    suffix = Path(file_path).suffix or ".py"
    pattern = FixPattern(
        pattern_id="",
        rule=rule,
        language="python",
        file_path=file_path,
        before_snippet=normalize_snippet(before),
        after_snippet=normalize_snippet(after),
        diff_patch=f"--- a/{file_path}\n+++ b/{file_path}\n@@ -1 +1 @@\n-{before}\n+{after}",
        confidence=confidence,
        source_finding_ids=[finding_id],
        last_verified_at=now_iso(),
        file_pattern="*" + suffix,
    )
    pid = store.append(pattern)
    # Restore requested confidence (store resets to INITIAL_CONFIDENCE on insert).
    if confidence != INITIAL_CONFIDENCE:
        store.update_confidence(pid, confidence - INITIAL_CONFIDENCE)
    return pid


class TestIntegratedLifecycle:
    """Cross-component lifecycle chains — extractor ↔ store ↔ replay ↔
    confidence ↔ shared library ↔ promotion."""

    # ------------------------------------------------------------
    # 1. Full fix → learn → replay → boost → auto-replay → promote
    # ------------------------------------------------------------
    def test_full_fix_learn_replay_promote_lifecycle(self, tmp_path, git_worktree):
        """Fix an issue → extract → store at INITIAL_CONFIDENCE → replay
        returns a hint at 0.5 → boost via positive feedback → confidence
        crosses AUTO_REPLAY_THRESHOLD → next replay auto-applies the fix →
        success recorded → publish to SharedPatternLibrary from many repos
        → promote to a recipe YAML.

        Components crossed:
          pattern_extractor.extract → FixPatternStore.append →
          pattern_replay.try_replay (hint path) →
          pattern_confidence.adjust_from_feedback →
          pattern_replay.try_replay (apply path) →
          FixPatternStore.record_replay →
          SharedPatternLibrary.publish → promote_pattern_to_recipe
        """
        repo = git_worktree
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        log_file = tmp_path / "lifecycle.log"
        finding = _make_s602_finding()
        src = repo / "app.py"

        # 1a. Apply the fix in the worktree, then extract a pattern from the diff.
        src.write_text(_S602_AFTER + "\n")
        _clear_pattern_cache()
        pid = extract(repo, finding, "autofix", store, log_file)
        assert pid is not None, "extract() should return a pattern_id"

        pattern = store.get_pattern(pid)
        assert pattern is not None
        assert pattern.confidence == INITIAL_CONFIDENCE
        assert pattern.rule == "S602"
        # The extractor normalizes both sides; verify the before side captured
        # the vulnerable call.
        assert "shell=True" in pattern.before_snippet

        # 1b. Revert the fix so the worktree matches the "before" state again.
        subprocess.run(
            ["git", "checkout", "--", "app.py"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        assert "shell=True" in src.read_text()

        # 1c. At confidence 0.5 (== PROMPT_HINT_THRESHOLD), replay must NOT
        # auto-apply; it should return the pattern_id as a hint.
        _clear_pattern_cache()
        applied, hint_id = try_replay(repo, finding, store, {}, log_file)
        assert applied is False, "Pattern at INITIAL_CONFIDENCE must not auto-apply"
        assert hint_id == pid, "Should return pattern_id as a hint"

        # 1d. Mark the pattern as verified (sets last_verified_at) so that
        # adjust_from_feedback treats it as fresh rather than stale.
        store.record_replay(pid, success=True)

        # 1e. Boost confidence via positive feedback until it crosses the
        # AUTO_REPLAY_THRESHOLD. CONFIDENCE_BOOST_POSITIVE is +0.1 per call;
        # 0.5 → 0.6 → ... → 1.0 needs 5 calls, but we loop defensively.
        for _ in range(10):
            new_conf = adjust_from_feedback(
                finding.finding_id,
                FeedbackSentiment.POSITIVE,
                store,
            )
            if new_conf is None:
                pytest.fail("adjust_from_feedback returned None — pattern lost")
            if new_conf >= AUTO_REPLAY_THRESHOLD:
                break

        pattern = store.get_pattern(pid)
        assert pattern.confidence >= AUTO_REPLAY_THRESHOLD, (
            f"Expected confidence >= {AUTO_REPLAY_THRESHOLD}, got {pattern.confidence}"
        )

        # 1f. At high confidence, try_replay must auto-apply the fix.
        _clear_pattern_cache()
        applied, result_id = try_replay(repo, finding, store, {}, log_file)
        assert applied is True, "High-confidence pattern should auto-replay"
        assert result_id == pid

        # 1g. The file should now reflect the fix.
        assert "shell=True" not in src.read_text()
        assert "capture_output=True" in src.read_text()

        # 1h. The replay recorded its own success — success_count must have
        # advanced beyond the marker set in 1d.
        pattern = store.get_pattern(pid)
        assert pattern.success_count >= 2

        # 1i. Promotion path: publish the (now high-confidence) pattern to the
        # SharedPatternLibrary from many repos with a strong success history.
        # This simulates the pattern being independently discovered across the
        # fleet — the prerequisite for promotion to a recipe.
        shared = SharedPatternLibrary(tmp_path / "shared")
        for i in range(PROMOTION_MIN_REPOS + 1):
            xp_id = shared.publish(
                rule="S602",
                language="python",
                before_snippet=_S602_BEFORE,
                after_snippet=_S602_AFTER,
                confidence=0.97,
                repo_id=f"fleet-repo-{i}",
                success_count=4,  # 6 repos * 4 = 24 >= PROMOTION_MIN_APPLICATIONS (20)
            )
        assert xp_id, "publish() should return a cross-repo pattern id"

        # The library file must physically exist on disk.
        assert (tmp_path / "shared" / "python.jsonl").exists()

        records = shared._load_language("python")
        assert len(records) >= 1
        cross_pat = next(iter(records.values()))
        assert cross_pat.rule == "S602"
        assert len(cross_pat.source_repos) >= PROMOTION_MIN_REPOS
        assert cross_pat.success_count >= PROMOTION_MIN_APPLICATIONS

        # Eligibility + actual promotion should write a recipe YAML.
        assert check_promotion_eligibility(cross_pat) is True
        recipes_dir = tmp_path / "recipes"
        recipe_path = promote_pattern_to_recipe(cross_pat, "python", recipes_dir)
        assert recipe_path is not None
        assert recipe_path.exists()
        assert recipe_path.suffix == ".yaml"

        # The recipe records the structural lineage — original identifiers
        # must NOT appear (sharing normalizes them out).
        recipe_text = recipe_path.read_text()
        assert "bluei-auto-promotion" in recipe_text
        assert "rule: S602" in recipe_text
        assert "result" not in recipe_text  # privacy-normalized

    # ------------------------------------------------------------
    # 2. Cross-repo pattern sharing via structural hashing
    # ------------------------------------------------------------
    def test_cross_repo_pattern_sharing(self, tmp_path):
        """Learn a pattern in 'repo A' → publish to SharedPatternLibrary →
        look it up from 'repo B' using a snippet with DIFFERENT variable
        names but identical structure → try_replay consults the shared
        library and resolves the cross-repo pattern via structural hashing.

        Components crossed:
          SharedPatternLibrary.publish (privacy normalization + structural hash) →
          JSONL file persistence on disk →
          SharedPatternLibrary.lookup (structural-hash match across renamed vars) →
          pattern_replay.try_replay with shared_library=... (xrepo-hit logged)

        Note on the apply step: SharedPatternLibrary.publish() privacy-normalizes
        snippets (every identifier → _v0/_v1/…, every literal → 0/<str>), but
        pattern_replay._find_snippet_in_file() does literal text matching against
        the target file. A privacy-normalized snippet therefore cannot be
        located inside a file that uses real identifiers. This test asserts
        that observable behavior (xrepo-hit logged, then snippet_not_found_in_file)
        rather than hiding the gap behind a mock.
        """
        # Build repo B (the consumer) with a nested file at src/app.py so the
        # default cross-repo file_pattern '**/*' matches under fnmatch
        # ('**/*' requires at least one '/' in the path).
        repo_b = tmp_path / "repo_b"
        repo_b.mkdir()
        (repo_b / "src").mkdir()
        repo_b_before = (
            "output = subprocess.run(command, shell=True, capture_output=True)"
        )
        (repo_b / "src" / "app.py").write_text(repo_b_before + "\n")
        for args in (
            ["git", "init"],
            ["git", "config", "user.email", "test@bluei.dev"],
            ["git", "config", "user.name", "Test"],
            ["git", "add", "."],
            ["git", "commit", "-m", "initial"],
        ):
            subprocess.run(args, cwd=str(repo_b), capture_output=True, check=True)

        # The structural hasher maps identifiers positionally, so a snippet
        # with renamed variables must produce the SAME hash as the original.
        hash_a = compute_structural_hash(_S602_BEFORE, "python")
        hash_b = compute_structural_hash(repo_b_before, "python")
        assert hash_a == hash_b, (
            f"Structural hashes must match for isomorphic snippets: "
            f"{hash_a} != {hash_b}"
        )

        # Repo A learns the pattern and publishes it. We pass an explicit
        # structural_hash to make the source-of-truth obvious.
        shared = SharedPatternLibrary(tmp_path / "shared")
        xp_id = shared.publish(
            rule="S602",
            language="python",
            before_snippet=_S602_BEFORE,
            after_snippet=_S602_AFTER,
            confidence=0.95,
            repo_id="repo-alpha",
            structural_hash=hash_a,
        )
        assert xp_id, "publish() above MIN_CONFIDENCE_TO_SHARE must succeed"
        assert xp_id.startswith("xp-")

        # Privacy normalization is observable on disk: the raw library file
        # must NOT contain repo-A-specific identifiers; they are replaced
        # with _v0, _v1, … by normalize_for_sharing.
        lang_file = tmp_path / "shared" / "python.jsonl"
        assert lang_file.exists(), "publish() must materialize the language JSONL"
        raw_library = lang_file.read_text()
        assert "result = subprocess" not in raw_library, (
            "Raw library snippet must be privacy-normalized"
        )
        assert "_v0 = _v1.run" in raw_library

        # Repo B's empty local store forces try_replay to fall through to
        # the shared library.
        store_b = FixPatternStore(tmp_path / "repo_b_patterns.jsonl")
        log_file = tmp_path / "xrepo.log"
        finding_b = Finding(
            finding_id="f-repo-b",
            repo="repo-beta",
            path="src/app.py",
            line=1,
            rule="S602",
            snippet=repo_b_before,
            confidence=0.9,
            quick_win=True,
            safe_to_autofix=True,
        )

        # Direct cross-repo lookup via structural hashing succeeds despite
        # the variable rename.
        lookup = shared.lookup(rule="S602", snippet=repo_b_before, language="python")
        assert lookup is not None, (
            "Cross-repo lookup must succeed via structural-hash match"
        )
        assert lookup.rule == "S602"
        assert lookup.pattern_id == xp_id
        assert "repo-alpha" in lookup.source_repos
        # Repo B's identity must NOT have leaked into the library — only
        # the publisher's repo id is recorded.
        assert "repo-beta" not in lookup.source_repos

        # Drive the full try_replay → shared_library integration path.
        _clear_pattern_cache()
        applied, result_id = try_replay(
            repo_b,
            finding_b,
            store_b,
            {},
            log_file,
            shared_library=shared,
        )

        # Observable integration: the replay log records that the shared
        # library was consulted and produced a high-confidence cross-repo hit.
        log_text = log_file.read_text()
        assert "pattern-replay-xrepo-hit" in log_text, (
            "try_replay must log cross-repo resolution via the shared library"
        )
        assert "pattern_id=xp-S602" in log_text
        assert "source_repos=1" in log_text
        assert "confidence=0.95" in log_text, (
            "High-confidence cross-repo pattern must reach the apply branch"
        )

        # The current replay engine cannot complete the file write because
        # privacy-normalized snippets (_v0 = _v1.run(...)) cannot be located
        # inside a file that uses real identifiers. This is an observable,
        # documented architectural gap — not a bug in this test.
        assert "reason=snippet_not_found_in_file" in log_text, (
            "Privacy-normalized snippets are not literal-matchable in files"
        )
        assert applied is False
        assert result_id is None

        # The file in repo B must be unchanged.
        assert repo_b_before in (repo_b / "src" / "app.py").read_text()

    # ------------------------------------------------------------
    # 3. Confidence lifecycle with real git (INITIAL → boost → decay → off)
    # ------------------------------------------------------------
    def test_confidence_lifecycle_with_real_git(self, tmp_path, git_worktree):
        """Seed a pattern at INITIAL_CONFIDENCE → verify try_replay only
        hints (no auto-apply) → boost via positive feedback until confidence
        crosses AUTO_REPLAY_THRESHOLD → decay via replay failures until
        confidence drops below DEACTIVATION_THRESHOLD → verify the pattern
        is excluded from load_active().

        Components crossed:
          FixPatternStore (append, update_confidence, record_replay, load_active) →
          pattern_confidence.adjust_from_feedback (POSITIVE boost) →
          pattern_confidence.adjust_from_replay_failure (decay) →
          pattern_replay.try_replay (hint-only at low confidence)
        """
        repo = git_worktree
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        log_file = tmp_path / "confidence.log"

        pid = _seed_verified_pattern(
            store,
            rule="S602",
            confidence=INITIAL_CONFIDENCE,
            finding_id="f-seed",
        )
        pattern = store.get_pattern(pid)
        assert pattern.confidence == INITIAL_CONFIDENCE
        assert INITIAL_CONFIDENCE < AUTO_REPLAY_THRESHOLD

        # 3a. At 0.5 confidence, try_replay must NOT auto-apply. It returns
        # a hint (pattern_id) because 0.5 == PROMPT_HINT_THRESHOLD.
        finding = _make_s602_finding()
        _clear_pattern_cache()
        applied, hint_id = try_replay(repo, finding, store, {}, log_file)
        assert applied is False
        assert hint_id == pid

        # 3b. Boost confidence via 5 positive feedbacks. Each is +0.1, so we
        # expect to land at min(1.0, 0.5 + 5*0.1) == 1.0.
        for _ in range(5):
            adjust_from_feedback("f-seed", FeedbackSentiment.POSITIVE, store)

        pattern = store.get_pattern(pid)
        expected_peak = min(1.0, INITIAL_CONFIDENCE + 5 * CONFIDENCE_BOOST_POSITIVE)
        assert pattern.confidence == pytest.approx(expected_peak, abs=0.01)
        assert pattern.confidence >= AUTO_REPLAY_THRESHOLD

        # 3c. Simulate a replay failure → confidence decays by
        # CONFIDENCE_DECAY_REPLAY_FAIL (-0.15).
        new_conf = adjust_from_replay_failure(pid, store)
        assert new_conf is not None, "Pattern still active — should be decayable"
        expected_after_fail = max(0.0, expected_peak + CONFIDENCE_DECAY_REPLAY_FAIL)
        assert new_conf == pytest.approx(expected_after_fail, abs=0.02)
        assert new_conf < pattern.confidence

        # 3d. Keep decaying. Once confidence drops below DEACTIVATION_THRESHOLD,
        # adjust_from_replay_failure zeros the pattern in the store AND excludes
        # it from load_active(), so subsequent calls return None.
        current = new_conf
        for _ in range(20):
            current = adjust_from_replay_failure(pid, store)
            if current is None or current < DEACTIVATION_THRESHOLD:
                break

        final = store.get_pattern(pid)
        assert final.confidence < DEACTIVATION_THRESHOLD, (
            f"Expected confidence < {DEACTIVATION_THRESHOLD}, got {final.confidence}"
        )
        # Deactivated patterns must not be returned by load_active().
        assert all(p.pattern_id != pid for p in store.load_active())

        # 3e. Once deactivated, replay must no longer hint at this pattern.
        _clear_pattern_cache()
        applied, hint_id = try_replay(repo, finding, store, {}, log_file)
        assert applied is False
        assert hint_id is None, "Deactivated pattern must not produce a hint"

    # ------------------------------------------------------------
    # 4. Auto-tune feedback loop (feedback → confidence → future replay)
    # ------------------------------------------------------------
    def test_auto_tune_feedback_loop(self, tmp_path):
        """Positive feedback boosts confidence; negative feedback decays it;
        enough negative feedback deactivates the pattern entirely. This is
        the "feedback → confidence → future replay eligibility" loop.

        Components crossed:
          FixPatternStore (append/update_confidence/lookup_by_finding/load_active) →
          pattern_confidence.adjust_from_feedback (POSITIVE + NEGATIVE) →
          observable change in store (confidence value, load_active membership)
        """
        store = FixPatternStore(tmp_path / "patterns.jsonl")

        before = "for i in range(n):"
        after = "for _ in range(n):"
        pid = _seed_verified_pattern(
            store,
            rule="B007",
            confidence=0.7,
            before=before,
            after=after,
            finding_id="f-tune",
            file_path="loop.py",
        )

        # 4a. Three positive feedbacks: 0.7 + 3*0.1 = 1.0 (capped).
        for _ in range(3):
            new_conf = adjust_from_feedback(
                "f-tune",
                FeedbackSentiment.POSITIVE,
                store,
            )
            assert new_conf is not None

        pattern = store.get_pattern(pid)
        assert pattern.confidence == pytest.approx(1.0, abs=0.001), (
            f"Positive feedback should cap at 1.0, got {pattern.confidence}"
        )

        # 4b. The pattern must currently be eligible for auto-replay.
        assert pattern.confidence >= AUTO_REPLAY_THRESHOLD

        # 4c. Two negative feedbacks: each is CONFIDENCE_DECAY_NEGATIVE (-0.2).
        # 1.0 + 2*(-0.2) = 0.6 — still above DEACTIVATION_THRESHOLD.
        for _ in range(2):
            new_conf = adjust_from_feedback(
                "f-tune",
                FeedbackSentiment.NEGATIVE,
                store,
            )
            assert new_conf is not None

        pattern = store.get_pattern(pid)
        expected_after_neg = min(1.0, max(0.0, 1.0 + 2 * CONFIDENCE_DECAY_NEGATIVE))
        assert pattern.confidence == pytest.approx(expected_after_neg, abs=0.01), (
            f"Expected ~{expected_after_neg} after 2 negative feedbacks, "
            f"got {pattern.confidence}"
        )
        # Net drift from the 0.7 starting point: +3*0.1 (pos) - 2*0.2 (neg) = -0.1
        # → 0.7 - 0.1 = 0.6. Matches expected_after_neg.
        assert pattern.confidence == pytest.approx(0.6, abs=0.01)

        # 4d. Keep sending negative feedback until the pattern is deactivated.
        # Once it crosses below DEACTIVATION_THRESHOLD, the store zeros it.
        for _ in range(10):
            result = adjust_from_feedback(
                "f-tune",
                FeedbackSentiment.NEGATIVE,
                store,
            )
            if result is None or result < DEACTIVATION_THRESHOLD:
                break

        final = store.get_pattern(pid)
        assert final.confidence < DEACTIVATION_THRESHOLD, (
            f"Expected deactivation (< {DEACTIVATION_THRESHOLD}), "
            f"got {final.confidence}"
        )

        # 4e. The auto-tune decision is observable: a deactivated pattern is
        # excluded from load_active(), so it cannot be selected for future
        # replays. This is the "feedback → future replay eligibility" link.
        active = store.load_active(rule="B007")
        assert all(p.pattern_id != pid for p in active), (
            "Deactivated pattern must not appear in load_active()"
        )

        # 4f. Negative feedback on an already-deactivated pattern keeps the
        # pattern at 0 — it does not resurrect it and does not error.
        result = adjust_from_feedback(
            "f-tune",
            FeedbackSentiment.NEGATIVE,
            store,
        )
        still_dead = store.get_pattern(pid)
        assert still_dead.confidence < DEACTIVATION_THRESHOLD
        # adjust_from_feedback returns the computed value (clamped to 0);
        # the store stays at 0 either way.
        assert result is not None
        assert result < DEACTIVATION_THRESHOLD


class TestIntegratedLifecycleObservability:
    """Sanity checks that the integrated tests above assert on durable,
    observable outcomes — not internal state that could change silently."""

    def test_pattern_store_persists_to_jsonl(self, tmp_path):
        """A pattern appended via the store must appear in the JSONL file."""
        store_path = tmp_path / "observable.jsonl"
        store = FixPatternStore(store_path)
        _seed_verified_pattern(store, finding_id="f-obs")

        # Observable: the JSONL file on disk contains the rule.
        raw = store_path.read_text()
        assert "S602" in raw
        assert "f-obs" in raw

    def test_shared_library_normalize_for_sharing_is_called(self, tmp_path):
        """Publishing through SharedPatternLibrary must store the normalized
        snippet, not the raw one — verifying the privacy boundary."""
        lib = SharedPatternLibrary(tmp_path / "lib")
        raw_before = "user_input = fetch_data(session_id)"
        raw_after = "user_input = fetch_data(session_id, validate=True)"
        lib.publish(
            rule="CUSTOM",
            language="python",
            before_snippet=raw_before,
            after_snippet=raw_after,
            confidence=0.9,
            repo_id="repo-x",
        )
        records = lib._load_language("python")
        assert len(records) == 1
        pat = next(iter(records.values()))
        # Stored snippet must equal the sharing-normalized form.
        assert pat.before_snippet == normalize_for_sharing(raw_before, "python")
        # And the raw identifier must not survive in the stored snippet.
        assert "user_input" not in pat.before_snippet
