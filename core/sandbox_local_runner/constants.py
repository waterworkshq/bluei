"""sandbox_local_runner.constants — all module-level constants, catalogs, and paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

CURRENT_FILE = Path(__file__)
AGENT_ROOT = CURRENT_FILE.resolve().parents[2]  # qa-agent/
WORKSPACE = AGENT_ROOT.parent  # shared OpenClaw workspace root
RUNNER_PATH = AGENT_ROOT / 'core' / 'sandbox_local_runner.py'
DEFAULT_REPO = WORKSPACE / 'qa-sandbox-repo'
DEFAULT_STATE = AGENT_ROOT / 'sandbox-state.json'
DEFAULT_LOG = AGENT_ROOT / 'logs' / 'sandbox-local-run.log'
DEFAULT_FINDINGS = AGENT_ROOT / 'state' / 'qa_findings.jsonl'
DEFAULT_ISSUES = AGENT_ROOT / 'state' / 'qa_issues.json'
DEFAULT_REFACTOR_QUEUE_DIR = AGENT_ROOT / 'state' / 'refactor_queue'
DEFAULT_BATCH_RULES_PATH = Path(__file__).resolve().parent / 'batch_rules.yaml'
DEFAULT_BATCH_STATE = AGENT_ROOT / 'state' / 'batches.jsonl'
DEFAULT_WORKTREE_ROOT = AGENT_ROOT / 'worktrees'
DEFAULT_STATUS = AGENT_ROOT / 'sandbox-trial-status.json'
DEFAULT_DOCS_INDEX = AGENT_ROOT / 'state' / 'docs_index.json'
DEFAULT_LESSONS_LOG = AGENT_ROOT / 'LESSONS_LOG.md'
DEFAULT_FIX_ENGINE = 'deterministic'
DEFAULT_CLAUDE_CMD_TEMPLATE = (
    'claude --print "Read {prompt_file} and apply minimal fix for finding {finding_id}. '
    'Run relevant tests and exit non-zero on failure."'
)
QA_FIX_PROMPT_FILENAME = '.qa-fix-prompt.md'
BLOCKED_REPOS = {'workout-planner-app', 'diet-tracking-app', 'mem-db'}
DEFAULT_FINDING_COOLDOWN_SECONDS = 4 * 60 * 60
DEFAULT_STALENESS_THRESHOLD_SECONDS = 2 * 60 * 60
MAX_RECONCILIATION_EVENTS = 100

DETECTOR_CATALOG: List[Dict[str, Any]] = [
    {'rule': 'discount-math-sign', 'category': 'bug', 'confidence': 0.95, 'autofix': True},
    {'rule': 'catalog-query-not-normalized', 'category': 'bug', 'confidence': 0.93, 'autofix': True},
    {'rule': 'orders-tax-truncation', 'category': 'bug', 'confidence': 0.92, 'autofix': False},
    {'rule': 'notifications-email-no-trim', 'category': 'bug', 'confidence': 0.89, 'autofix': True},
    {'rule': 'notifications-type-guard-missing', 'category': 'bug', 'confidence': 0.87, 'autofix': True},
    {'rule': 'inventory-invalid-quantity', 'category': 'bug', 'confidence': 0.84, 'autofix': True},
    {'rule': 'broad-except', 'category': 'lint', 'confidence': 0.88, 'autofix': False},
    {'rule': 'hardcoded-tmp-path', 'category': 'lint', 'confidence': 0.81, 'autofix': True},
    {'rule': 'trailing-whitespace', 'category': 'lint', 'confidence': 0.75, 'autofix': True},
    {'rule': 'debt-todo-marker', 'category': 'todo/debt', 'confidence': 0.72, 'autofix': False},
    {'rule': 'docs-legacy-reference', 'category': 'docs-mismatch', 'confidence': 0.91, 'autofix': True},
    {'rule': 'docs-missing-rollback', 'category': 'docs-gap', 'confidence': 0.86, 'autofix': True},
    {'rule': 'docs-quickstart-gap', 'category': 'docs-gap', 'confidence': 0.74, 'autofix': True},
    {'rule': 'perf-pop-front-loop', 'category': 'perf-smell', 'confidence': 0.83, 'autofix': True},
    {'rule': 'perf-list-membership-loop', 'category': 'perf-smell', 'confidence': 0.82, 'autofix': True},
    {'rule': 'test-gap-missing-file', 'category': 'test-gap', 'confidence': 0.79, 'autofix': True},
    {'rule': 'test-gap-missing-case', 'category': 'test-gap', 'confidence': 0.77, 'autofix': True},
    {'rule': 'doc-gap-uncovered-module', 'category': 'docs-gap', 'confidence': 0.78, 'autofix': False},
    {'rule': 'doc-drift-stale-reference', 'category': 'docs-drift', 'confidence': 0.8, 'autofix': False},
    # xo linter rules for ky repo
    {'rule': 'xo-max-lines', 'category': 'refactor', 'confidence': 0.85, 'autofix': True},
    {'rule': 'xo-no-warning-comments', 'category': 'lint', 'confidence': 0.80, 'autofix': True},
    {'rule': 'xo-complexity', 'category': 'refactor', 'confidence': 0.82, 'autofix': True},
    # Python ruff linter rules for zulip repo
    {'rule': 'ruff-b007', 'category': 'lint', 'confidence': 0.75, 'autofix': True},
    {'rule': 'ruff-b904', 'category': 'bug', 'confidence': 0.80, 'autofix': False},
    {'rule': 'ruff-e501', 'category': 'style', 'confidence': 0.65, 'autofix': True},
    {'rule': 'ruff-s311', 'category': 'security', 'confidence': 0.78, 'autofix': False},
    {'rule': 'ruff-c408', 'category': 'style', 'confidence': 0.72, 'autofix': False},
    # Type safety rules (TypeScript/JavaScript)
    {'rule': 'type-explicit-any', 'category': 'type-safety', 'confidence': 0.85, 'autofix': True},
    {'rule': 'type-missing-return', 'category': 'type-safety', 'confidence': 0.80, 'autofix': True},
    {'rule': 'type-missing-param', 'category': 'type-safety', 'confidence': 0.78, 'autofix': True},
    {'rule': 'type-untyped-import', 'category': 'type-safety', 'confidence': 0.75, 'autofix': False},
    # Test coverage rules
    {'rule': 'test-coverage-branch', 'category': 'test-coverage', 'confidence': 0.82, 'autofix': True},
    {'rule': 'test-coverage-function', 'category': 'test-coverage', 'confidence': 0.80, 'autofix': True},
    {'rule': 'test-coverage-line', 'category': 'test-coverage', 'confidence': 0.78, 'autofix': False},
]

# Max-lines refactor limits - files larger than this won't be auto-refactored
MAX_LINES_REFACTOR_LIMIT = 3000  # Maximum lines for auto-refactor
MAX_LINES_REFACTOR_TARGET = 1500  # Target lines per file after split

# Rules that require Claude fix engine (complex refactoring, not deterministic)
CLAUDE_REQUIRED_RULES = {'xo-max-lines', 'xo-complexity', 'test-coverage-branch', 'test-coverage-function', 'type-missing-return'}

BASELINE_VALIDATION_CHECKS: Dict[str, List[str]] = {
    'lint_check_py': ['python3', 'lint_check.py'],
    'test_price_py': ['python3', 'test_price.py'],
}

RULE_TARGET_CHECKS: Dict[str, Dict[str, List[str]]] = {
    'discount-math-sign': {
        'target_discount_math': ['python3', 'test_price.py'],
    },
    'catalog-query-not-normalized': {
        'target_catalog_query': [
            'python3',
            '-c',
            'import sys; sys.path.insert(0, "src"); from qa_sandbox.catalog import find_item; '
            'raise SystemExit(0 if find_item(["USB Cable"], " usb cable ") else 1)',
        ],
    },
    'notifications-email-no-trim': {
        'target_notifications_email': [
            'python3',
            '-c',
            'import sys; sys.path.insert(0, "src"); from qa_sandbox.notifications import normalize_email; '
            'raise SystemExit(0 if normalize_email("  USER@Example.com  ") == "user@example.com" else 1)',
        ],
    },
    'inventory-invalid-quantity': {
        'target_inventory_negative_quantity': [
            'python3',
            '-c',
            'import sys; sys.path.insert(0, "src"); from qa_sandbox.inventory import reserve_stock; '
            'stock={"SKU": 5}; ok = reserve_stock(stock, "SKU", -1) is False and stock["SKU"] == 5; '
            'raise SystemExit(0 if ok else 1)',
        ],
    },
    'hardcoded-tmp-path': {
        'target_hardcoded_tmp_path': [
            'python3',
            '-c',
            'from pathlib import Path; text = Path("scripts/report_health.py").read_text(encoding="utf-8"); '
            'bad = \'Path("/tmp/\' in text or "Path(\'/tmp/" in text; '
            'ok = (not bad) and ("state.json" in text); raise SystemExit(0 if ok else 1)',
        ],
    },
    'docs-legacy-reference': {
        'target_docs_legacy_reference': [
            'python3',
            '-c',
            'from pathlib import Path; files=["docs/ARCHITECTURE.md","docs/TROUBLESHOOTING.md"]; '
            'ok=all("legacy_pricer.py" not in Path(f).read_text(encoding="utf-8") for f in files); '
            'raise SystemExit(0 if ok else 1)',
        ],
    },
    'docs-missing-rollback': {
        'target_docs_missing_rollback': [
            'python3',
            '-c',
            'from pathlib import Path; text=Path("docs/OPERATIONS.md").read_text(encoding="utf-8"); '
            'has_section="## Rollback" in text or "## rollback" in text.lower(); '
            'has_revert="git revert" in text.lower() or "revert the" in text.lower(); '
            'raise SystemExit(0 if (has_section and has_revert) else 1)',
        ],
    },
    'docs-quickstart-gap': {
        'target_docs_quickstart_gap': [
            'python3',
            '-c',
            'from pathlib import Path; text=Path("README.md").read_text(encoding="utf-8"); '
            'ok=("pytest -q" in text) and (("pip install pytest" in text) or ("uv pip install pytest" in text)); '
            'raise SystemExit(0 if ok else 1)',
        ],
    },
    'notifications-type-guard-missing': {
        'target_notifications_type_guard': [
            'python3',
            '-c',
            'from pathlib import Path; text=Path("src/qa_sandbox/notifications.py").read_text(encoding="utf-8"); '
            'ok="isinstance" in text and "str" in text; raise SystemExit(0 if ok else 1)',
        ],
    },
    'trailing-whitespace': {
        'target_trailing_whitespace': [
            'python3',
            '-c',
            'from pathlib import Path; files=['
            '"src/qa_sandbox/catalog.py","src/qa_sandbox/orders.py","src/qa_sandbox/notifications.py",'
            '"src/qa_sandbox/inventory.py","src/qa_sandbox/analytics.py","scripts/report_health.py","price.py"]; '
            'ok=True; '
            'for f in files: '
            '  p=Path(f); '
            '  if p.exists() and any(line!=line.rstrip() for line in p.read_text().splitlines()): ok=False; break; '
            'raise SystemExit(0 if ok else 1)',
        ],
    },
}

# Inline test template string used by apply_autofix for test-gap-missing-file rule
TEST_NOTIFICATIONS_TEMPLATE = """import pytest
from qa_sandbox.notifications import normalize_email


def test_normalize_email_trims_and_lowers() -> None:
    assert normalize_email("  USER@Example.com  ") == "user@example.com"


def test_normalize_email_invalid_input_raises() -> None:
    with pytest.raises(AttributeError):
        normalize_email(None)  # type: ignore[arg-type]
"""

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
    
    rules_file = Path(__file__).parent / 'llm_fixable_rules.yaml'
    if not rules_file.exists():
        _LLM_FIXABLE_RULES_CACHE = {}
        return _LLM_FIXABLE_RULES_CACHE
    
    try:
        import yaml
        with open(rules_file) as f:
            data = yaml.safe_load(f)
        _LLM_FIXABLE_RULES_CACHE = data.get('rules', {}) if data else {}
    except ImportError:
        # yaml not available — fall back to inline defaults
        _LLM_FIXABLE_RULES_CACHE = {
            'ruff-b904': {
                'description': 'raise without cause',
                'prompt_hint': 'Fix bare raise by adding appropriate from clause.',
                'complexity': 'low',
                'languages': ['python'],
            },
            'ruff-s311': {
                'description': 'stdlib random in security context',
                'prompt_hint': 'Replace random with secrets module in security-sensitive code.',
                'complexity': 'low',
                'languages': ['python'],
            },
        }
    except Exception:
        _LLM_FIXABLE_RULES_CACHE = {}
    
    return _LLM_FIXABLE_RULES_CACHE


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
            }
        ]
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
        ]
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
        ]
    },
    {
        "rule": "ruff-s311",
        "default_strategy": "skip",
        "contexts": [
            {
                "file_patterns": ["**/test_*.py", "**/*_test.py", "tests/**", "**/fixtures*.py"],
                "framework": "any",
                "fix_strategy": "deterministic_safe",
                "prompt_hint": "Using pseudo-random generators in test files is acceptable for test data generation.",
            }
        ]
    }
]
