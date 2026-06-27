"""Coverage-focused tests for bluei.engine.lifecycle.

Targets specific uncovered branches identified by ``--cov-report=term-missing``.
Each test class corresponds to a logical gap area: pattern sharing config, fix
pattern extraction (publish / promote / error paths), Claude fix failure &
cleanup paths, the Mnemo client cache helper, deterministic autofix branches
(perf rules, xo-max-lines routing, tier-validate failures), cascade fallback
paths (store/library init failure, tier-validate failure), and the refactor
queue's validation/exception branches.

Tests follow the existing AAA pattern + ``unittest.mock.patch`` style used in
``tests/test_lifecycle.py`` and ``tests/test_lifecycle_helpers.py``.
"""

from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from bluei.engine.fix_tiers import FixTier
from bluei.engine.governance import PromotionResult
from bluei.engine.lifecycle import (
    ClaudeFixRequest,
    _extract_fix_pattern,
    _get_mnemo_client,
    _get_or_create_shared_library,
    _get_or_create_store,
    _is_pattern_sharing_enabled,
    _reset_lifecycle_cache,
    apply_autofix,
    apply_cascade_fix,
    apply_claude_fix,
    process_refactor_queue,
)
from bluei.engine.models import Finding
from bluei.engine.reforge import RefactorClass, RefactorPhase, RefactorWork


# ── _is_pattern_sharing_enabled ─────────────────────────────────────────


class TestIsPatternSharingEnabled:
    """Cover config.yaml read / parse / exception paths."""

    def test_returns_true_when_config_missing(self, tmp_path):
        # No config.yaml in workspace → default to enabled.
        assert _is_pattern_sharing_enabled(tmp_path) is True

    def test_returns_true_when_explicitly_enabled(self, tmp_path):
        (tmp_path / "config.yaml").write_text("pattern_sharing:\n  enabled: true\n")
        assert _is_pattern_sharing_enabled(tmp_path) is True

    def test_returns_false_when_disabled(self, tmp_path):
        (tmp_path / "config.yaml").write_text("pattern_sharing:\n  enabled: false\n")
        assert _is_pattern_sharing_enabled(tmp_path) is False

    def test_returns_true_when_key_missing(self, tmp_path):
        # pattern_sharing section absent → defaults to True via ``.get(..., True)``.
        (tmp_path / "config.yaml").write_text("other:\n  foo: bar\n")
        assert _is_pattern_sharing_enabled(tmp_path) is True

    def test_returns_true_on_malformed_yaml(self, tmp_path):
        # Malformed YAML raises during safe_load → exception path returns True.
        (tmp_path / "config.yaml").write_text("foo: [unclosed")
        assert _is_pattern_sharing_enabled(tmp_path) is True


# ── _extract_fix_pattern ─────────────────────────────────────────────────


class TestExtractFixPattern:
    """Cover detected_frameworks threading, publish/promote, and error paths."""

    def test_returns_early_when_pattern_store_is_none(self, tmp_path, make_finding):
        log = tmp_path / "log.txt"
        log.write_text("")
        # Should be a no-op; no log line written, no exceptions raised.
        _extract_fix_pattern(tmp_path, make_finding(), "autofix", None, log)
        assert log.read_text() == ""

    @patch("bluei.engine.lifecycle.FixPatternStore", create=True)
    def test_threads_detected_frameworks_to_extract(
        self, _mock_store_cls, tmp_path, make_finding
    ):
        """detected_frameworks is only threaded when populated (line 138)."""
        log = tmp_path / "log.txt"
        log.write_text("")
        store_path = tmp_path / "patterns.jsonl"

        with (
            patch("bluei.engine.pattern_extractor.extract") as mock_extract,
            patch("bluei.engine.pattern_store.FixPatternStore"),
        ):
            mock_extract.return_value = None  # no pattern extracted
            _extract_fix_pattern(
                tmp_path,
                make_finding(),
                "autofix",
                store_path,
                log,
                detected_frameworks=["flask", "pytest"],
            )
            assert mock_extract.call_count == 1
            assert mock_extract.call_args.kwargs["detected_frameworks"] == [
                "flask",
                "pytest",
            ]

    def test_logs_no_pattern_when_extract_returns_none(self, tmp_path, make_finding):
        log = tmp_path / "log.txt"
        log.write_text("")
        store_path = tmp_path / "patterns.jsonl"

        with (
            patch("bluei.engine.pattern_extractor.extract", return_value=None),
            patch("bluei.engine.pattern_store.FixPatternStore"),
        ):
            _extract_fix_pattern(
                tmp_path,
                make_finding(finding_id="abc"),
                "claude",
                store_path,
                log,
            )
        assert "result=no-pattern" in log.read_text()
        assert "finding_id=abc" in log.read_text()

    def test_publishes_high_confidence_pattern_to_shared_library(
        self, tmp_path, make_finding
    ):
        """Cover lines 160-202: confidence >= 0.8 triggers publish + promotion."""
        # Workspace layout: tmp_path / "state" / "fix_patterns.jsonl"
        # _extract_fix_pattern derives workspace = pattern_store_path.parent.parent
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        store_path = state_dir / "fix_patterns.jsonl"
        log = tmp_path / "log.txt"
        log.write_text("")

        # Build a real FixPattern-like object with confidence >= 0.8.
        stored_pattern = MagicMock()
        stored_pattern.confidence = 0.9
        stored_pattern.rule = "ruff-c408"
        stored_pattern.language = "python"
        stored_pattern.before_snippet = "before"
        stored_pattern.after_snippet = "after"
        stored_pattern.structural_hash = "abc123"
        stored_pattern.success_count = 3
        stored_pattern.failure_count = 0

        mock_store = MagicMock()
        mock_store.get_pattern.return_value = stored_pattern

        # Promotion path: lib._get_cached returns a cross-pattern that is
        # eligible → promote_pattern_to_recipe is invoked.
        mock_cross = MagicMock()
        mock_lib = MagicMock()
        mock_lib._get_cached.return_value = {"ruff-c408::abc123": mock_cross}

        with (
            patch("bluei.engine.pattern_extractor.extract", return_value="fp-1"),
            patch(
                "bluei.engine.pattern_store.FixPatternStore", return_value=mock_store
            ),
            patch(
                "bluei.engine.lifecycle._is_pattern_sharing_enabled",
                return_value=True,
            ),
            patch(
                "bluei.engine.shared_pattern_library.SharedPatternLibrary",
                return_value=mock_lib,
            ),
            patch(
                "bluei.engine.shared_pattern_library.check_promotion_eligibility",
                return_value=True,
            ),
            patch(
                "bluei.engine.shared_pattern_library.promote_pattern_to_recipe",
                return_value=PromotionResult.PROMOTED,
            ) as mock_promote,
            patch(
                "bluei.engine.recipe_engine.staged_recipe_dir",
                return_value=tmp_path / "staged",
            ),
            patch("bluei.engine.lifecycle._get_recipe_engine") as mock_engine,
        ):
            _extract_fix_pattern(
                tmp_path,
                make_finding(),
                "claude",
                store_path,
                log,
            )
            mock_lib.publish.assert_called_once()
            mock_promote.assert_called_once()
            mock_engine.return_value.reload.assert_called_once()

    def test_skips_publish_when_confidence_below_threshold(
        self, tmp_path, make_finding
    ):
        """Low-confidence patterns are stored but not published to the shared library."""
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        store_path = state_dir / "fix_patterns.jsonl"
        log = tmp_path / "log.txt"
        log.write_text("")

        stored_pattern = MagicMock()
        stored_pattern.confidence = 0.5  # below 0.8 threshold

        mock_store = MagicMock()
        mock_store.get_pattern.return_value = stored_pattern

        mock_lib = MagicMock()

        with (
            patch("bluei.engine.pattern_extractor.extract", return_value="fp-low"),
            patch(
                "bluei.engine.pattern_store.FixPatternStore", return_value=mock_store
            ),
            patch(
                "bluei.engine.lifecycle._is_pattern_sharing_enabled",
                return_value=True,
            ),
            patch(
                "bluei.engine.shared_pattern_library.SharedPatternLibrary",
                return_value=mock_lib,
            ),
        ):
            _extract_fix_pattern(
                tmp_path,
                make_finding(),
                "claude",
                store_path,
                log,
            )
            # Library IS constructed, but publish() is gated on confidence >= 0.8.
            mock_lib.publish.assert_not_called()

    def test_handles_extract_exception_and_logs_error(self, tmp_path, make_finding):
        """Outer except (line 208) writes error line to log."""
        log = tmp_path / "log.txt"
        log.write_text("")
        store_path = tmp_path / "patterns.jsonl"

        with (
            patch(
                "bluei.engine.pattern_extractor.extract",
                side_effect=RuntimeError("boom"),
            ),
            patch("bluei.engine.pattern_store.FixPatternStore"),
        ):
            _extract_fix_pattern(
                tmp_path,
                make_finding(finding_id="fx1"),
                "recipe",
                store_path,
                log,
            )
        assert "error=RuntimeError" in log.read_text()
        assert "finding_id=fx1" in log.read_text()

    def test_inner_log_exception_is_silent(self, tmp_path, make_finding, caplog):
        """If both extract AND log writing fail, the inner except logs to logger.

        Forces the error-path's ``_append_text`` to fail (line 215-216).
        """
        store_path = tmp_path / "patterns.jsonl"

        with (
            patch(
                "bluei.engine.pattern_extractor.extract",
                side_effect=RuntimeError("extract boom"),
            ),
            patch("bluei.engine.pattern_store.FixPatternStore"),
            patch(
                "bluei.engine.lifecycle._append_text",
                side_effect=OSError("disk full"),
            ),
        ):
            # Should not raise — inner except swallows the logging failure.
            _extract_fix_pattern(
                tmp_path,
                make_finding(),
                "recipe",
                store_path,
                tmp_path / "log.txt",
            )

    def test_recipe_reload_failure_after_promotion_is_swallowed(
        self, tmp_path, make_finding
    ):
        """Lines 195-198: recipe engine reload fails after successful promotion.

        The innermost except catches it and logs a debug message — no raise.
        """
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        store_path = state_dir / "fix_patterns.jsonl"
        log = tmp_path / "log.txt"
        log.write_text("")

        stored_pattern = MagicMock()
        stored_pattern.confidence = 0.9
        stored_pattern.rule = "ruff-c408"
        stored_pattern.language = "python"
        stored_pattern.structural_hash = "abc123"

        mock_store = MagicMock()
        mock_store.get_pattern.return_value = stored_pattern

        mock_cross = MagicMock()
        mock_lib = MagicMock()
        mock_lib._get_cached.return_value = {"ruff-c408::abc123": mock_cross}

        mock_engine = MagicMock()
        mock_engine.reload.side_effect = RuntimeError("reload failed")

        with (
            patch("bluei.engine.pattern_extractor.extract", return_value="fp-1"),
            patch(
                "bluei.engine.pattern_store.FixPatternStore", return_value=mock_store
            ),
            patch(
                "bluei.engine.lifecycle._is_pattern_sharing_enabled",
                return_value=True,
            ),
            patch(
                "bluei.engine.shared_pattern_library.SharedPatternLibrary",
                return_value=mock_lib,
            ),
            patch(
                "bluei.engine.shared_pattern_library.check_promotion_eligibility",
                return_value=True,
            ),
            patch(
                "bluei.engine.shared_pattern_library.promote_pattern_to_recipe",
                return_value=PromotionResult.PROMOTED,
            ),
            patch(
                "bluei.engine.recipe_engine.staged_recipe_dir",
                return_value=tmp_path / "staged",
            ),
            patch(
                "bluei.engine.lifecycle._get_recipe_engine", return_value=mock_engine
            ),
        ):
            # Must not raise despite reload failure.
            _extract_fix_pattern(
                tmp_path,
                make_finding(),
                "claude",
                store_path,
                log,
            )
            mock_engine.reload.assert_called_once()

    def test_promotion_failure_is_swallowed(self, tmp_path, make_finding):
        """Lines 199-200: promotion logic itself fails → outer except logs.

        ``promote_pattern_to_recipe`` raises; the middle except catches it.
        """
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        store_path = state_dir / "fix_patterns.jsonl"
        log = tmp_path / "log.txt"
        log.write_text("")

        stored_pattern = MagicMock()
        stored_pattern.confidence = 0.9
        stored_pattern.rule = "ruff-c408"
        stored_pattern.language = "python"
        stored_pattern.structural_hash = "abc123"

        mock_store = MagicMock()
        mock_store.get_pattern.return_value = stored_pattern

        mock_cross = MagicMock()
        mock_lib = MagicMock()
        # Key must match "{rule}::{structural_hash}" exactly.
        mock_lib._get_cached.return_value = {"ruff-c408::abc123": mock_cross}

        with (
            patch("bluei.engine.pattern_extractor.extract", return_value="fp-1"),
            patch(
                "bluei.engine.pattern_store.FixPatternStore", return_value=mock_store
            ),
            patch(
                "bluei.engine.lifecycle._is_pattern_sharing_enabled",
                return_value=True,
            ),
            patch(
                "bluei.engine.shared_pattern_library.SharedPatternLibrary",
                return_value=mock_lib,
            ),
            patch(
                "bluei.engine.shared_pattern_library.check_promotion_eligibility",
                return_value=True,
            ),
            patch(
                "bluei.engine.shared_pattern_library.promote_pattern_to_recipe",
                side_effect=RuntimeError("promotion write failed"),
            ),
            patch(
                "bluei.engine.recipe_engine.staged_recipe_dir",
                return_value=tmp_path / "staged",
            ),
        ):
            # Must not raise despite promotion failure.
            _extract_fix_pattern(
                tmp_path,
                make_finding(),
                "claude",
                store_path,
                log,
            )

    def test_publish_failure_is_swallowed(self, tmp_path, make_finding):
        """Lines 201-202: shared-library publish fails → outermost except logs.

        ``lib.publish`` raises; the outermost try/except (line 152/201) catches it.
        """
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        store_path = state_dir / "fix_patterns.jsonl"
        log = tmp_path / "log.txt"
        log.write_text("")

        stored_pattern = MagicMock()
        stored_pattern.confidence = 0.9

        mock_store = MagicMock()
        mock_store.get_pattern.return_value = stored_pattern

        mock_lib = MagicMock()
        mock_lib.publish.side_effect = RuntimeError("shared store write failed")

        with (
            patch("bluei.engine.pattern_extractor.extract", return_value="fp-1"),
            patch(
                "bluei.engine.pattern_store.FixPatternStore", return_value=mock_store
            ),
            patch(
                "bluei.engine.lifecycle._is_pattern_sharing_enabled",
                return_value=True,
            ),
            patch(
                "bluei.engine.shared_pattern_library.SharedPatternLibrary",
                return_value=mock_lib,
            ),
        ):
            # Must not raise despite publish failure.
            _extract_fix_pattern(
                tmp_path,
                make_finding(),
                "claude",
                store_path,
                log,
            )


# ── apply_claude_fix — failure paths ─────────────────────────────────────


class TestApplyClaudeFixFailurePaths:
    """Cover the lessons-on-failure append (339-340) and unlink-failure (360-361)."""

    @patch("bluei.engine.lifecycle.subprocess.run")
    @patch("bluei.engine.lifecycle.append_lesson")
    @patch("bluei.engine.lifecycle.load_lessons_for_finding", return_value=[])
    @patch("bluei.engine.lifecycle.load_lessons_for_rule", return_value=[])
    @patch("bluei.engine.lifecycle.load_failure_clusters_for_rule", return_value="")
    def test_failure_with_lessons_file_appends_what_broke(
        self,
        mock_cl,
        mock_rl,
        mock_lf,
        mock_lesson,
        mock_run,
        tmp_path,
        make_finding,
    ):
        """When Claude returns nonzero and lessons_file is set, the failure
        branch (lines 339-340) writes a ``what_broke`` lesson entry."""
        mock_run.return_value = MagicMock(returncode=1, stdout="some error\nmore")
        lessons = tmp_path / "LESSONS_LOG.md"
        lessons.write_text("")
        log = tmp_path / "log.txt"
        log.write_text("")

        rc, _output, _ = apply_claude_fix(
            ClaudeFixRequest(
                worktree_path=tmp_path,
                finding=make_finding(),
                baseline_checks={},
                target_checks={},
                claude_cmd_template="echo {prompt_file}",
                max_files_changed=5,
                max_loc_diff=200,
                log_file=log,
                lessons_file=lessons,
            ),
        )
        assert rc == 1
        mock_lesson.assert_called_once()
        # The failure path uses ``what_broke`` keyword.
        assert "what_broke" in mock_lesson.call_args.kwargs

    @patch("bluei.engine.lifecycle.subprocess.run")
    @patch("bluei.engine.lifecycle.Path.unlink", side_effect=OSError("denied"))
    def test_unlink_failure_in_finally_is_swallowed(
        self, mock_unlink, mock_run, tmp_path, make_finding, caplog
    ):
        """Line 360-361: failed prompt cleanup logs a debug message but does not raise."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        log = tmp_path / "log.txt"
        log.write_text("")

        # Must not raise despite unlink failure.
        rc, _output, _ = apply_claude_fix(
            ClaudeFixRequest(
                worktree_path=tmp_path,
                finding=make_finding(),
                baseline_checks={},
                target_checks={},
                claude_cmd_template="echo {prompt_file}",
                max_files_changed=5,
                max_loc_diff=200,
                log_file=log,
            ),
        )
        assert rc == 0


# ── _get_mnemo_client ────────────────────────────────────────────────────


class TestGetMnemoClient:
    """Cover the mnemo client cache helper (lines 410-422)."""

    def setup_method(self):
        _reset_lifecycle_cache()

    def teardown_method(self):
        _reset_lifecycle_cache()

    def test_returns_cached_client(self, tmp_path):
        from bluei.engine.lifecycle import _mnemo_clients

        fake_client = MagicMock()
        _mnemo_clients[str(tmp_path.resolve())] = fake_client
        result = _get_mnemo_client(tmp_path)
        assert result is fake_client

    @patch("bluei.engine.lifecycle.MnemoClient")
    def test_constructs_and_caches_new_client(self, mock_client_cls, tmp_path):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        result = _get_mnemo_client(tmp_path)
        assert result is mock_client
        mock_client_cls.assert_called_once_with(tmp_path)

        # Second call must hit the cache (no new construction).
        result2 = _get_mnemo_client(tmp_path)
        assert result2 is mock_client
        assert mock_client_cls.call_count == 1

    @patch(
        "bluei.engine.lifecycle.MnemoClient",
        side_effect=RuntimeError("mnemo unavailable"),
    )
    def test_returns_none_on_construction_failure(self, _mock, tmp_path):
        log = tmp_path / "log.txt"
        log.write_text("")
        result = _get_mnemo_client(tmp_path, log_file=log)
        assert result is None
        assert "mnemo-init: unavailable" in log.read_text()

    @patch(
        "bluei.engine.lifecycle.MnemoClient",
        side_effect=RuntimeError("mnemo unavailable"),
    )
    def test_returns_none_without_log_file(self, _mock, tmp_path):
        # Same failure path but log_file=None — should not raise.
        result = _get_mnemo_client(tmp_path, log_file=None)
        assert result is None


# ── apply_autofix — branches ─────────────────────────────────────────────


class TestApplyAutofixBranches:
    """Cover recipe tier-validate failure, perf-list-membership, xo-max-lines,
    and legacy-code tier-validate failure branches."""

    @patch(
        "bluei.engine.lifecycle.classify_finding",
        return_value=RefactorClass.SIMPLE_FIX,
    )
    @patch("bluei.engine.lifecycle._recipe_engine")
    def test_recipe_success_but_tier_validation_fails(
        self, mock_re, _mock_classify, tmp_path, make_finding
    ):
        """Line 595: recipe applies cleanly, but _tier_validate returns False."""
        mock_re.match.return_value = MagicMock(id="rcp")
        mock_re.apply.return_value = MagicMock(success=True)
        (tmp_path / "src.py").mkdir(parents=True)
        (tmp_path / "src.py" / "app.py").write_text("x = 1")
        log = tmp_path / "log.txt"
        log.write_text("")

        with (
            patch(
                "bluei.engine.lifecycle.resolve_tier",
                return_value=FixTier.T0_GUARANTEED,
            ),
            patch("bluei.engine.lifecycle._tier_validate", return_value=False),
            patch("bluei.engine.lifecycle._extract_fix_pattern"),
        ):
            assert (
                apply_autofix(tmp_path, make_finding(path="src.py/app.py"), log)
                is False
            )

    @patch(
        "bluei.engine.lifecycle.classify_finding",
        return_value=RefactorClass.SIMPLE_FIX,
    )
    @patch("bluei.engine.lifecycle._recipe_engine")
    def test_perf_list_membership_loop_converts_to_set(
        self, mock_re, _mock_classify, tmp_path, make_finding
    ):
        """Lines 622-635: perf-list-membership-loop converts ``x in list`` to set."""
        mock_re.match.return_value = None
        src = tmp_path / "src.py"
        src.mkdir()
        original = (
            "items = [1, 2, 3]\n"
            "for x in range(10):\n"
            "    if x in items:\n"
            "        print(x)\n"
        )
        (src / "app.py").write_text(original)
        log = tmp_path / "log.txt"
        log.write_text("")

        with (
            patch("bluei.engine.lifecycle._tier_validate", return_value=True),
            patch(
                "bluei.engine.lifecycle.resolve_tier",
                return_value=FixTier.T0_GUARANTEED,
            ),
            patch("bluei.engine.lifecycle._extract_fix_pattern"),
        ):
            apply_autofix(
                tmp_path,
                make_finding(path="src.py/app.py", rule="perf-list-membership-loop"),
                log,
            )
        content = (src / "app.py").read_text()
        assert "items_set" in content
        assert "in items_set" in content

    @patch(
        "bluei.engine.lifecycle.classify_finding",
        return_value=RefactorClass.SIMPLE_FIX,
    )
    @patch("bluei.engine.lifecycle._recipe_engine")
    def test_xo_max_lines_too_large_skips_autofix(
        self, mock_re, _mock_classify, tmp_path, make_finding
    ):
        """Lines 642-649: xo-max-lines on a file above MAX_LINES_REFACTOR_LIMIT
        logs ``too large`` and returns False without applying."""
        from bluei.engine.constants import MAX_LINES_REFACTOR_LIMIT

        mock_re.match.return_value = None
        src = tmp_path / "src.py"
        src.mkdir()
        # File just over the limit.
        (src / "big.py").write_text("\n" * (MAX_LINES_REFACTOR_LIMIT + 5))
        log = tmp_path / "log.txt"
        log.write_text("")

        assert (
            apply_autofix(
                tmp_path,
                make_finding(path="src.py/big.py", rule="xo-max-lines"),
                log,
            )
            is False
        )
        assert "too large" in log.read_text()

    @patch(
        "bluei.engine.lifecycle.classify_finding",
        return_value=RefactorClass.SIMPLE_FIX,
    )
    @patch("bluei.engine.lifecycle._recipe_engine")
    def test_xo_max_lines_eligible_routes_to_claude(
        self, mock_re, _mock_classify, tmp_path, make_finding
    ):
        """Lines 650-655: xo-max-lines on an eligible file logs ``eligible`` and
        returns False (Claude engine is the caller's responsibility)."""
        mock_re.match.return_value = None
        src = tmp_path / "src.py"
        src.mkdir()
        # File well under the limit but flagged for refactor.
        (src / "small.py").write_text("x = 1\n" * 50)
        log = tmp_path / "log.txt"
        log.write_text("")

        assert (
            apply_autofix(
                tmp_path,
                make_finding(path="src.py/small.py", rule="xo-max-lines"),
                log,
            )
            is False
        )
        assert "eligible" in log.read_text()

    @patch(
        "bluei.engine.lifecycle.classify_finding",
        return_value=RefactorClass.SIMPLE_FIX,
    )
    @patch("bluei.engine.lifecycle._recipe_engine")
    def test_legacy_path_tier_validate_failure_returns_false(
        self, mock_re, _mock_classify, tmp_path, make_finding
    ):
        """Line 679: deterministic (legacy) edit applied, but _tier_validate
        returns False → apply_autofix returns False."""
        mock_re.match.return_value = None
        src = tmp_path / "src.py"
        src.mkdir()
        (src / "app.py").write_text(
            "import os\n\ndata = [1, 2, 3]\nwhile data:\n    x = data.pop(0)\n"
        )
        log = tmp_path / "log.txt"
        log.write_text("")

        with (
            patch("bluei.engine.lifecycle._tier_validate", return_value=False),
            patch(
                "bluei.engine.lifecycle.resolve_tier",
                return_value=FixTier.T0_GUARANTEED,
            ),
            patch("bluei.engine.lifecycle._extract_fix_pattern"),
        ):
            assert (
                apply_autofix(
                    tmp_path,
                    make_finding(path="src.py/app.py", rule="perf-pop-front-loop"),
                    log,
                )
                is False
            )


# ── _get_or_create_store / _get_or_create_shared_library ────────────────


class TestStoreAndLibraryCaches:
    """Cover cache-miss lines 687 and 699."""

    def setup_method(self):
        _reset_lifecycle_cache()

    def teardown_method(self):
        _reset_lifecycle_cache()

    def test_get_or_create_store_caches_real_store(self, tmp_path):
        """Line 687: cache miss constructs a FixPatternStore and caches it."""
        store_path = tmp_path / "patterns.jsonl"
        store1 = _get_or_create_store(store_path)
        assert store1 is not None
        # Second call returns the cached instance.
        store2 = _get_or_create_store(store_path)
        assert store1 is store2

    def test_get_or_create_shared_library_caches_real_library(self, tmp_path):
        """Line 699: cache miss constructs a SharedPatternLibrary and caches it."""
        lib1 = _get_or_create_shared_library(tmp_path)
        assert lib1 is not None
        # Cache hit returns the same instance.
        lib2 = _get_or_create_shared_library(tmp_path)
        assert lib1 is lib2
        # Library created its state dir.
        assert (tmp_path / "state" / "shared_patterns").exists()


# ── apply_cascade_fix — fallback error branches ─────────────────────────


class TestApplyCascadeFixErrorPaths:
    """Cover pattern-store / shared-library init failures (737-739, 749-751)
    and tier-validate failure on cascade success (784)."""

    @patch("bluei.engine.lifecycle._tier_validate", return_value=False)
    @patch("bluei.engine.lifecycle._extract_fix_pattern")
    @patch("bluei.engine.lifecycle._is_pattern_sharing_enabled", return_value=False)
    @patch("bluei.engine.cascade.DeterministicCascade")
    @patch("bluei.engine.cascade.CascadeContext")
    @patch("bluei.engine.cascade.default_cascade_stages", return_value=[])
    def test_tier_validate_failure_on_cascade_success(
        self,
        mock_stages,
        mock_ctx_cls,
        mock_cascade_cls,
        mock_share,
        mock_ext,
        mock_tier,
        tmp_path,
        make_finding,
    ):
        """Line 784: cascade succeeds but tier validation fails → return False."""
        mock_ctx_cls.return_value = MagicMock(allow_llm=False)
        cascade = MagicMock()
        cascade.execute.return_value = MagicMock(success=True, stage_name="recipe")
        mock_cascade_cls.return_value = cascade
        log = tmp_path / "log.txt"
        log.write_text("")
        assert apply_cascade_fix(tmp_path, make_finding(), log) is False

    @patch("bluei.engine.lifecycle._is_pattern_sharing_enabled", return_value=False)
    @patch(
        "bluei.engine.lifecycle._get_or_create_store",
        side_effect=RuntimeError("store init failed"),
    )
    @patch("bluei.engine.cascade.DeterministicCascade")
    @patch("bluei.engine.cascade.CascadeContext")
    @patch("bluei.engine.cascade.default_cascade_stages", return_value=[])
    def test_pattern_store_creation_failure_swallowed(
        self,
        mock_stages,
        mock_ctx_cls,
        mock_cascade_cls,
        mock_store,
        mock_share,
        tmp_path,
        make_finding,
    ):
        """Lines 737-739: pattern store init failure is logged & swallowed."""
        mock_ctx_cls.return_value = MagicMock(allow_llm=False)
        cascade = MagicMock()
        cascade.execute.return_value = MagicMock(success=False, stage_name="recipe")
        mock_cascade_cls.return_value = cascade
        log = tmp_path / "log.txt"
        log.write_text("")
        # Should not raise; cascade.execute is still called with pattern_store=None.
        result = apply_cascade_fix(
            tmp_path,
            make_finding(),
            log,
            pattern_store_path=tmp_path / "p.jsonl",
            deterministic_only=True,
        )
        assert result is False

    @patch(
        "bluei.engine.lifecycle._get_or_create_shared_library",
        side_effect=RuntimeError("library init failed"),
    )
    @patch("bluei.engine.lifecycle._is_pattern_sharing_enabled", return_value=True)
    @patch("bluei.engine.lifecycle._get_or_create_store")
    @patch("bluei.engine.cascade.DeterministicCascade")
    @patch("bluei.engine.cascade.CascadeContext")
    @patch("bluei.engine.cascade.default_cascade_stages", return_value=[])
    def test_shared_library_creation_failure_swallowed(
        self,
        mock_stages,
        mock_ctx_cls,
        mock_cascade_cls,
        mock_store,
        mock_share,
        mock_lib,
        tmp_path,
        make_finding,
    ):
        """Lines 749-751: shared library init failure is logged & swallowed."""
        mock_ctx_cls.return_value = MagicMock(allow_llm=False)
        cascade = MagicMock()
        cascade.execute.return_value = MagicMock(success=False, stage_name="recipe")
        mock_cascade_cls.return_value = cascade
        log = tmp_path / "log.txt"
        log.write_text("")
        result = apply_cascade_fix(
            tmp_path,
            make_finding(),
            log,
            pattern_store_path=tmp_path / "p.jsonl",
            deterministic_only=True,
        )
        assert result is False


# ── process_refactor_queue — validation & exception branches ────────────


def _queue_item(work_id: str = "w-x", rule: str = "xo-complexity"):
    """Build a mock queue item compatible with process_refactor_queue."""
    item = MagicMock()
    item.work_id = work_id
    item.finding_dict = {
        "finding_id": f"f-{work_id}",
        "repo": "r",
        "path": "p",
        "line": 1,
        "rule": rule,
        "snippet": "s",
        "confidence": 0.9,
        "quick_win": False,
        "safe_to_autofix": False,
    }
    return item


class TestProcessRefactorQueueBranches:
    """Cover validation-failure (914-916) and exception (924-928) branches."""

    @patch(
        "bluei.engine.lifecycle.apply_claude_fix",
        return_value=(0, "fixed", "/tmp/p"),
    )
    @patch("bluei.engine.verify.run_validation")
    @patch("bluei.engine.lifecycle.RefactorQueue")
    def test_validation_failure_marks_item_failed(
        self, mock_rq_cls, mock_validate, mock_claude, tmp_path
    ):
        """Lines 914-916: Claude succeeds but validation.passed=False → fail."""
        mock_queue = MagicMock()
        mock_rq_cls.return_value = mock_queue
        item = _queue_item("w-vf")
        mock_queue.list_items.side_effect = [[item], []]

        # Validation returns passed=False with a message.
        mock_result = MagicMock()
        mock_result.passed = False
        mock_result.message = "regression detected"
        mock_validate.return_value = mock_result

        result = process_refactor_queue(tmp_path, tmp_path, dry_run=False)
        assert "w-vf" in result["failed"]
        mock_queue.fail.assert_called_once_with("w-vf", "regression detected")

    @patch(
        "bluei.engine.lifecycle.apply_claude_fix",
        return_value=(0, "fixed", "/tmp/p"),
    )
    @patch("bluei.engine.verify.run_validation")
    @patch("bluei.engine.lifecycle.RefactorQueue")
    def test_validation_failure_with_empty_message_uses_default(
        self, mock_rq_cls, mock_validate, mock_claude, tmp_path
    ):
        """Lines 914-916: empty validation.message falls back to default text."""
        mock_queue = MagicMock()
        mock_rq_cls.return_value = mock_queue
        item = _queue_item("w-vf2")
        mock_queue.list_items.side_effect = [[item], []]

        mock_result = MagicMock()
        mock_result.passed = False
        mock_result.message = ""  # Empty → default fallback.
        mock_validate.return_value = mock_result

        result = process_refactor_queue(tmp_path, tmp_path, dry_run=False)
        assert "w-vf2" in result["failed"]
        mock_queue.fail.assert_called_once_with("w-vf2", "validation failed")

    @patch("bluei.engine.lifecycle.RefactorQueue")
    def test_exception_during_processing_marks_failed(self, mock_rq_cls, tmp_path):
        """Lines 924-928: unexpected exception during processing → fail + record."""
        mock_queue = MagicMock()
        mock_rq_cls.return_value = mock_queue
        item = _queue_item("w-exc")
        mock_queue.list_items.side_effect = [[item], []]
        # start_execution raises an unhandled exception.
        mock_queue.start_execution.side_effect = RuntimeError("db locked")

        result = process_refactor_queue(tmp_path, tmp_path, dry_run=False)
        assert "w-exc" in result["failed"]
        mock_queue.fail.assert_called_once()
        # Error message preserved.
        assert "exception" in mock_queue.fail.call_args[0][1]
