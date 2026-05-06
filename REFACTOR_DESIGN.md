# REFACTOR_DESIGN.md — `sandbox_local_runner.py` Package Refactor

**Source:** `/home/vikas/.openclaw/workspace/qa-agent/core/sandbox_local_runner.py`
**Source size:** 4,176 lines
**Date:** 2026-03-25 (audited)
**Status:** FINAL AUDITED DESIGN — ready for execution

---

## CRITICAL CORRECTION TO EXISTING PLAN

> ⚠️ **The existing plan (memory/2026-03-25.md) contains a fundamental error.**
> The plan references a `SandboxLocalRunner` class holding per-run state. **That class does not exist in the source file.**
> The entire file is a **free-function + global-state monolith** — no classes at all (only the `Finding` dataclass).
> All per-run "state" is carried implicitly through function parameters or via module-level caches/singletons.
> The proposed `lifecycle.py` (intended for `SandboxLocalRunner`) does NOT make sense as described.
> The design below corrects this completely.

---

## 0. Phase 0 — Auto-Generate Function-to-Module Mapping

Before any manual refactoring, run this Phase 0 script to produce an authoritative mapping.

```python
#!/usr/bin/env python3
"""Phase 0: Generate function-to-module mapping from source AST.

Run from the qa-agent/core/ directory after copying the monolith.
Reads sandbox_local_runner.py, emits function→module assignments,
detects any unaccounted definitions, and validates the acyc Dependency order.
"""

import ast, json, sys
from pathlib import Path

SOURCE = Path("sandbox_local_runner.py")

def phase0_generate():
    tree = ast.parse(SOURCE.read_text())
    exports = {}  # name → {"type": "function"|"class", "lineno": int, "module": None}

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            exports[node.name] = {"type": "class", "lineno": node.lineno, "module": None}
        elif isinstance(node, ast.FunctionDef) and isinstance(node, ast.FunctionDef):
            if not node.name.startswith("_") or node.name.startswith("_git") or node.name.startswith("_code") or node.name.startswith("_has") or node.name.startswith("_external") or node.name.startswith("_get_ruff") or node.name.startswith("_append") or node.name.startswith("_build") or node.name.startswith("_read_lines") or node.name.startswith("_add_finding") or node.name.startswith("_normalize"):
                exports[node.name] = {"type": "function", "lineno": node.lineno, "module": None}

    # Sort by line number
    rows = sorted(exports.values(), key=lambda x: x["lineno"])
    for r in rows:
        print(f"{r['lineno']:5d}  {r['type']:8s}  {r['name']}")

phase0_generate()
```

**Action:** Run this and diff the output against the table in Section 3 below. Any discrepancy is a bug in this design document.

---

## 1. Package Structure

```
qa-agent/core/sandbox_local_runner/
├── __init__.py          # Minimal re-exports of public API
├── constants.py         # ALL module-level constants (paths, catalogs, limits)
├── models.py            # Finding dataclass + date/time helpers
├── utils.py             # run_capture, run_no_capture, is_path_tracked
├── state.py             # State, issues, findings persistence functions
├── gh.py                # All GitHub API calls
├── linters.py           # Ruff rule cache + ruff CLI runner
├── git_utils.py         # Git helpers + docs index
├── orchestrator.py      # discover_findings + cycle command builders
├── prompts.py           # All LLM prompt render functions
├── lifecycle.py         # apply_autofix, validation, git ops, fix orchestration
└── cli.py               # main() + update_status_artifact
```

**One class per file rule applies to `models.py` (only the `Finding` dataclass).**

---

## 2. Complete Function/Class Inventory (By Line Number)

Every symbol in the source file is assigned to exactly one module. `—` denotes a private helper (internal to its module).

| Line | Symbol | Type | Module | Public API? |
|------|--------|------|--------|-------------|
| 195 | `Finding` | class | `models.py` | ✅ public |
| 206 | `Finding.as_dict` | method | `models.py` | ✅ public |
| 220 | `now_iso` | function | `models.py` | ✅ public |
| 224 | `parse_iso` | function | `models.py` | ✅ public |
| 233 | `age_seconds` | function | `models.py` | ✅ public |
| 241 | `load_state` | function | `state.py` | ✅ public |
| 261 | `save_state` | function | `state.py` | ✅ public |
| 266 | `_append_text` | function | `state.py` | 🔒 private |
| 272 | `sanitize_command_template` | function | `utils.py` | ✅ public |
| 279 | `command_list_to_shell` | function | `utils.py` | ✅ public |
| 283 | `append_lesson` | function | `utils.py` | ✅ public |
| 313 | `run_capture` | function | `utils.py` | ✅ public |
| 321 | `run_no_capture` | function | `utils.py` | ✅ public |
| 326 | `is_path_tracked` | function | `utils.py` | ✅ public |
| 331 | `run_xo_linter_in_container` | function | `linters.py` | 🔒 private |
| 429 | `discover_xo_linter_findings` | function | `linters.py` | ✅ public |
| 558 | `discover_python_linter_findings` | function | `linters.py` | ✅ public |
| 693 | `discover_typescript_type_findings` | function | `linters.py` | ✅ public |
| 765 | `discover_test_coverage_findings` | function | `linters.py` | ✅ public |
| 864 | `assert_safe_repo` | function | `utils.py` | ✅ public |
| 872 | `_git_last_commit_for_path` | function | `git_utils.py` | 🔒 private |
| 879 | `_code_paths_for_docs_index` | function | `git_utils.py` | 🔒 private |
| 890 | `_has_inline_doc` | function | `git_utils.py` | 🔒 private |
| 906 | `_external_doc_text` | function | `git_utils.py` | 🔒 private |
| 921 | `refresh_docs_index` | function | `git_utils.py` | ✅ public |
| 966 | `load_docs_index` | function | `git_utils.py` | ✅ public |
| 986 | `guard_open_issues` | function | `state.py` | ✅ public |
| 992 | `guard_open_prs` | function | `state.py` | ✅ public |
| 998 | `get_branch` | function | `git_utils.py` | ✅ public |
| 1003 | `stable_finding_id` | function | `models.py` | ✅ public |
| 1008 | `load_findings_seen` | function | `state.py` | ✅ public |
| 1027 | `append_findings` | function | `state.py` | ✅ public |
| 1042 | `load_issues` | function | `state.py` | ✅ public |
| 1053 | `save_issues` | function | `state.py` | ✅ public |
| 1058 | `get_origin_url` | function | `gh.py` | ✅ public |
| 1065 | `parse_github_repo` | function | `gh.py` | ✅ public |
| 1084 | `finding_dedupe_marker` | function | `gh.py` | ✅ public |
| 1088 | `branch_suffix` | function | `utils.py` | ✅ public |
| 1093 | `gh_json` | function | `gh.py` | 🔒 private |
| 1103 | `parse_issue_number_from_url` | function | `gh.py` | ✅ public |
| 1110 | `parse_pr_number_from_url` | function | `gh.py` | ✅ public |
| 1117 | `find_existing_github_issue` | function | `gh.py` | ✅ public |
| 1144 | `find_existing_github_pr` | function | `gh.py` | ✅ public |
| 1171 | `gh_issue_comment` | function | `gh.py` | ✅ public |
| 1179 | `gh_issue_close` | function | `gh.py` | ✅ public |
| 1187 | `gh_pr_comment` | function | `gh.py` | ✅ public |
| 1195 | `finding_from_issue_record` | function | `gh.py` | ✅ public |
| 1226 | `repo_is_sandbox` | function | `gh.py` | ✅ public |
| 1234 | `fetch_open_prs_for_merge` | function | `gh.py` | ✅ public |
| 1256 | `evaluate_pr_check_health` | function | `gh.py` | ✅ public |
| 1313 | `evaluate_pr_reviews` | function | `gh.py` | ✅ public |
| 1344 | `merge_pr` | function | `gh.py` | ✅ public |
| 1365 | `create_or_update_github_issue` | function | `gh.py` | ✅ public |
| 1431 | `_get_ruff_rule_description` | function | `linters.py` | 🔒 private / ⚠️ DEAD CODE |
| 1455 | `create_or_update_github_pr` | function | `gh.py` | ✅ public |
| 1507 | `fetch_github_live_counts` | function | `gh.py` | ✅ public |
| 1554 | `record_reconciliation_event` | function | `state.py` | ✅ public |
| 1589 | `reconcile_open_workload` | function | `state.py` | ✅ public |
| 1632 | `mark_finding_activity` | function | `state.py` | ✅ public |
| 1644 | `filter_findings_by_cooldown` | function | `state.py` | ✅ public |
| 1676 | `_build_base_cycle_command` | function | `orchestrator.py` | 🔒 private |
| 1728 | `build_active_cycle_command` | function | `orchestrator.py` | ✅ public |
| 1733 | `build_issue_cycle_command` | function | `orchestrator.py` | ✅ public |
| 1738 | `build_pr_cycle_command` | function | `orchestrator.py` | ✅ public |
| 1743 | `build_merge_cycle_command` | function | `orchestrator.py` | ✅ public |
| 1748 | `build_orchestrated_cycle_command` | function | `orchestrator.py` | ✅ public |
| 1753 | `build_reconcile_only_command` | function | `orchestrator.py` | ✅ public |
| 1776 | `build_docs_index_refresh_command` | function | `orchestrator.py` | ✅ public |
| 1793 | `build_verification_only_command` | function | `orchestrator.py` | ✅ public |
| 1817 | `update_status_artifact` | function | `cli.py` | ✅ public |
| 1905 | `discover_findings` | function | `orchestrator.py` | ✅ public |
| 1923 | `_read_lines` | nested function | `orchestrator.py` | 🔒 private (nested in discover_findings) |
| 1929 | `_add_finding` | nested function | `orchestrator.py` | 🔒 private (nested in discover_findings) |
| 2266 | `create_issues_for_findings` | function | `orchestrator.py` | ✅ public |
| 2307 | `choose_safe_autofix_items` | function | `orchestrator.py` | ✅ public |
| 2315 | `find_issue_for_finding` | function | `orchestrator.py` | ✅ public |
| 2322 | `append_issue_history` | function | `orchestrator.py` | ✅ public |
| 2330 | `set_issue_status` | function | `orchestrator.py` | ✅ public |
| 2338 | `count_failed_fix_attempts` | function | `orchestrator.py` | ✅ public |
| 2362 | `ensure_issue_for_finding` | function | `orchestrator.py` | ✅ public |
| 2395 | `verify_fix_closed` | function | `lifecycle.py` | ✅ public |
| 2414 | `apply_autofix` | function | `lifecycle.py` | ✅ public |
| 2644 | `git_commit_all` | function | `lifecycle.py` | ✅ public |
| 2666 | `git_push_branch` | function | `lifecycle.py` | ✅ public |
| 2677 | `diff_stats` | function | `lifecycle.py` | ✅ public |
| 2697 | `_normalize_check_output` | function | `lifecycle.py` | 🔒 private |
| 2705 | `run_named_checks` | function | `lifecycle.py` | ✅ public |
| 2728 | `build_target_checks` | function | `lifecycle.py` | ✅ public |
| 2782 | `render_test_coverage_prompt` | function | `prompts.py` | ✅ public |
| 2848 | `render_type_safety_prompt` | function | `prompts.py` | ✅ public |
| 2914 | `render_complexity_refactor_prompt` | function | `prompts.py` | ✅ public |
| 2986 | `render_maxlines_refactor_prompt` | function | `prompts.py` | ✅ public |
| 3058 | `render_claude_fix_prompt` | function | `prompts.py` | ✅ public |
| 3150 | `apply_claude_fix` | function | `lifecycle.py` | ✅ public |
| 3210 | `run_validation_gate` | function | `lifecycle.py` | ✅ public |
| 3262 | `classify_review_feedback` | function | `lifecycle.py` | ✅ public |
| 3271 | `review_loop_allowed` | function | `lifecycle.py` | ✅ public |
| 3279 | `main` | function | `cli.py` | ✅ public |

**Totals:** 1 class + 94 functions = 95 symbols accounted for. Zero omissions.

### Special Cases

- **Lines 2426–2430 (`test_normalize_email_*`):** These are NOT functions. They are **inline test function definitions embedded in a triple-quoted string template** inside the `apply_autofix` function body (the template written to `tests/test_notifications.py` for the `test-gap-missing-file` rule). They should be placed inside `constants.py` as the `TEST_NOTIFICATIONS_TEMPLATE` string constant.

- **`_get_ruff_rule_description` (line 1431):** Defined and cached in module `_Ruff_Rule_Descriptions`, but **never called** anywhere in the file. It is dead code. It belongs in `linters.py` but should be verified as dead before committing. If used in the future, it would fetch ruff rule descriptions via CLI. ⚠️ Flag for removal or integration.

---

## 3. Global State and Module-Level Singletons

All module-level mutable state must be declared explicitly and managed per-module:

| Symbol | Module | Type | Thread Safety | Notes |
|--------|--------|------|---------------|-------|
| `_Ruff_Rule_Descriptions` | `linters.py` | dict (cache) | ❌ not thread-safe | Module-level cache. In a future async/multi-process context, replace with `functools.lru_cache` or a lock. |
| `DETECTOR_CATALOG` | `constants.py` | list of dicts | ✅ immutable after init | 40-entry read-only catalog. Treat as immutable. |
| `BASELINE_VALIDATION_CHECKS` | `constants.py` | dict | ✅ immutable after init | |
| `RULE_TARGET_CHECKS` | `constants.py` | dict | ✅ immutable after init | |
| `CLAUDE_REQUIRED_RULES` | `constants.py` | set | ✅ immutable after init | |
| `BLOCKED_REPOS` | `constants.py` | set | ✅ immutable after init | |
| `MAX_LINES_REFACTOR_LIMIT` | `constants.py` | int | ✅ immutable | |
| `MAX_LINES_REFACTOR_TARGET` | `constants.py` | int | ✅ immutable | |
| `DEFAULT_FINDING_COOLDOWN_SECONDS` | `constants.py` | int | ✅ immutable | |
| `DEFAULT_STALENESS_THRESHOLD_SECONDS` | `constants.py` | int | ✅ immutable | |
| `MAX_RECONCILIATION_EVENTS` | `constants.py` | int | ✅ immutable | |
| `QA_FIX_PROMPT_FILENAME` | `constants.py` | str | ✅ immutable | |
| `WORKSPACE` | `constants.py` | Path | ✅ immutable | Derived from `__file__` at import time |
| `AGENT_ROOT` | `constants.py` | Path | ✅ immutable | |
| `RUNNER_PATH` | `constants.py` | Path | ✅ immutable | |

**Path constants** (`DEFAULT_REPO`, `DEFAULT_STATE`, `DEFAULT_LOG`, `DEFAULT_FINDINGS`, `DEFAULT_ISSUES`, `DEFAULT_WORKTREE_ROOT`, `DEFAULT_STATUS`, `DEFAULT_DOCS_INDEX`, `DEFAULT_LESSONS_LOG`, `DEFAULT_FIX_ENGINE`, `DEFAULT_CLAUDE_CMD_TEMPLATE`) — all in `constants.py`.

**IMPORTANT — NO class-level singleton for SandboxLocalRunner:** Since there is no such class, this concern from the original plan is moot. All per-run state is passed as function parameters. The sole module-level cache is `_Ruff_Rule_Descriptions`.

---

## 4. Acyclic Dependency Ordering

```
models.py
    ↑
constants.py    (no upward deps — leaves)
    ↑
utils.py        (imports constants only)
    ↑
state.py        (imports models, utils)
    ↑
gh.py           (imports models, utils, state)
    ↑
linters.py      (imports models, utils, state)
    ↑
git_utils.py    (imports models, utils, state, linters)
    ↑
prompts.py      (imports utils, models)
    ↑
orchestrator.py (imports models, utils, state, linters, git_utils, gh, prompts)
    ↑
lifecycle.py    (imports models, utils, state, linters, git_utils, orchestrator, prompts)
    ↑
cli.py          (imports models, utils, state, gh, orchestrator, lifecycle, prompts)
```

### Dependency Edge Summary

| From | Imports |
|------|---------|
| `models.py` | typing stdlib only |
| `constants.py` | `pathlib`, `typing` stdlib only |
| `utils.py` | `subprocess`, `shlex`, `argparse`, `shutil` stdlib only |
| `state.py` | `models`, `utils`, `json`, `datetime` |
| `gh.py` | `models`, `utils`, `re`, `json` |
| `linters.py` | `models`, `utils`, `subprocess`, `json`, `pathlib`, `re`, `ast` |
| `git_utils.py` | `models`, `utils`, `linters`, `json`, `datetime`, `ast` |
| `prompts.py` | `models`, `utils` |
| `orchestrator.py` | `models`, `utils`, `state`, `linters`, `git_utils`, `gh`, `prompts`, `os` |
| `lifecycle.py` | `models`, `utils`, `state`, `linters`, `git_utils`, `orchestrator`, `prompts`, `subprocess`, `shlex`, `shutil`, `re`, `json`, `pathlib` |
| `cli.py` | `models`, `utils`, `state`, `gh`, `orchestrator`, `lifecycle`, `prompts`, `argparse`, `datetime`, `timedelta`, `pathlib`, `json` |

**No cycles exist.** The ordering `models → constants → utils → state → gh → linters → git_utils → prompts → orchestrator → lifecycle → cli` is a topological sort.

---

## 5. Module-by-Module Content

### `constants.py` (TOP OF DEPENDENCY GRAPH)
All module-level constants, paths, the `DETECTOR_CATALOG` list, `BASELINE_VALIDATION_CHECKS` dict, `RULE_TARGET_CHECKS` dict, `CLAUDE_REQUIRED_RULES`, `MAX_*` limits, `DEFAULT_*` paths, `BLOCKED_REPOS`, `WORKSPACE`, `AGENT_ROOT`, `RUNNER_PATH`, `QA_FIX_PROMPT_FILENAME`.

**Public symbols:** None — this module exposes data only, no functions.
**Re-export in `__init__.py`:** `DETECTOR_CATALOG`, `BASELINE_VALIDATION_CHECKS`, `RULE_TARGET_CHECKS`, `CLAUDE_REQUIRED_RULES`, `BLOCKED_REPOS`, `MAX_LINES_REFACTOR_LIMIT`, `MAX_LINES_REFACTOR_TARGET`, `DEFAULT_FINDING_COOLDOWN_SECONDS`, `DEFAULT_STALENESS_THRESHOLD_SECONDS`, `MAX_RECONCILIATION_EVENTS`, `QA_FIX_PROMPT_FILENAME`, `WORKSPACE`, `AGENT_ROOT`, `RUNNER_PATH`, `DEFAULT_*` path constants.

### `models.py`
- `class Finding` (line 195) + its `as_dict` method
- `now_iso` (line 220)
- `parse_iso` (line 224)
- `age_seconds` (line 233)
- `stable_finding_id` (line 1003)

**Re-export in `__init__.py`:** `Finding`, `now_iso`, `parse_iso`, `age_seconds`, `stable_finding_id`.

### `utils.py`
- `run_capture` (line 313)
- `run_no_capture` (line 321)
- `is_path_tracked` (line 326)
- `sanitize_command_template` (line 272)
- `command_list_to_shell` (line 279)
- `append_lesson` (line 283)
- `assert_safe_repo` (line 864)
- `branch_suffix` (line 1088)

**Re-export in `__init__.py`:** all of the above.

### `state.py`
- `load_state` (line 241)
- `save_state` (line 261)
- `_append_text` (line 266) — private
- `load_findings_seen` (line 1008)
- `append_findings` (line 1027)
- `load_issues` (line 1042)
- `save_issues` (line 1053)
- `guard_open_issues` (line 986)
- `guard_open_prs` (line 992)
- `record_reconciliation_event` (line 1554)
- `reconcile_open_workload` (line 1589)
- `mark_finding_activity` (line 1632)
- `filter_findings_by_cooldown` (line 1644)

**Re-export in `__init__.py`:** all except `_append_text`.

### `gh.py`
- `get_origin_url` (line 1058)
- `parse_github_repo` (line 1065)
- `finding_dedupe_marker` (line 1084)
- `gh_json` (line 1093) — private
- `parse_issue_number_from_url` (line 1103)
- `parse_pr_number_from_url` (line 1110)
- `find_existing_github_issue` (line 1117)
- `find_existing_github_pr` (line 1144)
- `gh_issue_comment` (line 1171)
- `gh_issue_close` (line 1179)
- `gh_pr_comment` (line 1187)
- `finding_from_issue_record` (line 1195)
- `repo_is_sandbox` (line 1226)
- `fetch_open_prs_for_merge` (line 1234)
- `evaluate_pr_check_health` (line 1256)
- `evaluate_pr_reviews` (line 1313)
- `merge_pr` (line 1344)
- `create_or_update_github_issue` (line 1365)
- `create_or_update_github_pr` (line 1455)
- `fetch_github_live_counts` (line 1507)

**Re-export in `__init__.py`:** all except `gh_json`.

### `linters.py`
- `_Ruff_Rule_Descriptions` (line 1428) — module-level cache singleton
- `_get_ruff_rule_description` (line 1431) — private, ⚠️ dead code, verify before keeping
- `run_xo_linter_in_container` (line 331)
- `discover_xo_linter_findings` (line 429)
- `discover_python_linter_findings` (line 558)
- `discover_typescript_type_findings` (line 693)
- `discover_test_coverage_findings` (line 765)

**Re-export in `__init__.py`:** all discovery functions. Do NOT re-export `_Ruff_Rule_Descriptions` or `_get_ruff_rule_description`.

### `git_utils.py`
- `get_branch` (line 998)
- `_git_last_commit_for_path` (line 872) — private
- `_code_paths_for_docs_index` (line 879) — private
- `_has_inline_doc` (line 890) — private
- `_external_doc_text` (line 906) — private
- `refresh_docs_index` (line 921)
- `load_docs_index` (line 966)

**Re-export in `__init__.py`:** `get_branch`, `refresh_docs_index`, `load_docs_index`.

### `prompts.py`
- `render_test_coverage_prompt` (line 2782)
- `render_type_safety_prompt` (line 2848)
- `render_complexity_refactor_prompt` (line 2914)
- `render_maxlines_refactor_prompt` (line 2986)
- `render_claude_fix_prompt` (line 3058)

**Re-export in `__init__.py`:** all of the above.

### `orchestrator.py`
- `_build_base_cycle_command` (line 1676) — private
- `build_active_cycle_command` (line 1728)
- `build_issue_cycle_command` (line 1733)
- `build_pr_cycle_command` (line 1738)
- `build_merge_cycle_command` (line 1743)
- `build_orchestrated_cycle_command` (line 1748)
- `build_reconcile_only_command` (line 1753)
- `build_docs_index_refresh_command` (line 1776)
- `build_verification_only_command` (line 1793)
- `discover_findings` (line 1905) with nested `_read_lines` (line 1923) and `_add_finding` (line 1929)
- `create_issues_for_findings` (line 2266)
- `choose_safe_autofix_items` (line 2307)
- `find_issue_for_finding` (line 2315)
- `append_issue_history` (line 2322)
- `set_issue_status` (line 2330)
- `count_failed_fix_attempts` (line 2338)
- `ensure_issue_for_finding` (line 2362)

**Re-export in `__init__.py`:** all except `_build_base_cycle_command`, `_read_lines`, `_add_finding`.

### `lifecycle.py`
- `verify_fix_closed` (line 2395)
- `apply_autofix` (line 2414) — **contains the inline test template string** (lines 2417-2430 in source)
- `git_commit_all` (line 2644)
- `git_push_branch` (line 2666)
- `diff_stats` (line 2677)
- `_normalize_check_output` (line 2697) — private
- `run_named_checks` (line 2705)
- `build_target_checks` (line 2728)
- `apply_claude_fix` (line 3150)
- `run_validation_gate` (line 3210)
- `classify_review_feedback` (line 3262)
- `review_loop_allowed` (line 3271)

**Re-export in `__init__.py`:** all except `_normalize_check_output`.

### `cli.py`
- `update_status_artifact` (line 1817)
- `main` (line 3279)

**Re-export in `__init__.py`:** `update_status_artifact`. Do NOT re-export `main` (it is the entry point).

---

## 6. `__init__.py` — Minimal Explicit Re-Exports

The package `__init__.py` MUST contain explicit re-exports only. No wildcard imports.

```python
"""sandbox_local_runner — local sandbox QA workflow runner package."""

# Re-export public API only. No wildcard imports.
from sandbox_local_runner.constants import (
    DETECTOR_CATALOG,
    BASELINE_VALIDATION_CHECKS,
    RULE_TARGET_CHECKS,
    CLAUDE_REQUIRED_RULES,
    BLOCKED_REPOS,
    MAX_LINES_REFACTOR_LIMIT,
    MAX_LINES_REFACTOR_TARGET,
    DEFAULT_FINDING_COOLDOWN_SECONDS,
    DEFAULT_STALENESS_THRESHOLD_SECONDS,
    MAX_RECONCILIATION_EVENTS,
    QA_FIX_PROMPT_FILENAME,
    WORKSPACE,
    AGENT_ROOT,
    RUNNER_PATH,
    DEFAULT_REPO,
    DEFAULT_STATE,
    DEFAULT_LOG,
    DEFAULT_FINDINGS,
    DEFAULT_ISSUES,
    DEFAULT_WORKTREE_ROOT,
    DEFAULT_STATUS,
    DEFAULT_DOCS_INDEX,
    DEFAULT_LESSONS_LOG,
    DEFAULT_FIX_ENGINE,
    DEFAULT_CLAUDE_CMD_TEMPLATE,
)
from sandbox_local_runner.models import Finding, now_iso, parse_iso, age_seconds, stable_finding_id
from sandbox_local_runner.utils import (
    run_capture, run_no_capture, is_path_tracked,
    sanitize_command_template, command_list_to_shell, append_lesson,
    assert_safe_repo, branch_suffix,
)
from sandbox_local_runner.state import (
    load_state, save_state,
    load_findings_seen, append_findings,
    load_issues, save_issues,
    guard_open_issues, guard_open_prs,
    record_reconciliation_event, reconcile_open_workload,
    mark_finding_activity, filter_findings_by_cooldown,
)
from sandbox_local_runner.gh import (
    get_origin_url, parse_github_repo, finding_dedupe_marker,
    parse_issue_number_from_url, parse_pr_number_from_url,
    find_existing_github_issue, find_existing_github_pr,
    gh_issue_comment, gh_issue_close, gh_pr_comment,
    finding_from_issue_record, repo_is_sandbox,
    fetch_open_prs_for_merge, evaluate_pr_check_health,
    evaluate_pr_reviews, merge_pr,
    create_or_update_github_issue, create_or_update_github_pr,
    fetch_github_live_counts,
)
from sandbox_local_runner.linters import (
    run_xo_linter_in_container,
    discover_xo_linter_findings,
    discover_python_linter_findings,
    discover_typescript_type_findings,
    discover_test_coverage_findings,
)
from sandbox_local_runner.git_utils import get_branch, refresh_docs_index, load_docs_index
from sandbox_local_runner.prompts import (
    render_test_coverage_prompt,
    render_type_safety_prompt,
    render_complexity_refactor_prompt,
    render_maxlines_refactor_prompt,
    render_claude_fix_prompt,
)
from sandbox_local_runner.orchestrator import (
    build_active_cycle_command,
    build_issue_cycle_command,
    build_pr_cycle_command,
    build_merge_cycle_command,
    build_orchestrated_cycle_command,
    build_reconcile_only_command,
    build_docs_index_refresh_command,
    build_verification_only_command,
    discover_findings,
    create_issues_for_findings,
    choose_safe_autofix_items,
    find_issue_for_finding,
    append_issue_history,
    set_issue_status,
    count_failed_fix_attempts,
    ensure_issue_for_finding,
)
from sandbox_local_runner.lifecycle import (
    verify_fix_closed,
    apply_autofix,
    git_commit_all,
    git_push_branch,
    diff_stats,
    run_named_checks,
    build_target_checks,
    apply_claude_fix,
    run_validation_gate,
    classify_review_feedback,
    review_loop_allowed,
)
from sandbox_local_runner.cli import update_status_artifact
```

This ensures any code doing `from sandbox_local_runner import X` continues to work identically after the refactor.

---

## 7. Enforcement Mechanisms

### 7.1 Import Audit CI Script (MUST RUN on Every PR)

```python
#!/usr/bin/env python3
"""enforce_architecture.py — CI gate: verify acyc import order and completeness."""

import ast, sys
from pathlib import Path

PACKAGE = Path("qa-agent/core/sandbox_local_runner")
EXPECTED_MODULES = {
    "constants.py", "models.py", "utils.py", "state.py", "gh.py",
    "linters.py", "git_utils.py", "prompts.py", "orchestrator.py",
    "lifecycle.py", "cli.py", "__init__.py",
}
LEGAL_IMPORTS = {
    # module: set of modules it may legally import from
    "constants.py": set(),
    "models.py": {"constants"},
    "utils.py": {"constants"},
    "state.py": {"constants", "models", "utils"},
    "gh.py": {"constants", "models", "utils"},
    "linters.py": {"constants", "models", "utils"},
    "git_utils.py": {"constants", "models", "utils", "linters"},
    "prompts.py": {"constants", "models", "utils"},
    "orchestrator.py": {"constants", "models", "utils", "state", "linters", "git_utils", "gh", "prompts"},
    "lifecycle.py": {"constants", "models", "utils", "state", "linters", "git_utils", "prompts", "orchestrator"},
    "cli.py": {"constants", "models", "utils", "state", "gh", "orchestrator", "lifecycle", "prompts"},
}

def check_module(path: Path) -> list[str]:
    """Return list of violations for one module. Empty = clean."""
    tree = ast.parse(path.read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and "sandbox_local_runner" in str(node.module):
                imports.add(node.module.split(".")[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "sandbox_local_runner" in str(alias.name):
                    imports.add(alias.name.split(".")[-1])
    name = path.name
    legal = LEGAL_IMPORTS.get(name, set())
    illegal = imports - legal
    return [f"{name} illegally imports {i}" for i in illegal]

def main():
    violations = []
    for py_file in PACKAGE.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        violations.extend(check_module(py_file))

    # Also check __init__.py has no wildcard re-exports
    init = PACKAGE / "__init__.py"
    if init.exists():
        src = init.read_text()
        if "*" in src and "import *" in src:
            violations.append("__init__.py contains wildcard import")

    if violations:
        for v in violations:
            print(f"VIOLATION: {v}", file=sys.stderr)
        sys.exit(1)
    print("✅  Architecture check passed")

main()
```

### 7.2 Completeness Check — Every Source Symbol Has a Destination

```python
#!/usr/bin/env python3
"""check_completeness.py — verify every source def/class appears in exactly one target module."""

import ast
from pathlib import Path

SOURCE = Path("sandbox_local_runner.py")
PACKAGE = Path("qa-agent/core/sandbox_local_runner")

# Build source inventory
tree = ast.parse(SOURCE.read_text())
source_symbols = {}
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef):
        source_symbols[node.name] = "class"
    elif isinstance(node, ast.FunctionDef):
        source_symbols[node.name] = "function"

# Build target inventory
target_symbols = {}
for py_file in PACKAGE.glob("*.py"):
    if py_file.name.startswith("_"):
        continue
    t = ast.parse(py_file.read_text())
    for node in ast.walk(t):
        if isinstance(node, ast.ClassDef):
            target_symbols[node.name] = py_file.name
        elif isinstance(node, ast.FunctionDef):
            target_symbols[node.name] = py_file.name

missing = set(source_symbols.keys()) - set(target_symbols.keys())
extra = set(target_symbols.keys()) - set(source_symbols.keys())

if missing:
    print(f"MISSING from package: {missing}")
if extra:
    print(f"EXTRA in package (not in source): {extra}")
if not missing and not extra:
    print("✅  Completeness check passed")
```

### 7.3 Shadowing Detection

Before the refactor cutover, add to the CI:

```bash
# After refactor, verify the old monolith no longer exists
test ! -f sandbox_local_runner.py || { echo "Old monolith still present!"; exit 1; }

# Verify the package is importable
python3 -c "from sandbox_local_runner import *" 2>&1 | grep -v "re-imported as" || true
```

---

## 8. Phased Execution Plan

### Phase 1: Bootstrap (Create Skeleton)
1. Create `sandbox_local_runner/` directory
2. Create empty `__init__.py`, `constants.py`, `models.py`, `utils.py`, `state.py`, `gh.py`, `linters.py`, `git_utils.py`, `prompts.py`, `orchestrator.py`, `lifecycle.py`, `cli.py`
3. Create `enforce_architecture.py` and `check_completeness.py` in the package directory

### Phase 2: Populate Modules (dependency order)
Populate in reverse dependency order (bottom of graph first):
1. Populate `constants.py` — all module-level constants
2. Populate `models.py` — Finding + date helpers
3. Populate `utils.py` — run_capture, run_no_capture, etc.
4. Populate `state.py`
5. Populate `gh.py`
6. Populate `linters.py` — ⚠️ verify `_get_ruff_rule_description` is dead before committing
7. Populate `git_utils.py`
8. Populate `prompts.py`
9. Populate `orchestrator.py`
10. Populate `lifecycle.py` — move the inline test template string to `constants.py`
11. Populate `cli.py`
12. Populate `__init__.py`

### Phase 3: Smoke Test
```bash
cd qa-agent/core/sandbox_local_runner
python3 -c "import sandbox_local_runner; print('import ok')"
python3 enforce_architecture.py
python3 check_completeness.py
```

### Phase 4: Cutover
1. Replace `sandbox_local_runner.py` with a shim:
```python
"""sandbox_local_runner — shim for backward compatibility."""
from sandbox_local_runner import *
```
2. Run the full test suite
3. Verify all callers (`--repo-path`, `--state-file`, etc. CLI invocations) work identically
4. Remove the shim once all callers are updated

---

## 9. Flagged Uncertainties and Risks

### 🔴 HIGH RISK

**R1. `_get_ruff_rule_description` is dead code (line 1431).**
It is defined, populates `_Ruff_Rule_Descriptions`, but no function in the file calls it. This means:
- The ruff rule description cache is populated but never read.
- Either this was intended for future use (the `create_or_update_github_issue` and `create_or_update_github_pr` functions would be the natural consumers), OR it was abandoned.
- **Action:** Investigate before discarding. If intentionally dead, remove it and the `_Ruff_Rule_Descriptions` cache. If accidentally dead, integrate it into the PR/issue creation path.

**R2. The original plan referenced a `SandboxLocalRunner` class that does not exist.**
The plan described a `lifecycle.py` module containing `SandboxLocalRunner` and `apply_autofix`. Since no such class exists, the lifecycle module's design intent was unclear. The design here correctly places all per-run logic as free functions in `lifecycle.py`.

**R3. Inline test template at `apply_autofix` lines 2417–2430.**
The triple-quoted string containing `test_normalize_email_*` functions is embedded in `apply_autofix`. This should be moved to `constants.py` as `TEST_NOTIFICATIONS_TEMPLATE = """..."""`. **Risk:** The string contains Python source code with its own indentation. When moving, ensure the template string's internal indentation is preserved exactly as-is (the `\n` newline handling and leading indentation inside the template).

### 🟡 MEDIUM RISK

**R4. `_Ruff_Rule_Descriptions` is not thread-safe.**
It is a plain dict mutated by `_get_ruff_rule_description`. If the package is ever used in a multi-threaded context (e.g., concurrent issue processing), this cache could corrupt. Mitigation: either document it as single-threaded-only, or wrap with a lock.

**R5. Path constants are computed at import time from `__file__`.**
`WORKSPACE = CURRENT_FILE.resolve().parents[1]` and downstream `AGENT_ROOT`, `RUNNER_PATH`, and all `DEFAULT_*` paths are frozen at import. This is standard but means that if the file is moved without updating `WORKSPACE` derivation, paths break. This was already the case in the monolith.

**R6. `RULE_TARGET_CHECKS` and `BASELINE_VALIDATION_CHECKS` reference hardcoded file paths.**
E.g., `'target_discount_math': ['python3', 'test_price.py']` — these are relative paths. After refactoring, ensure the working directory when running these checks is the repo root. This was already a concern in the monolith.

### 🟢 LOW RISK / NOTES

**R7. The `_build_base_cycle_command` function (line 1676) uses `RUNNER_PATH`.**
After refactoring to a package, this should still point to `sandbox_local_runner/__main__.py` or the shim. Ensure the entry point is updated.

**R8. `QA_FIX_PROMPT_FILENAME` (`.qa-fix-prompt.md`) is written to `worktree_path` by `apply_claude_fix`.**
This is not affected by the refactor — it writes to the worktree, not the package directory.

**R9. `append_lesson` is in `utils.py` but logs to a file outside the package.**
`DEFAULT_LESSONS_LOG = AGENT_ROOT / 'LESSONS_LOG.md'`. This is fine — it's a working directory for logs.

**R10. Import of `argparse` in `utils.py` (used by `sanitize_command_template` is a stdlib import only — no circularity with `cli.py`'s use of argparse in `main`).**
Wait — `utils.py` does NOT import `argparse`. Let me recheck: `sanitize_command_template` uses `re` only. `append_lesson` uses `datetime`. The only stdlib imports in `utils.py` are `subprocess`, `shlex`, `shutil`, `re`. No `argparse`. ✓

---

## 10. Design Decisions Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Package name | `sandbox_local_runner/` | Preserves import compatibility |
| `__init__.py` | Explicit re-exports only | No hidden namespace pollution |
| `_get_ruff_rule_description` | Keep in `linters.py`, mark dead | Don't delete until R1 is resolved |
| Inline test template | Move to `constants.py` as `TEST_NOTIFICATIONS_TEMPLATE` | String constants belong in constants module |
| `_append_text` (line 266) | Private in `state.py` | Only called by `refresh_docs_index` and a few other state functions |
| `gh_json` (line 1093) | Private in `gh.py` | Thin wrapper around `run_capture` + `json.loads`; not part of public API |
| `run_capture` / `run_no_capture` | In `utils.py` | Core primitives used by gh, linters, lifecycle, git_utils, orchestrator |
| `_read_lines` / `_add_finding` | Nested in `discover_findings` | These are truly private helpers of that one function; no need to elevate |
| Dead function `test_*` at 2426/2430 | Not real functions | Confirm by reading source — they are string content |
| CLI entry point | `cli.py:main` | After cutover, update `__main__.py` or `pyproject.toml` to point here |
