"""Validation and checking infrastructure — gates, smoke tests, fix verification."""

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.constants import DEFAULT_DOCS_INDEX
from bluei.engine.models import Finding
from bluei.engine.orchestrator import discover_findings
from bluei.engine.state import _append_text
from bluei.engine.utils import run_capture

_logger = logging.getLogger(__name__)


def _normalize_check_output(out: str, cwd: Path) -> str:
    """Normalize check output for fingerprinting: strip CWD, line numbers, and noise."""
    normalized = out.replace(str(cwd), "<CWD>")
    normalized = re.sub(r"/qa-sandbox-v2-[^/]+", "/<WORKTREE>", normalized)
    normalized = re.sub(r"line\s+\d+", "line <N>", normalized)

    filtered_lines: List[str] = []
    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^[✔✓]", line):
            continue
        if re.match(r"^\d+\s+warnings?$", line, re.IGNORECASE):
            continue
        if re.match(r"^\d+(?:\.\d+)?s$", line, re.IGNORECASE):
            continue
        filtered_lines.append(line)

    normalized = "\n".join(filtered_lines)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized[:2000]


def run_named_checks(
    repo_path: Path,
    checks: Dict[str, List[str]],
    log_file: Path,
    phase: str,
) -> Dict[str, Dict[str, Any]]:
    """Execute a dict of named check commands and return structured results.

    Args:
        repo_path: Working directory for command execution.
        checks: Mapping of check name → command (as a list of strings).
        log_file: Path to the run log.
        phase: Label for log lines (e.g. ``"validation-baseline"``).

    Returns:
        Dict of check name → ``{"rc", "cmd", "output", "fingerprint"}``.
    """
    results: Dict[str, Dict[str, Any]] = {}
    for name, cmd in checks.items():
        rc, out = run_capture(cmd, cwd=repo_path)
        normalized = _normalize_check_output(out, repo_path)
        fingerprint = (
            hashlib.sha256(normalized.encode("utf-8")).hexdigest() if rc != 0 else ""
        )
        results[name] = {
            "rc": rc,
            "cmd": cmd,
            "output": out,
            "fingerprint": fingerprint,
        }
        _append_text(log_file, f"{phase}: check={name} rc={rc} cmd={' '.join(cmd)}")
        if out:
            _append_text(log_file, f"{phase}-output[{name}]: {out[:500]}")
    return results


def build_target_checks(finding: Finding) -> Dict[str, List[str]]:
    """Build rule-specific target checks that must pass after a fix is applied.

    Args:
        finding: The Finding being fixed.

    Returns:
        Dict of check name → command. Empty dict if no target checks apply.
    """
    if finding.rule == "docs-legacy-reference":
        return {
            "target_docs_legacy_reference": [
                "python3",
                "-c",
                (
                    f'from pathlib import Path; text = Path("{finding.path}").read_text(encoding="utf-8"); '
                    'raise SystemExit(0 if "legacy_pricer.py" not in text else 1)'
                ),
            ]
        }

    if (
        finding.rule == "test-gap-missing-case"
        and finding.path == "tests/test_orders.py"
    ):
        return {
            "target_test_gap_orders_case": [
                "python3",
                "-c",
                (
                    'from pathlib import Path; text = Path("tests/test_orders.py").read_text(encoding="utf-8").lower(); '
                    'raise SystemExit(0 if ("invalid" in text and "coupon" in text) else 1)'
                ),
            ]
        }

    # Default: no target-specific checks for rules not listed above.
    # Must return {} (not None) — render_claude_fix_prompt() calls .items() on target_checks.
    return {}


def run_validation_gate(
    repo_path: Path,
    worktree_path: Path,
    checks: Optional[Dict[str, List[str]]],
    baseline_results: Optional[Dict[str, Dict[str, Any]]] = None,
    post_fix_results: Optional[Dict[str, Dict[str, Any]]] = None,
    target_results: Optional[Dict[str, Dict[str, Any]]] = None,
    allow_unchanged_baseline_failures: bool = True,
    log_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run baseline + post-fix + target checks and return structured result.

    Args:
        repo_path: Repository root path.
        worktree_path: Worktree path.
        checks: Dict of check-name → command list. If None, runs no checks.
        baseline_results: Optional pre-computed baseline results.
        post_fix_results: Optional pre-computed post-fix results.
        target_results: Optional pre-computed target results.
        allow_unchanged_baseline_failures: If True, baseline check failures
            that are unchanged post-fix are not treated as regressions.
        log_file: Optional log file path.

    Returns:
        Dict with keys:
            - passed: bool
            - message: str (empty on success)
            - regressions: List[str] (names of checks that regressed)
            - target_failures: List[str] (names of target checks that failed)
    """
    result: Dict[str, Any] = {
        "passed": True,
        "message": "",
        "regressions": [],
        "target_failures": [],
        "baseline_results": {},
        "post_results": {},
    }

    if checks is None:
        checks = {}

    phase_prefix = "validation"
    if log_file is None:
        log_file = Path("/dev/null")

    # Run baseline checks (or reuse pre-computed results)
    if baseline_results is not None:
        result["baseline_results"] = baseline_results
    else:
        result["baseline_results"] = run_named_checks(
            repo_path=repo_path,
            checks=checks,
            log_file=log_file,
            phase=f"{phase_prefix}-baseline",
        )

    # Run post-fix checks (or reuse pre-computed results)
    if post_fix_results is not None:
        result["post_results"] = post_fix_results
    else:
        result["post_results"] = run_named_checks(
            repo_path=repo_path,
            checks=checks,
            log_file=log_file,
            phase=f"{phase_prefix}-postfix",
        )

    # Run target checks (or reuse pre-computed results)
    if target_results is not None:
        pass
    elif checks:
        target_results = run_named_checks(
            repo_path=repo_path,
            checks=checks,
            log_file=log_file,
            phase=f"{phase_prefix}-target",
        )
    else:
        target_results = {}

    # Evaluate regressions: a check that passed at baseline but failed post-fix is a regression
    for name, baseline in result["baseline_results"].items():
        post = result["post_results"].get(name, {"rc": 1, "fingerprint": "missing"})
        baseline_rc = int(baseline.get("rc", 1))
        post_rc = int(post.get("rc", 1))
        if baseline_rc == 0 and post_rc != 0:
            result["regressions"].append(name)

    target_failures = [n for n, r in target_results.items() if int(r.get("rc", 1)) != 0]
    if target_failures:
        result["target_failures"] = target_failures

    if result["regressions"]:
        result["passed"] = False
        result["message"] = f"regression in: {', '.join(result['regressions'])}"
        _append_text(log_file, f"validation-gate: FAIL {result['message']}")
        return result

    if result["target_failures"]:
        result["passed"] = False
        result["message"] = (
            f"target checks failed: {', '.join(result['target_failures'])}"
        )
        _append_text(log_file, f"validation-gate: FAIL {result['message']}")
        return result

    result["message"] = "all checks passed"
    _append_text(log_file, "validation-gate: PASS")
    return result


def run_smoke_test(repo_path: Path, log_file: Path) -> Dict[str, Any]:
    """Run a cheap pre-flight smoke test on a registered repo.

    Args:
        repo_path: Path to the repository to smoke-test.
        log_file: Path to the run log.

    Returns:
        Dict with keys:
            passed: bool  (True only if all checks pass)
            checks: dict of {name: bool}
            duration_ms: int  (total wall-clock time)
            errors: List[str]  (one per failed check)
    """
    import time

    errors: list[str] = []
    checks: dict[str, bool] = {
        "git": False,
        "worktree": False,
        "linter": False,
    }
    t0 = time.monotonic()

    # --- Check 1: repo path exists and is a git repository ---
    t_git = time.monotonic()
    if not repo_path.exists():
        errors.append(f"repo path does not exist: {repo_path}")
    elif not (repo_path / ".git").exists() and not repo_path.is_dir():
        errors.append(f"not a directory or missing .git: {repo_path}")
    else:
        rc, out = run_capture(["git", "rev-parse", "--git-dir"], cwd=repo_path)
        if rc == 0:
            checks["git"] = True
        else:
            errors.append(f"git rev-parse failed rc={rc} out={out[:200]}")
    _append_text(
        log_file,
        f"smoke-test: git-check passed={checks['git']} duration_ms={int((time.monotonic() - t_git) * 1000)}",
    )

    # --- Check 2: worktree is clean ---
    t_wt = time.monotonic()
    rc, out = run_capture(["git", "status", "--porcelain"], cwd=repo_path)
    if rc == 0:
        if not out.strip():
            checks["worktree"] = True
        else:
            dirty_count = len([l for l in out.splitlines() if l.strip()])
            errors.append(f"worktree is dirty: {dirty_count} uncommitted change(s)")
    else:
        errors.append(f"git status --porcelain failed rc={rc} out={out[:200]}")
    _append_text(
        log_file,
        f"smoke-test: worktree-check passed={checks['worktree']} duration_ms={int((time.monotonic() - t_wt) * 1000)}",
    )

    # --- Check 3: linter (ruff) is available and can analyze a .py file ---
    t_li = time.monotonic()
    # Discover ruff using the same strategy as discover_python_linter_findings
    ruff_path: str | None = None
    for candidate in ["ruff", "~/.local/bin/ruff"]:
        expanded = Path(candidate).expanduser()
        if expanded.exists():
            ruff_path = str(expanded)
            break
    if ruff_path is None:
        import shutil as _shutil

        found = _shutil.which("ruff")
        if found:
            ruff_path = found

    if ruff_path is None:
        checks["linter"] = False
        errors.append("ruff not found — cannot run linter check")
    else:
        # Find a single .py file to lint quickly
        py_files = list(repo_path.rglob("*.py"))
        if not py_files:
            checks["linter"] = True  # no Python files = trivially OK
        else:
            # Lint just one small file for a quick check
            target = py_files[0]
            rc, out = run_capture(
                [
                    ruff_path,
                    "check",
                    str(target),
                    "--select=B,E,W,F,S,C4",
                    "--output-format=concise",
                ],
                cwd=repo_path,
            )
            # rc=0 means no issues, rc=1 means issues found but linter works
            if rc in (0, 1):
                checks["linter"] = True
            else:
                errors.append(f"ruff check failed rc={rc} out={out[:200]}")
    _append_text(
        log_file,
        f"smoke-test: linter-check passed={checks['linter']} duration_ms={int((time.monotonic() - t_li) * 1000)}",
    )

    t1 = time.monotonic()
    duration_ms = int((t1 - t0) * 1000)
    passed = all(checks.values())

    result: Dict[str, Any] = {
        "passed": passed,
        "checks": checks,
        "duration_ms": duration_ms,
        "errors": errors,
    }

    _append_text(
        log_file,
        f"smoke-test: result passed={passed} duration_ms={duration_ms} "
        f"git={checks['git']} worktree={checks['worktree']} linter={checks['linter']}",
    )
    if errors:
        for err in errors:
            _append_text(log_file, f"smoke-test: error {err}")

    return result


def verify_fix_closed(
    worktree_path: Path,
    finding: Finding,
    log_file: Path,
    docs_index_file: Path = DEFAULT_DOCS_INDEX,
) -> bool:
    """Re-scan the worktree and confirm the finding no longer fires.

    Args:
        worktree_path: Path to the worktree to re-scan.
        finding: The Finding that should be resolved.
        log_file: Path to the run log.
        docs_index_file: Path to the docs index used by the discovery engine.

    Returns:
        True if the finding is gone (fix closed), False if it still fires.
    """
    rescanned = discover_findings(worktree_path, docs_index_file=docs_index_file)
    still_firing = [
        f
        for f in rescanned
        if (
            f.finding_id == finding.finding_id
            or (
                f.rule == finding.rule
                and f.path == finding.path
                and int(f.line) == int(finding.line)
            )
        )
    ]
    _append_text(
        log_file,
        f"verification: finding_id={finding.finding_id} rule={finding.rule} "
        f"path={finding.path} line={finding.line} still_firing={len(still_firing)}",
    )
    return len(still_firing) == 0


def choose_validation_baseline(
    repo_baseline_results: Dict[str, Dict[str, Any]],
    worktree_baseline_results: Dict[str, Dict[str, Any]],
    log_file: Path,
) -> Dict[str, Dict[str, Any]]:
    """Choose between repo-level and worktree-level baseline results."""
    if worktree_baseline_results:
        first_val = next(iter(worktree_baseline_results.values()), None)
        if first_val is not None and first_val.get("rc") is not None:
            _append_text(
                log_file, "validation-baseline: using worktree-specific baseline"
            )
            return worktree_baseline_results
    _append_text(log_file, "validation-baseline: using repo-level baseline")
    return repo_baseline_results
