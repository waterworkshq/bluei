"""Tests for TS/Go/Rust transform packs — loadability and structure."""

from bluei.engine.ast_engine.transforms.typescript_transforms import (
    TS_TRANSFORMS,
    get_transforms as get_ts,
)
from bluei.engine.ast_engine.transforms.go_transforms import (
    GO_TRANSFORMS,
    get_transforms as get_go,
)
from bluei.engine.ast_engine.transforms.rust_transforms import (
    RUST_TRANSFORMS,
    get_transforms as get_rust,
)
from bluei.engine.ast_engine.models import ASTTransform


class TestTypeScriptTransforms:
    def test_get_transforms_returns_list(self):
        transforms = get_ts()
        assert isinstance(transforms, list)
        assert len(transforms) > 0

    def test_all_transforms_are_ast_transform(self):
        for t in TS_TRANSFORMS:
            assert isinstance(t, ASTTransform)

    def test_all_have_source_pattern_ids(self):
        for t in TS_TRANSFORMS:
            assert t.source_pattern_id, f"Transform {t.id} missing source_pattern_id"

    def test_all_have_transform_type(self):
        for t in TS_TRANSFORMS:
            assert t.transform_type, f"Transform {t.id} missing transform_type"

    def test_covers_known_ts_patterns(self):
        """Every TS pattern should have a corresponding transform."""
        pattern_ids = {t.source_pattern_id for t in TS_TRANSFORMS}
        expected = {
            "type-explicit-any",
            "debug-console-log",
            "ts-unsafe-any-cast",
            "type-missing-return",
            "xo-no-warning-comments",
            "ts-unhandled-promise",
        }
        assert expected.issubset(pattern_ids), (
            f"Missing transforms for patterns: {expected - pattern_ids}"
        )


class TestGoTransforms:
    def test_get_transforms_returns_list(self):
        from bluei.engine.ast_engine.transforms.go_transforms import get_transforms

        transforms = get_transforms()
        assert isinstance(transforms, list)
        assert len(transforms) > 0

    def test_all_transforms_are_ast_transform(self):
        for t in GO_TRANSFORMS:
            assert isinstance(t, ASTTransform)

    def test_covers_known_go_patterns(self):
        pattern_ids = {t.source_pattern_id for t in GO_TRANSFORMS}
        expected = {"go-unchecked-error", "go-empty-interface", "go-goroutine-unsafe"}
        assert expected.issubset(pattern_ids), (
            f"Missing transforms for patterns: {expected - pattern_ids}"
        )


class TestRustTransforms:
    def test_get_transforms_returns_list(self):
        from bluei.engine.ast_engine.transforms.rust_transforms import get_transforms

        transforms = get_transforms()
        assert isinstance(transforms, list)
        assert len(transforms) > 0

    def test_all_transforms_are_ast_transform(self):
        for t in RUST_TRANSFORMS:
            assert isinstance(t, ASTTransform)

    def test_covers_known_rust_patterns(self):
        pattern_ids = {t.source_pattern_id for t in RUST_TRANSFORMS}
        expected = {"rust-unwrap-panic", "rust-clone-unnecessary", "rust-expect-vague"}
        assert expected.issubset(pattern_ids), (
            f"Missing transforms for patterns: {expected - pattern_ids}"
        )


class TestPackageExports:
    def test_transforms_package_exports_all(self):
        """The transforms __init__ should re-export all language packs."""
        from bluei.engine.ast_engine.transforms import (
            PYTHON_TRANSFORMS,
            TS_TRANSFORMS as PKG_TS,
            GO_TRANSFORMS as PKG_GO,
            RUST_TRANSFORMS as PKG_RUST,
            get_python_transforms,
            get_typescript_transforms,
            get_go_transforms,
            get_rust_transforms,
        )

        assert len(PYTHON_TRANSFORMS) > 0
        assert len(PKG_TS) > 0
        assert len(PKG_GO) > 0
        assert len(PKG_RUST) > 0
        assert callable(get_python_transforms)
        assert callable(get_typescript_transforms)
        assert callable(get_go_transforms)
        assert callable(get_rust_transforms)

    def test_ast_engine_init_exports_all(self):
        """The ast_engine __init__ should expose all transform getters."""
        from bluei.engine.ast_engine import (
            get_python_transforms,
            get_typescript_transforms,
            get_go_transforms,
            get_rust_transforms,
            PYTHON_TRANSFORMS,
            TS_TRANSFORMS,
            GO_TRANSFORMS,
            RUST_TRANSFORMS,
        )

        assert len(PYTHON_TRANSFORMS) > 0
        assert len(TS_TRANSFORMS) > 0
        assert len(GO_TRANSFORMS) > 0
        assert len(RUST_TRANSFORMS) > 0
