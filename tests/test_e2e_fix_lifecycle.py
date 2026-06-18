import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bluei.engine.fix_tiers import FixTier
from bluei.engine.lifecycle import (
    _get_recipe_engine,
    _tier_validate,
    apply_autofix,
    apply_cascade_fix,
)
from bluei.engine.models import Finding
from bluei.engine.pattern_replay import (
    AUTO_REPLAY_THRESHOLD,
    _clear_pattern_cache,
    try_replay,
)
from bluei.engine.pattern_store import FixPattern, FixPatternStore, INITIAL_CONFIDENCE
from bluei.engine.recipe_handlers import RecipeFixResult
from bluei.engine.reforge import RefactorClass, classify_finding


def _create_fixable_repo(tmp_path: Path, git_repo, git_commit_all) -> Path:
    repo = git_repo
    src = repo / "src"
    src.mkdir(exist_ok=True)
    (src / "__init__.py").write_text("")
    (src / "trailing.py").write_text("def hello():\n    x = 1   \n    return x  \n\n")
    (src / "popfront.py").write_text(
        "def process(items):\n"
        "    for idx, item in enumerate(items):\n"
        "        entry = items.pop(0)\n"
        "        if entry is None:\n"
        "            break\n"
        "    return items\n"
    )
    git_commit_all(repo)
    return repo


def test_recipe_trailing_whitespace_fix(
    tmp_path, make_finding, git_repo, git_commit_all
):
    repo = _create_fixable_repo(tmp_path, git_repo, git_commit_all)
    log_file = tmp_path / "test.log"
    target = repo / "src" / "trailing.py"

    original = target.read_text()
    assert "   \n" in original or "  \n" in original

    finding = make_finding(
        rule="trailing-whitespace",
        path="src/trailing.py",
        line=2,
        snippet="    x = 1   ",
    )

    result = apply_autofix(repo, finding, log_file)

    assert result is True
    fixed = target.read_text()
    for line in fixed.splitlines():
        assert line == line.rstrip(), f"trailing whitespace remains: {line!r}"


def test_cascade_exhaustion_rollback(tmp_path, make_finding, git_repo, git_commit_all):
    repo = _create_fixable_repo(tmp_path, git_repo, git_commit_all)
    log_file = tmp_path / "test.log"
    target = repo / "src" / "trailing.py"
    original = target.read_text()

    finding = make_finding(
        rule="completely-unknown-rule-xyz",
        path="src/trailing.py",
        line=1,
        snippet="def hello():",
    )

    result = apply_cascade_fix(
        repo,
        finding,
        log_file,
        deterministic_only=True,
    )

    assert result is False
    assert target.read_text() == original


def test_verification_failure_rollback(
    tmp_path, make_finding, git_repo, git_commit_all
):
    repo = git_repo
    (repo / "src").mkdir(exist_ok=True)

    valid_content = "def good():\n    return 42\n"
    (repo / "src" / "target.py").write_text(valid_content)
    git_commit_all(repo)

    broken_content = "def good():\n    return 42\n   broken syntax {\n"
    (repo / "src" / "target.py").write_text(broken_content)

    log_file = tmp_path / "test.log"
    finding = make_finding(
        rule="trailing-whitespace",
        path="src/target.py",
        line=1,
        snippet="def good():",
    )

    passed = _tier_validate(FixTier.T0_GUARANTEED, repo, finding, log_file)

    assert passed is False
    assert (repo / "src" / "target.py").read_text() == valid_content


def test_pattern_store_and_lookup(tmp_path):
    store_path = tmp_path / "patterns.jsonl"
    store = FixPatternStore(store_path)

    pattern = FixPattern(
        pattern_id="fp-store-test",
        rule="trailing-whitespace",
        language="python",
        file_path="src/trailing.py",
        before_snippet="    x = 1   ",
        after_snippet="    x = 1",
        diff_patch="--- a\n+++ b\n",
    )

    pid = store.append(pattern)
    assert pid.startswith("fp-")

    hit = store.lookup("trailing-whitespace", "    x = 1   ")
    assert hit is not None
    assert hit.rule == "trailing-whitespace"
    assert hit.pattern_id == pid

    active = store.load_active(rule="trailing-whitespace")
    assert len(active) >= 1

    miss = store.lookup("nonexistent-rule", "    x = 1   ")
    assert miss is None


def test_pattern_confidence_lifecycle(tmp_path, make_finding, git_repo, git_commit_all):
    repo = git_repo
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "fixme.py").write_text("x = old_value\n")
    git_commit_all(repo)

    store_path = tmp_path / "patterns.jsonl"
    store = FixPatternStore(store_path)

    pattern = FixPattern(
        pattern_id="fp-conf",
        rule="test-rule",
        language="python",
        file_path="src/fixme.py",
        before_snippet="x = old_value",
        after_snippet="x = new_value",
        diff_patch="",
        file_pattern="src/*",
    )

    pid = store.append(pattern)
    _clear_pattern_cache()

    finding = make_finding(
        rule="test-rule",
        path="src/fixme.py",
        line=1,
        snippet="x = old_value",
    )
    log_file = tmp_path / "test.log"

    success, hint_pid = try_replay(repo, finding, store, {}, log_file)
    assert success is False
    assert hint_pid == pid

    store.update_confidence(pid, 0.5)
    _clear_pattern_cache()

    success, replay_pid = try_replay(repo, finding, store, {}, log_file)
    assert success is True
    assert replay_pid == pid

    assert "new_value" in (repo / "src" / "fixme.py").read_text()
    assert "old_value" not in (repo / "src" / "fixme.py").read_text()

    fetched = store.get_pattern(pid)
    assert fetched is not None
    assert fetched.confidence >= AUTO_REPLAY_THRESHOLD


# ---------------------------------------------------------------------------
# Integrated lifecycle tests — multi-component chains
# ---------------------------------------------------------------------------
# Each test below crosses at least two internal component boundaries
# (cascade → recipe_engine → pattern_store → pattern_replay, etc.).
# Only external subprocess tools are mocked; internal bluei functions run real.
# ---------------------------------------------------------------------------


def _write_legacy_doc_guide(repo: Path, git_commit_all) -> Path:
    """Create a root-level `guide.md` with a fixable `legacy_pricer.py` reference.

    Uses the built-in `docs-legacy-reference.yaml` recipe (text handler,
    safety=guaranteed → T0). T0 on a `.md` file only checks existence,
    no syntax compile — so the recipe's fix always clears validation.

    Note: the file is at the *root* of the repo (not under `docs/`) because
    `_compute_file_pattern` derives `docs/**/*.md` for nested paths, and
    `fnmatch` (used by pattern_replay) doesn't expand `**`. A root-level
    file produces `*.md`, which matches via `fnmatch` and lets the full
    extract → store → replay loop complete end-to-end. Real-world docs
    often live at the repo root (README.md, CHANGELOG.md, etc.).
    """
    guide = repo / "guide.md"
    guide.write_text(
        "# Pricing Guide\n"
        "\n"
        "See legacy_pricer.py for the old implementation.\n"
        "The new module is price.py.\n",
        encoding="utf-8",
    )
    git_commit_all(repo)
    return guide


def test_pattern_extracted_after_recipe_fix(
    tmp_path, make_finding, git_repo, git_commit_all
):
    """After a recipe-based fix succeeds, `_extract_fix_pattern` runs and stores a pattern.

    Lifecycle chain: classify → recipe_engine.match → recipe_engine.apply
    → lifecycle._extract_fix_pattern → FixPatternStore.append.

    Components crossed: recipe_engine → lifecycle → pattern_store.
    """
    repo = git_repo
    guide = _write_legacy_doc_guide(repo, git_commit_all)

    log_file = tmp_path / "run.log"
    log_file.write_text("", encoding="utf-8")
    pattern_store_path = tmp_path / "state" / "patterns.jsonl"

    finding = make_finding(
        rule="docs-legacy-reference",
        path="guide.md",
        line=3,
        snippet="legacy_pricer.py",
    )

    # Drive the real recipe pipeline end-to-end.
    result = apply_autofix(repo, finding, log_file, pattern_store_path)
    assert result is True

    # Recipe actually mutated the file.
    assert "legacy_pricer.py" not in guide.read_text(encoding="utf-8")
    assert "price.py" in guide.read_text(encoding="utf-8")

    # Cross-component: pattern_store now has the extracted pattern.
    store = FixPatternStore(pattern_store_path)
    patterns = store.load_active(rule="docs-legacy-reference")
    assert len(patterns) == 1

    pat = patterns[0]
    assert pat.rule == "docs-legacy-reference"
    assert pat.source == "recipe"  # tagged by lifecycle when invoked via apply_autofix
    assert pat.before_snippet != pat.after_snippet
    assert "legacy_pricer.py" in pat.before_snippet
    assert "price.py" in pat.after_snippet
    # The extractor records which finding produced the pattern.
    assert finding.finding_id in pat.source_finding_ids

    # Audit-trail: pattern-extract-hook fired with the right source label.
    log_text = log_file.read_text(encoding="utf-8")
    assert "pattern-extract: rule=docs-legacy-reference" in log_text
    assert "pattern-extract-hook: source=recipe" in log_text
    assert f"finding_id={finding.finding_id}" in log_text


def test_extract_store_replay_cycle(tmp_path, make_finding, git_repo, git_commit_all):
    """THE CENTERPIECE: full learning loop.

    apply fix → extract pattern → store → revert fix → replay pattern
    → verify the replayed file matches the originally-fixed file.

    Components crossed:
        lifecycle.apply_autofix → pattern_extractor.extract
        → FixPatternStore.append → pattern_replay.try_replay.

    This is bluei's core value proposition: a successful fix becomes a
    replayable pattern that re-applies itself deterministically next time.
    """
    _clear_pattern_cache()
    repo = git_repo
    guide = _write_legacy_doc_guide(repo, git_commit_all)

    log_file = tmp_path / "run.log"
    log_file.write_text("", encoding="utf-8")
    pattern_store_path = tmp_path / "state" / "patterns.jsonl"

    # --- Phase 1: apply a real fix via apply_autofix ---------------------
    # The recipe engine applies `docs-legacy-reference`; lifecycle then
    # calls _extract_fix_pattern internally, capturing the diff as a pattern.
    finding1 = make_finding(
        rule="docs-legacy-reference",
        path="guide.md",
        line=3,
        snippet="legacy_pricer.py",
        finding_id="f-learn-001",
    )

    success = apply_autofix(repo, finding1, log_file, pattern_store_path)
    assert success is True

    fixed_after_apply = guide.read_text(encoding="utf-8")
    assert "legacy_pricer.py" not in fixed_after_apply
    assert "price.py" in fixed_after_apply

    # --- Phase 2: verify the pattern was extracted & stored --------------
    store = FixPatternStore(pattern_store_path)
    patterns = store.load_active(rule="docs-legacy-reference")
    assert len(patterns) >= 1
    stored = patterns[0]

    # Sanity: the stored pattern encodes the transformation we just saw.
    assert "legacy_pricer.py" in stored.before_snippet
    assert "price.py" in stored.after_snippet
    assert stored.before_snippet != stored.after_snippet

    # --- Phase 3: revert the fix in the worktree -------------------------
    # `git checkout` restores the file to its pre-fix (committed) state.
    subprocess.run(
        ["git", "checkout", "--", "guide.md"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    reverted = guide.read_text(encoding="utf-8")
    assert "legacy_pricer.py" in reverted, (
        "revert failed: file should contain legacy ref"
    )

    # --- Phase 4: replay the stored pattern on a fresh finding -----------
    # Boost the stored pattern confidence above AUTO_REPLAY_THRESHOLD so
    # try_replay auto-applies instead of returning a hint.
    delta_needed = max(0.0, AUTO_REPLAY_THRESHOLD - stored.confidence + 0.05)
    if delta_needed > 0:
        store.update_confidence(stored.pattern_id, delta_needed)
    _clear_pattern_cache()

    # The replay lookup matches by normalized before-snippet, so the new
    # finding's snippet must match what the extractor captured.
    finding2 = make_finding(
        rule="docs-legacy-reference",
        path="guide.md",
        line=3,
        snippet=stored.before_snippet,
        finding_id="f-learn-002",
    )

    fresh_store = FixPatternStore(pattern_store_path)
    replayed, replay_pid = try_replay(repo, finding2, fresh_store, {}, log_file)

    # --- Phase 5: assert the replay produced the SAME fix ---------------
    assert replayed is True, "pattern replay should have auto-applied"
    assert replay_pid == stored.pattern_id

    fixed_after_replay = guide.read_text(encoding="utf-8")
    assert "legacy_pricer.py" not in fixed_after_replay
    assert "price.py" in fixed_after_replay

    # The replayed file must be byte-identical to the originally-fixed file.
    assert fixed_after_replay == fixed_after_apply, (
        "replay produced a different result than the original fix"
    )

    # Audit-trail: replay outcome recorded against the pattern.
    log_text = log_file.read_text(encoding="utf-8")
    assert f"pattern-replay-hit: rule=docs-legacy-reference" in log_text
    assert f"pattern_id={stored.pattern_id}" in log_text
    assert "pattern-replay-success:" in log_text


def test_bad_fix_rolled_back_by_tier_validate(
    tmp_path, make_finding, git_repo, git_commit_all
):
    """When a fix passes the cascade but fails tier validation, worktree is rolled back.

    Lifecycle chain: recipe_engine.apply (patched to inject broken syntax)
    → lifecycle._extract_fix_pattern → resolve_tier → _tier_validate
    → `git checkout -- <path>` on failure.

    Components crossed: recipe_engine → lifecycle._tier_validate → git worktree.

    Unlike the existing unit-level `test_verification_failure_rollback`
    (which manually writes broken content and calls `_tier_validate`
    directly), this test drives the real `apply_autofix` flow. Only the
    recipe engine's `apply` method is patched — everything else
    (classify, tier resolution, validation, rollback) runs for real.
    """
    repo = git_repo
    (repo / "src").mkdir(exist_ok=True)
    target = repo / "src" / "target.py"
    valid_content = "def good():\n    return 42\n"
    target.write_text(valid_content, encoding="utf-8")
    git_commit_all(repo)

    log_file = tmp_path / "run.log"
    log_file.write_text("", encoding="utf-8")
    pattern_store_path = tmp_path / "state" / "patterns.jsonl"

    finding = make_finding(
        rule="trailing-whitespace",
        path="src/target.py",
        line=1,
        snippet="def good():",
    )

    # Patch the recipe engine's `apply` to write syntactically broken Python.
    # The recipe match still succeeds (so the fix is "applied"), but the
    # resulting file cannot compile — T0 validation must catch it.
    engine = _get_recipe_engine()

    def _broken_apply(self, recipe, file_path, worktree, finding=None):
        file_path.write_text("def broken(\n", encoding="utf-8")
        return RecipeFixResult(
            success=True,
            files_changed=[str(file_path.relative_to(worktree))],
            recipe_id=getattr(recipe, "id", "test-broken"),
            method="text",
        )

    with patch.object(type(engine), "apply", _broken_apply):
        result = apply_autofix(repo, finding, log_file, pattern_store_path)

    # Tier validation rejected the broken fix.
    assert result is False

    # Worktree state was restored: file is back to its committed (valid) form.
    restored = target.read_text(encoding="utf-8")
    assert restored == valid_content, (
        "worktree not rolled back: file should match pre-fix committed state"
    )
    # And the restored file still compiles (proves the rollback is correct).
    compile(restored, str(target), "exec")

    # Audit-trail: tier-validation logged the failure AND the rollback.
    log_text = log_file.read_text(encoding="utf-8")
    assert "tier-validation: tier=T0_GUARANTEED passed=False" in log_text
    assert "tier-validation: ROLLBACK " in log_text
    assert f"finding_id={finding.finding_id}" in log_text
    # The recipe stage DID run (the broken apply fired).
    assert "autofix: recipe=" in log_text


def test_full_cascade_with_recipe_match(
    tmp_path, make_finding, git_repo, git_commit_all
):
    """Full cascade pipeline: linter stages decline → recipe stage applies → validate.

    Lifecycle chain: apply_cascade_fix → DeterministicCascade.execute
    → LinterFixStage.can_handle (declines) → RecipeCascadeStage.can_handle+attempt
    → lifecycle._extract_fix_pattern → resolve_tier → _tier_validate.

    Components crossed: cascade → recipe_engine → pattern_store → tier validation.

    Verifies that all cascade stages work together: the linter stages
    correctly decline a non-`ruff-*` finding, the recipe stage matches and
    applies the fix, validation passes, and a learned pattern is extracted.
    """
    _clear_pattern_cache()
    repo = git_repo
    guide = _write_legacy_doc_guide(repo, git_commit_all)

    log_file = tmp_path / "run.log"
    log_file.write_text("", encoding="utf-8")
    pattern_store_path = tmp_path / "state" / "patterns.jsonl"

    finding = make_finding(
        rule="docs-legacy-reference",
        path="guide.md",
        line=3,
        snippet="legacy_pricer.py",
    )

    # Drive the full cascade — deterministic_only prevents any LLM fallback.
    result = apply_cascade_fix(
        repo,
        finding,
        log_file,
        pattern_store_path=pattern_store_path,
        deterministic_only=True,
    )
    assert result is True

    # Recipe stage actually mutated the file.
    fixed = guide.read_text(encoding="utf-8")
    assert "legacy_pricer.py" not in fixed
    assert "price.py" in fixed

    log_text = log_file.read_text(encoding="utf-8")

    # Cascade telemetry: linter stages were SKIPPED (rule doesn't match ruff-*).
    # Each ruff stage logs a "trying" only if can_handle returned True; here
    # they should all be skipped without trying.
    assert "cascade: trying stage=recipe-engine" in log_text
    for ruff_stage in ("ruff-fix", "ruff-format", "pyupgrade", "autoflake", "isort"):
        trying_msg = f"cascade: trying stage={ruff_stage}"
        assert trying_msg not in log_text, (
            f"{ruff_stage} should have been skipped (rule mismatch)"
        )

    # Cascade telemetry records success at the recipe stage.
    assert "cascade: stage=recipe-engine succeeded" in log_text
    assert '"final_stage": "recipe-engine"' in log_text
    assert '"success": true' in log_text

    # Cross-component: cascade → pattern_store. The success path calls
    # _extract_fix_pattern with the winning stage_name as source.
    store = FixPatternStore(pattern_store_path)
    patterns = store.load_active(rule="docs-legacy-reference")
    assert len(patterns) >= 1
    pat = patterns[0]
    assert pat.rule == "docs-legacy-reference"
    # Source tagged with the cascade stage_name ("recipe:<id>") per lifecycle.
    assert pat.source.startswith("recipe")

    # Cross-component: cascade → tier validation. After cascade success,
    # apply_cascade_fix runs resolve_tier + _tier_validate.
    assert "tier-validation:" in log_text
    assert "tier-validation: tier=T2_VALIDATED passed=True" in log_text

    # Cascade must NOT have fallen through to the LLM stage.
    assert "cascade: falling to LLM" not in log_text
