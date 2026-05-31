"""bluei.engine.constants — all module-level constants, catalogs, and paths."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

CURRENT_FILE = Path(__file__)
AGENT_ROOT = CURRENT_FILE.resolve().parents[2]  # bluei/
WORKSPACE = AGENT_ROOT.parent
RUNNER_PATH = AGENT_ROOT / "bluei" / "engine" / "cli.py"
DEFAULT_REPO = AGENT_ROOT / "repos" / "default"
DEFAULT_STATE = AGENT_ROOT / "sandbox-state.json"
DEFAULT_LOG = AGENT_ROOT / "logs" / "sandbox-local-run.log"
DEFAULT_FINDINGS = AGENT_ROOT / "state" / "qa_findings.jsonl"
DEFAULT_ISSUES = AGENT_ROOT / "state" / "qa_issues.json"
DEFAULT_REFACTOR_QUEUE_DIR = AGENT_ROOT / "state" / "refactor_queue"
DEFAULT_BATCH_RULES_PATH = Path(__file__).resolve().parent / "batch_rules.yaml"
DEFAULT_BATCH_STATE = AGENT_ROOT / "state" / "batches.jsonl"
DEFAULT_WORKTREE_ROOT = AGENT_ROOT / "worktrees"
DEFAULT_STATUS = AGENT_ROOT / "sandbox-trial-status.json"
DEFAULT_DOCS_INDEX = AGENT_ROOT / "state" / "docs_index.json"
DEFAULT_LESSONS_LOG = AGENT_ROOT / "LESSONS_LOG.md"


def bluei_home() -> Path:
    return Path.home() / ".bluei"


def bluei_registry_path() -> Path:
    return bluei_home() / "registry.json"


def _load_bluei_registry() -> Dict[str, Any]:
    p = bluei_registry_path()
    if p.exists():
        import json

        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _logger.debug("Failed to read cached config JSON")
    return {"repos": {}}


def _save_bluei_registry(data: Dict[str, Any]) -> None:
    import json

    p = bluei_registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _cleanup_stale_registry() -> None:
    registry = _load_bluei_registry()
    stale = []
    for resolved_path in registry.get("repos", {}):
        if not Path(resolved_path).exists():
            stale.append(resolved_path)
    if not stale:
        return
    for path in stale:
        del registry["repos"][path]
    _save_bluei_registry(registry)


def _slugify_repo_name(name: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "repo"


def bluei_repo_dir(repo_path: Path) -> Path:
    _cleanup_stale_registry()
    resolved = str(repo_path.resolve())
    registry = _load_bluei_registry()

    if resolved in registry.get("repos", {}):
        slug = registry["repos"][resolved]["slug"]
        return bluei_home() / "repos" / slug

    base_slug = _slugify_repo_name(repo_path.resolve().name)
    existing_slugs = {entry["slug"] for entry in registry.get("repos", {}).values()}

    slug = base_slug
    if slug in existing_slugs:
        n = 1
        while f"{base_slug}-{n}" in existing_slugs:
            n += 1
        slug = f"{base_slug}-{n}"

    registry.setdefault("repos", {})[resolved] = {
        "slug": slug,
        "path": resolved,
        "registered_at": __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat(),
    }
    _save_bluei_registry(registry)

    repo_dir = bluei_home() / "repos" / slug
    repo_dir.mkdir(parents=True, exist_ok=True)
    return repo_dir


def repo_lessons_path(repo_path: Path) -> Path:
    return bluei_repo_dir(repo_path) / "lessons_log.md"


def repo_findings_path(repo_path: Path) -> Path:
    return bluei_repo_dir(repo_path) / "findings.jsonl"


def repo_patterns_path(repo_path: Path) -> Path:
    return bluei_repo_dir(repo_path) / "fix_patterns.jsonl"


def repo_docs_index_path(repo_path: Path) -> Path:
    return bluei_repo_dir(repo_path) / "docs_index.json"


def repo_issues_path(repo_path: Path) -> Path:
    return bluei_repo_dir(repo_path) / "issues.json"


DEFAULT_FIX_ENGINE = "deterministic"
DEFAULT_CLAUDE_CMD_TEMPLATE = (
    'claude --print "Read {prompt_file} and apply minimal fix for finding {finding_id}. '
    'Run relevant tests and exit non-zero on failure."'
)
QA_FIX_PROMPT_FILENAME = ".qa-fix-prompt.md"
DEFAULT_FINDING_COOLDOWN_SECONDS = 4 * 60 * 60
DEFAULT_STALENESS_THRESHOLD_SECONDS = 2 * 60 * 60
MAX_RECONCILIATION_EVENTS = 100

# Detection rules catalog. See docs/RULES_REFERENCE.md for the full reference.
DETECTOR_CATALOG: List[Dict[str, Any]] = [
    {
        "rule": "discount-math-sign",
        "category": "bug",
        "confidence": 0.95,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "catalog-query-not-normalized",
        "category": "bug",
        "confidence": 0.93,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "orders-tax-truncation",
        "category": "bug",
        "confidence": 0.92,
        "autofix": False,
        "language": "python",
    },
    {
        "rule": "notifications-email-no-trim",
        "category": "bug",
        "confidence": 0.89,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "notifications-type-guard-missing",
        "category": "bug",
        "confidence": 0.87,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "inventory-invalid-quantity",
        "category": "bug",
        "confidence": 0.84,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "broad-except",
        "category": "lint",
        "confidence": 0.88,
        "autofix": False,
        "language": "python",
    },
    {
        "rule": "hardcoded-tmp-path",
        "category": "lint",
        "confidence": 0.81,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "trailing-whitespace",
        "category": "lint",
        "confidence": 0.75,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "debt-todo-marker",
        "category": "todo/debt",
        "confidence": 0.72,
        "autofix": False,
        "language": "python",
    },
    {
        "rule": "docs-legacy-reference",
        "category": "docs-mismatch",
        "confidence": 0.91,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "docs-missing-rollback",
        "category": "docs-gap",
        "confidence": 0.86,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "docs-quickstart-gap",
        "category": "docs-gap",
        "confidence": 0.74,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "perf-pop-front-loop",
        "category": "perf-smell",
        "confidence": 0.83,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "perf-list-membership-loop",
        "category": "perf-smell",
        "confidence": 0.82,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "test-gap-missing-file",
        "category": "test-gap",
        "confidence": 0.79,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "test-gap-missing-case",
        "category": "test-gap",
        "confidence": 0.77,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "doc-gap-uncovered-module",
        "category": "docs-gap",
        "confidence": 0.78,
        "autofix": False,
        "language": "python",
    },
    {
        "rule": "doc-drift-stale-reference",
        "category": "docs-drift",
        "confidence": 0.8,
        "autofix": False,
        "language": "python",
    },
    # xo linter rules
    {
        "rule": "xo-max-lines",
        "category": "refactor",
        "confidence": 0.85,
        "autofix": True,
        "language": "typescript",
    },
    {
        "rule": "xo-no-warning-comments",
        "category": "lint",
        "confidence": 0.80,
        "autofix": True,
        "language": "typescript",
    },
    {
        "rule": "xo-complexity",
        "category": "refactor",
        "confidence": 0.82,
        "autofix": True,
        "language": "typescript",
    },
    # Python ruff linter rules
    {
        "rule": "ruff-b007",
        "category": "lint",
        "confidence": 0.75,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "ruff-b904",
        "category": "bug",
        "confidence": 0.80,
        "autofix": False,
        "language": "python",
    },
    {
        "rule": "ruff-e501",
        "category": "style",
        "confidence": 0.65,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "ruff-s311",
        "category": "security",
        "confidence": 0.78,
        "autofix": False,
        "language": "python",
    },
    {
        "rule": "ruff-c408",
        "category": "style",
        "confidence": 0.72,
        "autofix": False,
        "language": "python",
    },
    # Type safety rules (TypeScript/JavaScript)
    {
        "rule": "type-explicit-any",
        "category": "type-safety",
        "confidence": 0.85,
        "autofix": True,
        "language": "javascript",
    },
    {
        "rule": "type-missing-return",
        "category": "type-safety",
        "confidence": 0.80,
        "autofix": True,
        "language": "javascript",
    },
    {
        "rule": "type-missing-param",
        "category": "type-safety",
        "confidence": 0.78,
        "autofix": True,
        "language": "javascript",
    },
    {
        "rule": "type-untyped-import",
        "category": "type-safety",
        "confidence": 0.75,
        "autofix": False,
        "language": "javascript",
    },
    # Test coverage rules
    {
        "rule": "test-coverage-branch",
        "category": "test-coverage",
        "confidence": 0.82,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "test-coverage-function",
        "category": "test-coverage",
        "confidence": 0.80,
        "autofix": True,
        "language": "python",
    },
    {
        "rule": "test-coverage-line",
        "category": "test-coverage",
        "confidence": 0.78,
        "autofix": False,
        "language": "python",
    },
    # AST-detected TypeScript/JavaScript rules
    {
        "rule": "ts-unsafe-any-cast",
        "category": "type-safety",
        "confidence": 0.82,
        "autofix": True,
        "language": "javascript",
    },
    {
        "rule": "ts-unhandled-promise",
        "category": "bug",
        "confidence": 0.78,
        "autofix": False,
        "language": "javascript",
    },
    {
        "rule": "debug-console-log",
        "category": "lint",
        "confidence": 0.70,
        "autofix": False,
        "language": "javascript",
    },
    # AST-detected Go rules
    {
        "rule": "go-unchecked-error",
        "category": "bug",
        "confidence": 0.75,
        "autofix": False,
        "language": "go",
    },
    {
        "rule": "go-empty-interface",
        "category": "lint",
        "confidence": 0.70,
        "autofix": True,
        "language": "go",
    },
    {
        "rule": "go-goroutine-unsafe",
        "category": "bug",
        "confidence": 0.65,
        "autofix": False,
        "language": "go",
    },
    # AST-detected Rust rules
    {
        "rule": "rust-unwrap-panic",
        "category": "bug",
        "confidence": 0.72,
        "autofix": False,
        "language": "rust",
    },
    {
        "rule": "rust-clone-unnecessary",
        "category": "perf-smell",
        "confidence": 0.60,
        "autofix": False,
        "language": "rust",
    },
    {
        "rule": "rust-expect-vague",
        "category": "lint",
        "confidence": 0.70,
        "autofix": False,
        "language": "rust",
    },
]

# Max-lines refactor limits - files larger than this won't be auto-refactored
MAX_LINES_REFACTOR_LIMIT = 3000  # Maximum lines for auto-refactor
MAX_LINES_REFACTOR_TARGET = 1500  # Target lines per file after split

# Rules that require Claude fix engine (complex refactoring, not deterministic)
CLAUDE_REQUIRED_RULES = {
    "xo-max-lines",
    "xo-complexity",
    "test-coverage-branch",
    "test-coverage-function",
    "type-missing-return",
}

BASELINE_VALIDATION_CHECKS: Dict[str, List[str]] = {
    "lint_check_py": ["python3", "lint_check.py"],
    "test_price_py": ["python3", "test_price.py"],
}

RULE_TARGET_CHECKS: Dict[str, Dict[str, List[str]]] = {
    "discount-math-sign": {
        "target_discount_math": ["python3", "test_price.py"],
    },
    "catalog-query-not-normalized": {
        "target_catalog_query": [
            "python3",
            "-c",
            'import sys; sys.path.insert(0, "src"); from qa_sandbox.catalog import find_item; '
            'raise SystemExit(0 if find_item(["USB Cable"], " usb cable ") else 1)',
        ],
    },
    "notifications-email-no-trim": {
        "target_notifications_email": [
            "python3",
            "-c",
            'import sys; sys.path.insert(0, "src"); from qa_sandbox.notifications import normalize_email; '
            'raise SystemExit(0 if normalize_email("  USER@Example.com  ") == "user@example.com" else 1)',
        ],
    },
    "inventory-invalid-quantity": {
        "target_inventory_negative_quantity": [
            "python3",
            "-c",
            'import sys; sys.path.insert(0, "src"); from qa_sandbox.inventory import reserve_stock; '
            'stock={"SKU": 5}; ok = reserve_stock(stock, "SKU", -1) is False and stock["SKU"] == 5; '
            "raise SystemExit(0 if ok else 1)",
        ],
    },
    "hardcoded-tmp-path": {
        "target_hardcoded_tmp_path": [
            "python3",
            "-c",
            'from pathlib import Path; text = Path("scripts/report_health.py").read_text(encoding="utf-8"); '
            "bad = 'Path(\"/tmp/' in text or \"Path('/tmp/\" in text; "
            'ok = (not bad) and ("state.json" in text); raise SystemExit(0 if ok else 1)',
        ],
    },
    "docs-legacy-reference": {
        "target_docs_legacy_reference": [
            "python3",
            "-c",
            'from pathlib import Path; files=["docs/ARCHITECTURE.md","docs/TROUBLESHOOTING.md"]; '
            'ok=all("legacy_pricer.py" not in Path(f).read_text(encoding="utf-8") for f in files); '
            "raise SystemExit(0 if ok else 1)",
        ],
    },
    "docs-missing-rollback": {
        "target_docs_missing_rollback": [
            "python3",
            "-c",
            'from pathlib import Path; text=Path("docs/OPERATIONS.md").read_text(encoding="utf-8"); '
            'has_section="## Rollback" in text or "## rollback" in text.lower(); '
            'has_revert="git revert" in text.lower() or "revert the" in text.lower(); '
            "raise SystemExit(0 if (has_section and has_revert) else 1)",
        ],
    },
    "docs-quickstart-gap": {
        "target_docs_quickstart_gap": [
            "python3",
            "-c",
            'from pathlib import Path; text=Path("README.md").read_text(encoding="utf-8"); '
            'ok=("pytest -q" in text) and (("pip install pytest" in text) or ("uv pip install pytest" in text)); '
            "raise SystemExit(0 if ok else 1)",
        ],
    },
    "notifications-type-guard-missing": {
        "target_notifications_type_guard": [
            "python3",
            "-c",
            'from pathlib import Path; text=Path("src/qa_sandbox/notifications.py").read_text(encoding="utf-8"); '
            'ok="isinstance" in text and "str" in text; raise SystemExit(0 if ok else 1)',
        ],
    },
    "trailing-whitespace": {
        "target_trailing_whitespace": [
            "python3",
            "-c",
            "from pathlib import Path; files=["
            '"src/qa_sandbox/catalog.py","src/qa_sandbox/orders.py","src/qa_sandbox/notifications.py",'
            '"src/qa_sandbox/inventory.py","src/qa_sandbox/analytics.py","scripts/report_health.py","price.py"]; '
            "ok=True; "
            "for f in files: "
            "  p=Path(f); "
            "  if p.exists() and any(line!=line.rstrip() for line in p.read_text().splitlines()): ok=False; break; "
            "raise SystemExit(0 if ok else 1)",
        ],
    },
}

_VALIDATION_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def load_validation_config(workspace: Optional[Path] = None) -> Dict[str, Any]:
    """Load per-repo validation check overrides from config.yaml.

    Config shape (optional keys — absent keys fall back to built-in defaults):

        baseline_validation_checks:
          baseline-0: ["python3", "run_lint.py"]
        rule_target_checks:
          my-rule:
            target_my_rule: ["python3", "test_rule.py"]

    Returns dict with keys 'baseline_validation_checks' and 'rule_target_checks'.
    """
    global _VALIDATION_CONFIG_CACHE
    if _VALIDATION_CONFIG_CACHE is not None:
        return _VALIDATION_CONFIG_CACHE

    result: Dict[str, Any] = {
        "baseline_validation_checks": dict(BASELINE_VALIDATION_CHECKS),
        "rule_target_checks": dict(RULE_TARGET_CHECKS),
    }

    config_path = (workspace or WORKSPACE) / "config.yaml"
    if not config_path.exists():
        _VALIDATION_CONFIG_CACHE = result
        return result

    try:
        import yaml

        with open(config_path) as f:
            config = yaml.safe_load(f)
        if config and isinstance(config, dict):
            if "baseline_validation_checks" in config:
                result["baseline_validation_checks"] = config[
                    "baseline_validation_checks"
                ]
            if "rule_target_checks" in config:
                result["rule_target_checks"] = config["rule_target_checks"]
    except Exception:
        pass

    _VALIDATION_CONFIG_CACHE = result
    return result


def clear_validation_config_cache() -> None:
    global _VALIDATION_CONFIG_CACHE
    _VALIDATION_CONFIG_CACHE = None


# LLM-fixable rules: rules that linters can't autofix but LLM agents can
# Loaded from llm_fixable_rules.yaml in the same directory
_LLM_FIXABLE_RULES_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def load_llm_fixable_rules() -> Dict[str, Dict[str, Any]]:
    """Load LLM-fixable rule definitions from YAML config.

    Returns a dict mapping rule names to their config:
    {
        'ruff-b904': {
            'description': '...',
            'prompt_hint': '...',
            'complexity': 'low',
            'languages': ['python'],
        },
        ...
    }
    """
    global _LLM_FIXABLE_RULES_CACHE
    if _LLM_FIXABLE_RULES_CACHE is not None:
        return _LLM_FIXABLE_RULES_CACHE

    rules_file = Path(__file__).parent / "llm_fixable_rules.yaml"
    if not rules_file.exists():
        _LLM_FIXABLE_RULES_CACHE = {}
        return _LLM_FIXABLE_RULES_CACHE

    try:
        import yaml

        with open(rules_file) as f:
            data = yaml.safe_load(f)
        _LLM_FIXABLE_RULES_CACHE = data.get("rules", {}) if data else {}
    except ImportError:
        # yaml not available — fall back to inline defaults
        _LLM_FIXABLE_RULES_CACHE = {
            "ruff-b904": {
                "description": "raise without cause",
                "prompt_hint": "Fix bare raise by adding appropriate from clause.",
                "complexity": "low",
                "languages": ["python"],
            },
            "ruff-s311": {
                "description": "stdlib random in security context",
                "prompt_hint": "Replace random with secrets module in security-sensitive code.",
                "complexity": "low",
                "languages": ["python"],
            },
        }
    except Exception:
        logging.debug("llm_fixable_rules.yaml load failed, defaulting to empty rules")
        _LLM_FIXABLE_RULES_CACHE = {}

    return _LLM_FIXABLE_RULES_CACHE


# Context-aware routing rules. See docs/RULES_REFERENCE.md for strategy docs.
CONTEXT_RULES: List[Dict[str, Any]] = [
    {
        "rule": "ruff-c408",
        "default_strategy": "deterministic",
        "contexts": [
            {
                "file_patterns": ["**/migrations/*.py"],
                "framework": "django",
                "fix_strategy": "skip",
                "prompt_hint": "Django migration files require dict() for runtime model resolution. Do not rewrite.",
            },
            {
                "file_patterns": ["**/test_*.py", "**/*_test.py", "tests/**"],
                "framework": "any",
                "fix_strategy": "deterministic_safe",
                "prompt_hint": "Using dict() literal in test files is safe.",
            },
        ],
    },
    {
        "rule": "ruff-b904",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": ["**/middleware*.py"],
                "framework": "django",
                "fix_strategy": "llm_with_context",
                "prompt_hint": "In Django middleware, preserve exception chain semantics. Use 'raise ... from e' but don't break existing error handling contracts.",
            }
        ],
    },
    {
        "rule": "ruff-b007",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": ["**/fixtures*.py"],
                "framework": "any",
                "fix_strategy": "deterministic_safe",
                "prompt_hint": "Rename unused loop variable `i` to `_i`",
            }
        ],
    },
    {
        "rule": "ruff-s311",
        "default_strategy": "skip",
        "contexts": [
            {
                "file_patterns": [
                    "**/test_*.py",
                    "**/*_test.py",
                    "tests/**",
                    "**/fixtures*.py",
                ],
                "framework": "any",
                "fix_strategy": "deterministic_safe",
                "prompt_hint": "Using pseudo-random generators in test files is acceptable for test data generation.",
            }
        ],
    },
    {
        "rule": "ruff-e501",
        "default_strategy": "deterministic_safe",
        "contexts": [
            {
                "file_patterns": ["**/migrations/*.py"],
                "framework": "django",
                "fix_strategy": "skip",
                "prompt_hint": "Django migration files are auto-generated and line-wrapping them makes diffs harder to read.",
            },
            {
                "file_patterns": ["**/urls.py", "**/routing.py"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "URL routing files often have long URL patterns that are more readable on one line.",
            },
        ],
    },
    {
        "rule": "broad-except",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": [
                    "**/taskqueue*.py",
                    "**/celery*.py",
                    "**/tasks/*.py",
                    "**/workers/*.py",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Task queue workers often intentionally catch all exceptions for retry/logging. Don't narrow.",
            },
            {
                "file_patterns": ["**/middleware*.py"],
                "framework": "django",
                "fix_strategy": "skip",
                "prompt_hint": "Django middleware exception handlers catch broadly by design.",
            },
            {
                "file_patterns": ["**/test_*.py", "**/*_test.py"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Broad except in tests is often intentional for asserting exception behavior.",
            },
        ],
    },
    {
        "rule": "perf-pop-front-loop",
        "default_strategy": "deterministic_safe",
        "contexts": [
            {
                "file_patterns": ["**/migrations/*.py"],
                "framework": "django",
                "fix_strategy": "skip",
                "prompt_hint": "Migration data operations may use pop(0) intentionally for ordered processing.",
            },
        ],
    },
    {
        "rule": "perf-list-membership-loop",
        "default_strategy": "deterministic_safe",
        "contexts": [
            {
                "file_patterns": ["**/test_*.py", "**/*_test.py"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Performance optimization in tests is rarely worth the cognitive overhead.",
            },
        ],
    },
    {
        "rule": "trailing-whitespace",
        "default_strategy": "deterministic_safe",
        "contexts": [
            {
                "file_patterns": [
                    "**/*.md",
                    "*.md",
                    "**/*.rst",
                    "*.rst",
                    "**/*.txt",
                    "*.txt",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Markdown and RST files may use trailing whitespace for line continuation (two trailing spaces = line break).",
            },
        ],
    },
    {
        "rule": "hardcoded-tmp-path",
        "default_strategy": "deterministic_safe",
        "contexts": [
            {
                "file_patterns": ["**/test_*.py", "**/*_test.py", "**/conftest.py"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Test files often use /tmp intentionally for tempdir fixtures.",
            },
            {
                "file_patterns": [
                    "**/scripts/*.py",
                    "scripts/*.py",
                    "**/tools/*.py",
                    "tools/*.py",
                ],
                "framework": "any",
                "fix_strategy": "llm_with_context",
                "prompt_hint": "Scripts may use /tmp intentionally for ephemeral output. Verify before replacing.",
            },
        ],
    },
    {
        "rule": "type-explicit-any",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": ["**/*.d.ts", "**/types/**", "**/typings/**"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Declaration files may legitimately use 'any' for type interop boundaries.",
            },
            {
                "file_patterns": [
                    "**/test/**",
                    "**/__tests__/**",
                    "**/*.test.ts",
                    "**/*.spec.ts",
                ],
                "framework": "any",
                "fix_strategy": "deterministic_safe",
                "prompt_hint": "In test files, replace 'any' with 'unknown' or the expected test type.",
            },
            {
                "file_patterns": [
                    "**/migrations/**",
                    "**/scripts/**",
                    "scripts/**",
                    "migrations/**",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Migration and script files often use 'any' for dynamic data shapes.",
            },
            {
                "file_patterns": [
                    "**/next.config.*",
                    "next.config.*",
                    "**/webpack.config.*",
                    "webpack.config.*",
                    "**/vite.config.*",
                    "vite.config.*",
                    "**/rollup.config.*",
                    "rollup.config.*",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Build configuration files have complex dynamic types; 'any' is often intentional.",
            },
        ],
    },
    {
        "rule": "type-missing-return",
        "default_strategy": "deterministic_safe",
        "contexts": [
            {
                "file_patterns": ["**/pages/api/**", "**/app/api/**"],
                "framework": "nextjs",
                "fix_strategy": "llm_with_context",
                "prompt_hint": "Next.js API route handlers have specific return type expectations. Infer from Response usage.",
            },
            {
                "file_patterns": ["**/controllers/**", "**/handlers/**"],
                "framework": "express",
                "fix_strategy": "llm_with_context",
                "prompt_hint": "Express handlers return void (middleware pattern). Don't add return types without checking.",
            },
        ],
    },
    {
        "rule": "xo-max-lines",
        "default_strategy": "skip",
        "contexts": [
            {
                "file_patterns": [
                    "**/generated/**",
                    "generated/**",
                    "**/auto-generated/**",
                    "auto-generated/**",
                    "**/*.generated.ts",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Generated files should not be refactored. Exclude from max-lines rule.",
            },
            {
                "file_patterns": [
                    "**/migrations/**",
                    "migrations/**",
                    "**/seeds/**",
                    "seeds/**",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Database migration and seed files are typically large by nature. Skip.",
            },
        ],
    },
    {
        "rule": "xo-complexity",
        "default_strategy": "skip",
        "contexts": [
            {
                "file_patterns": [
                    "**/generated/**",
                    "generated/**",
                    "**/auto-generated/**",
                    "auto-generated/**",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Generated code is not a refactoring target.",
            },
            {
                "file_patterns": [
                    "**/parsers/**",
                    "**/serializers/**",
                    "**/deserializers/**",
                ],
                "framework": "any",
                "fix_strategy": "llm_with_context",
                "prompt_hint": "Parser/deserializer code is inherently complex. Consider extract-by-case rather than general split.",
            },
        ],
    },
    {
        "rule": "xo-no-warning-comments",
        "default_strategy": "deterministic_safe",
        "contexts": [
            {
                "file_patterns": ["**/test/**", "**/__tests__/**"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "TODO/FIXME in test files are acceptable tracking markers.",
            },
        ],
    },
    {
        "rule": "test-gap-missing-file",
        "default_strategy": "deterministic_safe",
        "contexts": [
            {
                "file_patterns": [
                    "**/migrations/**",
                    "migrations/**",
                    "**/alembic/**",
                    "alembic/**",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Database migrations don't need test files.",
            },
            {
                "file_patterns": [
                    "**/__pycache__/**",
                    "**/node_modules/**",
                    "node_modules/**",
                    "**/.venv/**",
                    ".venv/**",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Generated/vendor directories don't need tests.",
            },
        ],
    },
    {
        "rule": "test-coverage-line",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": [
                    "**/migrations/**",
                    "migrations/**",
                    "**/alembic/**",
                    "alembic/**",
                    "**/generated/**",
                    "generated/**",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Generated/migration code should not get test coverage targets.",
            },
            {
                "file_patterns": ["**/conftest.py", "**/fixtures/**"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Test infrastructure files don't need their own coverage.",
            },
        ],
    },
    {
        "rule": "orders-tax-truncation",
        "default_strategy": "deterministic_safe",
        "contexts": [
            {
                "file_patterns": ["**/test_*.py", "**/*_test.py", "tests/**"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Test files may intentionally test truncation behavior. Do not change.",
            },
        ],
    },
    {
        "rule": "debug-console-log",
        "default_strategy": "deterministic_safe",
        "contexts": [
            {
                "file_patterns": [
                    "**/*.test.ts",
                    "**/*.spec.ts",
                    "**/__tests__/**",
                    "**/test/**",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Test files may use console.log for test output. Do not remove.",
            },
            {
                "file_patterns": [
                    "**/scripts/**",
                    "scripts/**",
                    "**/tools/**",
                    "tools/**",
                    "**/debug/**",
                    "debug/**",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Script/debug files may intentionally use console.log for output.",
            },
        ],
    },
    {
        "rule": "go-empty-interface",
        "default_strategy": "deterministic_safe",
        "contexts": [
            {
                "file_patterns": [
                    "**/vendor/**",
                    "vendor/**",
                    "**/third_party/**",
                    "third_party/**",
                    "**/*.generated.go",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Generated/vendor Go files should not be modified.",
            },
        ],
    },
    {
        "rule": "rust-unwrap-panic",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": [
                    "**/test/**",
                    "test/**",
                    "**/*_test.rs",
                    "**/tests/**",
                    "tests/**",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Rust test functions commonly use .unwrap() for simplicity.",
            },
        ],
    },
    {
        "rule": "ts-unsafe-any-cast",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": ["**/*.d.ts", "**/types/**", "**/typings/**"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Declaration files use 'as any' for type interop boundaries. Do not change.",
            },
            {
                "file_patterns": ["**/*.test.ts", "**/*.spec.ts"],
                "framework": "any",
                "fix_strategy": "deterministic_safe",
                "prompt_hint": "In test files, replace 'as any' with 'as unknown' for safer typing.",
            },
            {
                "file_patterns": ["**/*.config.*", "*.config.*"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Build config files use 'as any' for complex dynamic types.",
            },
        ],
    },
    {
        "rule": "go-unchecked-error",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": ["**/*_test.go", "**/test/**", "test/**"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Go test functions (TestXxx) often skip error checking for brevity.",
            },
            {
                "file_patterns": ["**/vendor/**", "vendor/**", "**/*.generated.go"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Generated/vendor code should not be modified.",
            },
        ],
    },
    {
        "rule": "ts-unhandled-promise",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": ["**/*.test.ts", "**/*.spec.ts", "**/__tests__/**"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Test files often intentionally leave promises unhandled for test assertions.",
            },
            {
                "file_patterns": [
                    "**/*.config.*",
                    "*.config.*",
                    "**/webpack.config.*",
                    "webpack.config.*",
                    "**/vite.config.*",
                    "vite.config.*",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Build configuration files often use fire-and-forget async patterns.",
            },
        ],
    },
    {
        "rule": "go-goroutine-unsafe",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": ["**/*_test.go", "**/test/**", "test/**"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Test goroutines don't need production-grade panic recovery.",
            },
            {
                "file_patterns": ["**/vendor/**", "vendor/**", "**/*.generated.go"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Generated/vendor code should not be modified.",
            },
        ],
    },
    {
        "rule": "type-missing-param",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": ["**/*.d.ts", "**/types/**"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Declaration files may intentionally omit parameter types for generic signatures.",
            },
            {
                "file_patterns": [
                    "**/*.config.*",
                    "*.config.*",
                    "**/generated/**",
                    "generated/**",
                    "**/auto-generated/**",
                    "auto-generated/**",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Generated and config files should not be type-annotated.",
            },
        ],
    },
    {
        "rule": "type-untyped-import",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": [
                    "**/*.config.*",
                    "*.config.*",
                    "**/generated/**",
                    "generated/**",
                    "**/scripts/**",
                    "scripts/**",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Config, generated, and script files often import untyped modules intentionally.",
            },
        ],
    },
    {
        "rule": "rust-expect-vague",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": [
                    "**/generated/**",
                    "generated/**",
                    "**/auto-generated/**",
                    "auto-generated/**",
                ],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": "Generated code should not be modified.",
            },
            {
                "file_patterns": ["**/*_test.rs", "**/tests/**", "tests/**"],
                "framework": "any",
                "fix_strategy": "skip",
                "prompt_hint": 'Test code often uses .expect("test") intentionally.',
            },
        ],
    },
]
