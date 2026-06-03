"""bluei.engine.constants — all module-level constants, catalogs, and paths."""

from __future__ import annotations

import logging
import yaml
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
_CATALOG_FILE = CURRENT_FILE.parent / "detector_catalog.yaml"
if _CATALOG_FILE.exists():
    DETECTOR_CATALOG: List[Dict[str, Any]] = yaml.safe_load(
        _CATALOG_FILE.read_text(encoding="utf-8")
    )
else:
    _logger.warning("detector_catalog.yaml not found, using empty catalog")
    DETECTOR_CATALOG: List[Dict[str, Any]] = []
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
_CONTEXT_RULES_FILE = CURRENT_FILE.parent / "context_rules.yaml"
if _CONTEXT_RULES_FILE.exists():
    CONTEXT_RULES: List[Dict[str, Any]] = yaml.safe_load(
        _CONTEXT_RULES_FILE.read_text(encoding="utf-8")
    )
else:
    _logger.warning("context_rules.yaml not found, using empty rules")
    CONTEXT_RULES: List[Dict[str, Any]] = []
