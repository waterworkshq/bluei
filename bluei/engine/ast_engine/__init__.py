"""AST-based pattern matching and transformation subpackage.

Python uses stdlib ast; TS/Go/Rust use tree-sitter via factory functions.
"""

from .matcher import ASTPatternMatcher
from .models import ASTMatch, ASTPattern, ASTTransform, ASTTransformResult
from .patterns.python_patterns import PYTHON_PATTERNS
from .patterns.python_patterns import get_patterns as get_python_patterns
from .transforms.python_transforms import PYTHON_TRANSFORMS
from .transforms.python_transforms import get_transforms as get_python_transforms
from .transforms.typescript_transforms import TS_TRANSFORMS
from .transforms.typescript_transforms import (
    get_transforms as get_typescript_transforms,
)
from .transforms.go_transforms import GO_TRANSFORMS
from .transforms.go_transforms import get_transforms as get_go_transforms
from .transforms.rust_transforms import RUST_TRANSFORMS
from .transforms.rust_transforms import get_transforms as get_rust_transforms


def get_python_matcher():
    return ASTPatternMatcher(get_python_patterns())


def get_ts_matcher():
    from .patterns.ts_patterns import get_patterns
    from .ts_matcher import TSPatternMatcher

    return TSPatternMatcher(get_patterns())


def get_go_matcher():
    from .patterns.go_patterns import get_patterns
    from .ts_matcher import TSPatternMatcher

    return TSPatternMatcher(get_patterns())


def get_rust_matcher():
    from .patterns.rust_patterns import get_patterns
    from .ts_matcher import TSPatternMatcher

    return TSPatternMatcher(get_patterns())
