"""Python AST transform definitions — dicts consumed by transformer."""

from __future__ import annotations

from typing import List

from ..models import ASTTransform



def get_transforms() -> List[ASTTransform]:
    return PYTHON_TRANSFORMS


PYTHON_TRANSFORMS: List[ASTTransform] = [
    ASTTransform(
        id="fix-perf-pop-front-loop",
        source_pattern_id="perf-pop-front-loop",
        transform_type="prepend_method",
        params={},
    ),
    ASTTransform(
        id="fix-perf-list-membership-loop",
        source_pattern_id="perf-list-membership-loop",
        transform_type="wrap_set",
        params={},
    ),
    ASTTransform(
        id="fix-notifications-email-no-trim",
        source_pattern_id="notifications-email-no-trim",
        transform_type="prepend_method",
        params={"method": "strip"},
    ),
    ASTTransform(
        id="fix-notifications-type-guard-missing",
        source_pattern_id="notifications-type-guard-missing",
        transform_type="insert_guard",
        params={"variable": "value", "check_type": "str", "message": "Expected str"},
    ),
    ASTTransform(
        id="fix-hardcoded-tmp-path",
        source_pattern_id="hardcoded-tmp-path",
        transform_type="replace_path",
        params={"old_prefix": "/tmp/", "new_prefix": "/var/lib/"},
    ),
    ASTTransform(
        id="fix-catalog-query-not-normalized",
        source_pattern_id="catalog-query-not-normalized",
        transform_type="wrap_normalize",
        params={"methods": ["strip", "lower"]},
    ),
    ASTTransform(
        id="fix-discount-math-sign",
        source_pattern_id="discount-math-sign",
        transform_type="replace_op",
        params={"old_op": "Add", "new_op": "Sub"},
    ),
    ASTTransform(
        id="fix-broad-except",
        source_pattern_id="broad-except",
        transform_type="replace_op",
        params={},
    ),
]
