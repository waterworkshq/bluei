"""Validation and checking infrastructure — gates, smoke tests, fix verification."""

import hashlib
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.constants import DEFAULT_DOCS_INDEX, DETECTOR_CATALOG
from bluei.engine.models import Finding
from bluei.engine.state import _append_text, canonical_markdown_rule
from bluei.engine.utils import run_capture

# Exit-code sentinel for a check that never executed (missing executable,
# timeout, invalid argv, missing script). Always paired with available=False;
# a genuine OS-signal death (e.g. SIGHUP, rc=-1) keeps available=True and its
# failure fingerprint — the available marker disambiguates, rc alone never does.
RC_UNAVAILABLE = -1


def _get_default_discover_fn():
    """Lazy import of discover_findings from orchestrator (avoids load-time coupling)."""
    from bluei.engine.orchestrator import discover_findings

    return discover_findings


_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Responsible-detector evidence
# ---------------------------------------------------------------------------
# A detector that cannot report a rule has run: that is not evidence the rule
# stopped firing. Minimal rule-family -> producer mapping over the exact
# detector outcome keys discovery records; NOT a registry framework.

# Rule-family prefixes -> detector outcome keys able to produce that family.
# ruff- is special-cased below by exact core-catalog code membership.
_RULE_FAMILY_DETECTORS: Dict[str, tuple] = {
    "test-coverage-": ("test-coverage",),
    # type-* are tsc strict-mode diagnostics: only the compiler detector can
    # re-report them (the XO plugin's heuristics are not closure evidence for
    # a compiler finding). ts-* come only from the TypeScript AST detector —
    # plugin-typescript emits no ts- rules; unmapped external rule IDs stay bare.
    "type-": ("typescript-types",),
    "ts-": ("ast-typescript",),
    "xo-": ("xo-linter", "plugin:plugin-typescript"),
    "go-": ("ast-go", "plugin:plugin-go"),
    "rust-": ("ast-rust", "plugin:plugin-rust"),
    "clippy-": ("plugin:plugin-rust",),
    "shell-": ("plugin:plugin-shell",),
    "docker-": ("plugin:plugin-dockerfile",),
    "md-": ("plugin:plugin-markdown",),
}
# Core python-linter-ruff filters any code not in this exact catalog set;
# those rules are produced only by plugin-python (which emits ruff-<code>
# verbatim, e.g. ruff-F821).
_CORE_RUFF_RULES = frozenset(
    str(entry["rule"]).lower()
    for entry in DETECTOR_CATALOG
    if str(entry["rule"]).lower().startswith("ruff-")
)
# Exact built-in rule ids -> their single producer. Not a broad catalog
# default: docs-index backed doc-gap-uncovered-module/doc-drift-stale-reference
# have NO dedicated outcome key (sandbox-string-detectors "ran" says nothing
# about the index scan), so they stay conservatively unknown/open.
_AST_PYTHON_RULES = frozenset({
    "broad-except", "catalog-query-not-normalized", "discount-math-sign",
    "hardcoded-tmp-path", "hardcoded-tmp-path-string", "lock-self-reentry",
    "notifications-email-no-trim", "notifications-type-guard-missing",
    "perf-list-membership-loop", "perf-pop-front-loop",
})
_SANDBOX_STRING_RULES = frozenset({
    "orders-tax-truncation", "inventory-invalid-quantity", "debt-todo-marker",
    "trailing-whitespace", "docs-legacy-reference", "docs-missing-rollback",
    "docs-quickstart-gap", "test-gap-missing-file", "test-gap-missing-case",
})
def responsible_detector_ran(rule: str, outcomes: Dict[str, Dict[str, Any]]) -> bool:
    """True only if a detector that can actually report ``rule`` ran.

    Unknown rules (no known producer) never qualify: findings absent from an
    unrelated detector are not closure evidence for them — they stay open.
    """
    rule_lower = str(rule).lower()
    if rule == "no-console":
        # Exact bare tool rule, not a default for arbitrary external rule IDs.
        producer = outcomes.get("plugin:plugin-typescript", {})
        return producer.get("status") == "ran" and producer.get("tool_outcome") == "tool-ran"
    detectors = None
    if rule_lower.startswith("ruff-"):
        detectors = (
            ("python-linter-ruff", "plugin:plugin-python")
            if rule_lower in _CORE_RUFF_RULES
            else ("plugin:plugin-python",)
        )
    else:
        for prefix, keys in _RULE_FAMILY_DETECTORS.items():
            if rule_lower.startswith(prefix):
                detectors = keys
                break
    # markdownlint emits bare codes ("MD009") alongside prefixed "md-MD009".
    if detectors is None and (
        rule_lower.startswith("md") and rule_lower[2:3].isdigit()
    ):
        detectors = _RULE_FAMILY_DETECTORS["md-"]
    if detectors is None and rule_lower in _AST_PYTHON_RULES:
        detectors = ("ast-python",)
    if detectors is None and rule_lower in _SANDBOX_STRING_RULES:
        detectors = ("sandbox-string-detectors",)
    if detectors is None:
        return False
    return any(outcomes.get(d, {}).get("status") == "ran" for d in detectors)


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


# CPython interpreter flags this pre-check understands. Anything else in
# flag position is "unsupported-invocation": an unknown flag may consume a
# value and shift the script position, which would let a missing script be
# reported as an executed failure (false availability). Fail closed instead.
_PY_FLAGS_NOARG = frozenset(
    "-b -B -d -E -i -I -O -OO -P -q -R -s -S -u -v -x".split()
)
_PY_FLAGS_VALUE = frozenset({"-W", "-X"})  # value as the NEXT token
# Flags that terminate the interpreter without a script and without reading
# stdin — reaching end-of-args after ONLY non-terminating flags is an
# interactive REPL/sticky-stdin invocation (would hang until timeout).
_PY_FLAGS_TERMINATING = frozenset({"-V", "-VV", "-h", "--help", "--version"})


_MODULE_PROBE_CODE = (
    "import importlib.util,sys; "
    "raise SystemExit(0 if importlib.util.find_spec(sys.argv[1]) is not None else 1)"
)


def _module_unresolvable(exe: str, flags: List[str], module: str, cwd: Path) -> bool:
    """True when ``-m module`` fails a same-interpreter find_spec probe.

    Exit code only (no output sniffing); flags/cwd/env preserved. find_spec
    may import the module's parent package — accepted risk, exercised only
    in isolated test repos. Any probe failure fails closed to unavailable.
    ponytail: probe failure of any kind maps to "missing-module"; upgrade
    path distinguishes probe errors from definitive not-found.
    """
    try:
        rc, _ = run_capture(
            [exe, *flags, "-c", _MODULE_PROBE_CODE, module], cwd=cwd
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return True
    return rc != 0


def _python_invocation_reason(cmd: List[str], cwd: Path) -> Optional[str]:
    """Strict minimal CPython argv classification (see reason codes below).

    Non-python launchers are NOT classified — they execute with their real
    rc (no wrapper framework, no output sniffing). Unknown python flags,
    no-workload (REPL/stdin/version), missing operands, missing/unfile
    scripts, and unresolvable ``-m`` modules are unavailable, fail closed.
    ponytail: ceiling is the CPython flag set above; future flags fall to
    unsupported-invocation. Upgrade path: launcher classification table.
    """
    args = cmd[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-c", "-m"):
            operand = args[i + 1] if i + 1 < len(args) else None
            if operand is None or operand == "":
                return "invalid-argv"  # flag missing its code/module value
            if arg == "-m":
                if operand.startswith("-"):
                    return "invalid-argv"
                if _module_unresolvable(cmd[0], args[:i], operand, cwd):
                    return "missing-module"
            return None  # later tokens are code/module argv, not a file
        if arg in _PY_FLAGS_VALUE:
            if i + 1 >= len(args):
                return "invalid-argv"  # flag missing its value
            i += 2
            continue
        if arg in _PY_FLAGS_TERMINATING:
            # No workload (--version/--help): nothing to qualify.
            return "unsupported-invocation"
        if arg in _PY_FLAGS_NOARG:
            i += 1
            continue
        if re.match(r"^-[WX].+", arg):  # attached value: -Xutf8, -Werror
            i += 1
            continue
        if arg == "--":
            nxt = args[i + 1] if i + 1 < len(args) else None
            if nxt is None or nxt == "-":
                return "unsupported-invocation"  # stdin script: may hang
            if not (cwd / nxt).is_file():
                return "missing-script"
            return None
        if arg.startswith("-"):
            return "unsupported-invocation"
        if arg == "-":
            return "unsupported-invocation"  # stdin script: may hang
        if not (cwd / arg).is_file():
            return "missing-script"
        return None
    # Flags only, no script positional: interactive REPL/sticky stdin.
    return "unsupported-invocation"


# Bare Python-tool names the runtime-shadowing guard covers. pythonX.Y is
# matched by the existing name.startswith("python") check. poetry/uv and
# unknown wrapper commands are explicitly unsupported by the guard — they
# keep their existing availability verdicts, no fallback is invented.
_VENV_GUARDED_TOOLS = frozenset({"pytest", "ruff", "mypy"})


def _venv_shadow_reason(
    exe_name: str, cwd: Path, source_repo: Optional[Path]
) -> Optional[str]:
    """Bare-tool runtime-shadowing guard: fail closed, never rewrite.

    A declared local venv (``.venv``/``venv`` existing at all in the
    environment repo — ``source_repo`` or ``cwd`` — broken root symlinks and
    a missing ``bin/`` included) defines the intended runtime; a bare name
    whose ``shutil.which`` result lives outside that venv's lexical ``bin``
    would launch a foreign environment — venv ``python`` symlinks to the
    host binary yet changes ``sys.prefix``, so identity is the lexical bin
    dir (never ``Path.resolve``). Returns ``runtime-shadowing``;
    explicit/absolute commands never reach this guard.
    ponytail: POSIX ``bin`` only; no interpreter-version or ``.pth`` probing
    (absolute interpreter never proves import provenance).
    """
    env = source_repo if source_repo is not None else cwd
    for venv_dir in (".venv", "venv"):
        if not os.path.lexists(env / venv_dir):
            continue
        bin_dir = Path(os.path.abspath(env / venv_dir / "bin"))
        if Path(os.path.abspath(shutil.which(exe_name))).parent == bin_dir:
            return None  # resolved into the same interpreter environment
        return "runtime-shadowing"  # declared venv: refuse foreign runtime
    return None


def _invocation_unavailable_reason(
    cmd: Any, cwd: Path, source_repo: Optional[Path] = None
) -> Optional[str]:
    """Narrow invocation-prerequisite check before spending a subprocess.

    Returns an ``unavailable_reason`` string, or None when the invocation
    looks runnable. Covers only: argv shape (incl. NUL bytes), argv[0]
    existence/executability resolved like subprocess does (relative paths
    with a slash resolve against ``cwd``, not the process cwd), the CPython
    ``script.py`` positional, and — for bare python/pytest/ruff/mypy only —
    the local-venv runtime-shadowing guard against ``source_repo`` (defaults
    to ``cwd``). Explicit operator commands (any argv[0] containing "/",
    including inferred absolute venv ``python -m`` invocations) are honored
    as given and never shadow-checked. No stderr sniffing, no output
    heuristics — a check that actually executes always keeps its real rc.
    """
    if (
        not isinstance(cmd, list)
        or not cmd
        or not all(isinstance(a, str) for a in cmd)
    ):
        return "invalid-argv"
    if any("\0" in a for a in cmd):
        return "invalid-argv"
    exe = cmd[0]
    if "/" in exe:
        exe_path = Path(exe) if os.path.isabs(exe) else (cwd / exe)
        if not exe_path.exists() or exe_path.is_dir():
            return "missing-executable"
        if not os.access(exe_path, os.X_OK):
            return "not-executable"
    elif shutil.which(exe) is None:
        return "missing-executable"
    name = Path(exe).name
    if "/" not in exe and (
        name.startswith("python") or name in _VENV_GUARDED_TOOLS
    ):
        shadow = _venv_shadow_reason(name, cwd, source_repo)
        if shadow is not None:
            return shadow
    if name.startswith("python"):
        return _python_invocation_reason(cmd, cwd)
    return None


def run_named_checks(
    repo_path: Path,
    checks: Dict[str, List[str]],
    log_file: Path,
    phase: str,
    environment_repo: Optional[Path] = None,
) -> Dict[str, Dict[str, Any]]:
    """Execute a dict of named check commands and return structured results.

    A check whose invocation prerequisites are missing (missing/not-executable
    executable, invalid argv, missing Python script) or that times out is
    recorded as *unavailable evidence*: ``rc=RC_UNAVAILABLE``,
    ``available=False`` with a closed-vocabulary ``unavailable_reason``. It
    never counts as an executed pass or failure. A check that executes keeps
    its real rc regardless of outcome — a genuine executed baseline failure
    still gets a fingerprint for the unchanged-failure policy.

        Args:
            repo_path: Working directory for command execution.
            checks: Mapping of check name → command (as a list of strings).
            log_file: Path to the run log.
            phase: Label for log lines (e.g. ``"validation-baseline"``).
            environment_repo: Repo whose local ``.venv``/``venv`` defines the
                intended tool runtime for the bare-name shadowing guard
                (defaults to ``repo_path``). Pass the registered source repo
                when executing from a linked worktree that has no hydrated
                venv of its own.

    Returns:
        Dict of check name → ``{"rc", "available", "unavailable_reason",
        "cmd", "output", "fingerprint"}``. ``cmd``/``output`` are private
        diagnostics — do not forward them into model prompts. ``available``
        is a runtime marker set only here: result dicts crafted by hand
        (tests, callers) never carry runtime-availability proof; it marks
        launch prerequisites met and the process executed, not check quality.
    """
    results: Dict[str, Dict[str, Any]] = {}
    for name, cmd in checks.items():
        reason = _invocation_unavailable_reason(cmd, repo_path, environment_repo)
        rc: Optional[int] = None
        out = ""
        if reason is None:
            try:
                rc, out = run_capture(cmd, cwd=repo_path)
            except subprocess.TimeoutExpired:
                reason = "timeout"
            except (FileNotFoundError, NotADirectoryError):
                reason = "missing-executable"
            except PermissionError:
                reason = "not-executable"
            except (TypeError, ValueError):
                reason = "invalid-argv"
            except OSError:
                # e.g. ENOEXEC (exec format error): the launch itself failed.
                reason = "execution-unavailable"
        available = reason is None
        if not available:
            rc = RC_UNAVAILABLE
            out = ""
        normalized = _normalize_check_output(out, repo_path)
        fingerprint = (
            hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if available and rc != 0
            else ""
        )
        results[name] = {
            "rc": rc,
            "available": available,
            "unavailable_reason": reason,
            "cmd": cmd,
            "output": out,
            "fingerprint": fingerprint,
        }
        cmd_repr = (
            " ".join(cmd)
            if isinstance(cmd, list) and all(isinstance(a, str) for a in cmd)
            else repr(cmd)
        )
        _append_text(
            log_file,
            f"{phase}: check={name} rc={rc} available={available}"
            + (f" unavailable_reason={reason}" if reason else "")
            + f" cmd={cmd_repr}",
        )
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
                    'import sys; from pathlib import Path; text = Path(sys.argv[1]).read_text(encoding="utf-8"); '
                    'raise SystemExit(0 if "legacy_pricer.py" not in text else 1)'
                ),
                finding.path,
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
            - message: str (empty on success; a pass without executed
              evidence says ``unqualified: ...`` instead of claiming success)
            - regressions: List[str] (names of checks that regressed)
            - target_failures: List[str] (names of target checks that failed)
            - qualified: bool — True only when checks were configured AND every
              configured name is present with runtime ``available=True`` in
              BOTH baseline and post results (the gate generates those itself
              when the caller passes None) AND at least one configured check
              actually PASSES (rc==0, runtime-available) in both baseline and
              post — a passing control. Target results are optional for
              qualification. Empty/None checks keep ``passed=True`` (no-check
              diagnostic contract) but are ``qualified=False``: an empty check
              set must never qualify autonomous repair. Hand-crafted result
              dicts without the runtime marker are deliberately not qualified.
              An all-failing (merely tolerated) baseline is unqualified: it
              cannot distinguish "checks ran and pass" from "wrappers never
              ran". Stage-1 semantic ceiling, stated honestly: a passing
              control does NOT prove the quality of arbitrary check commands;
              that needs a runner framework this gate does not provide.
            - checks_configured: int (number of configured check names)
            - checks_available: int (observed names with all-executed evidence)
            - checks_passed: int (configured names passing with runtime
              evidence in BOTH baseline and post — the passing control count)
            - unavailable: List[str] (names with missing/unavailable evidence)
            - tolerated_failures: List[str] (executed baseline failures whose
              rc and fingerprint are unchanged post-fix — real evidence,
              tolerated only alongside a passing control, and the message
              never claims "all checks passed" over them)
    """
    result: Dict[str, Any] = {
        "passed": True,
        "message": "",
        "regressions": [],
        "target_failures": [],
        "baseline_results": {},
        "post_results": {},
        "qualified": False,
        "checks_configured": 0,
        "checks_available": 0,
        "checks_passed": 0,
        "unavailable": [],
        "tolerated_failures": [],
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

    # Availability/qualification accounting. `available` is a runtime marker
    # produced only by run_named_checks; a hand-crafted result dict (no
    # marker) is deliberately NOT runtime proof — it can drive the low-level
    # passed/non-regression math but never qualifies autonomous repair.
    # Qualification requires EVERY configured check name to be present and
    # runtime-available in BOTH baseline and post results (the gate itself
    # generates those when the caller passes None). Target results are
    # optional for qualification. An empty observed set with non-empty
    # configured checks therefore never qualifies.
    def _is_available(r: Any) -> bool:
        return isinstance(r, dict) and r.get("available") is True

    def _is_passing_control(r: Any) -> bool:
        rc = r.get("rc") if isinstance(r, dict) else None
        return _is_available(r) and isinstance(rc, int) and not isinstance(rc, bool) and rc == 0

    unavailable: List[str] = []
    for name in checks:
        if not _is_available(result["baseline_results"].get(name)):
            if name not in unavailable:
                unavailable.append(name)
        if not _is_available(result["post_results"].get(name)):
            if name not in unavailable:
                unavailable.append(name)
    for source in (result["baseline_results"], result["post_results"], target_results):
        for name, r in source.items():
            if not _is_available(r) and name not in unavailable:
                unavailable.append(name)
    observed = list(
        dict.fromkeys(
            [
                *result["baseline_results"],
                *result["post_results"],
                *target_results,
            ]
        )
    )
    # Passing control (conservative default): at least one CONFIGURED check
    # must actually pass with runtime evidence in BOTH baseline and post.
    # Tolerated unchanged failures are permitted only alongside it; an
    # all-failing baseline cannot distinguish "ran and failed before" from
    # "wrappers never ran", so it stays unqualified.
    result["checks_configured"] = len(checks)
    result["unavailable"] = unavailable
    result["checks_available"] = len([n for n in observed if n not in unavailable])
    result["checks_passed"] = len(
        [
            name
            for name in checks
            if _is_passing_control(result["baseline_results"].get(name))
            and _is_passing_control(result["post_results"].get(name))
        ]
    )
    result["qualified"] = bool(checks) and not unavailable and result["checks_passed"] >= 1

    # A pre-existing failure is tolerated only when policy allows it and its
    # exit code and nonempty failure fingerprint are unchanged. Unavailable
    # evidence (rc sentinel, empty fingerprint) is never an unchanged failure,
    # so it surfaces as a regression rather than fabricated proof.
    def _rc_of(r: Any) -> int:
        rc = r.get("rc") if isinstance(r, dict) else None
        return rc if isinstance(rc, int) and not isinstance(rc, bool) else 1

    for name in dict.fromkeys([*result["baseline_results"], *result["post_results"]]):
        baseline = result["baseline_results"].get(name, {})
        post = result["post_results"].get(name, {})
        baseline_rc = _rc_of(baseline)
        post_rc = _rc_of(post)
        unchanged_failure = (
            allow_unchanged_baseline_failures and baseline_rc != 0
            and baseline_rc == post_rc and baseline.get("fingerprint")
            and baseline["fingerprint"] == post.get("fingerprint")
        )
        if unchanged_failure:
            result["tolerated_failures"].append(name)
        if not post or (post_rc != 0 and not unchanged_failure):
            result["regressions"].append(name)

    target_failures = [n for n, r in target_results.items() if _rc_of(r) != 0]
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

    if not result["qualified"]:
        # passed keeps its low-level legacy meaning, but a pass that is not
        # executed-evidence-backed must say so — it can never qualify
        # autonomous repair.
        if not checks:
            result["message"] = "unqualified: no checks configured"
        elif unavailable:
            def _reason_for(name: str) -> str:
                for source in (
                    result["baseline_results"], result["post_results"], target_results,
                ):
                    r = source.get(name)
                    if isinstance(r, dict):
                        if r.get("unavailable_reason"):
                            return str(r["unavailable_reason"])
                        if not _is_available(r):
                            return "no-runtime-marker"
                return "missing"

            detail = ", ".join(
                f"{n}({_reason_for(n)})" for n in result["unavailable"]
            )
            result["message"] = f"unqualified: {detail or 'no executed evidence'}"
        elif result["checks_passed"] == 0:
            result["message"] = "unqualified: no passing configured check"
        else:
            result["message"] = "unqualified: no executed evidence"
        _append_text(log_file, f"validation-gate: PASS-UNQUALIFIED {result['message']}")
        return result

    if result["tolerated_failures"]:
        result["message"] = (
            "checks passed with tolerated pre-existing failures: "
            + ", ".join(result["tolerated_failures"])
        )
        _append_text(log_file, f"validation-gate: PASS {result['message']}")
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
    discover_fn: Any = None,
    discovery_scope: str = "repository",
) -> bool:
    """Re-scan the worktree and require the rule to be absent from the file.

    Line numbers and finding IDs can change after unrelated edits, so closure
    is conservative when another instance of the same rule remains in the file.

    Args:
        worktree_path: Path to the worktree to re-scan.
        finding: The Finding that should be resolved.
        log_file: Path to the run log.
        docs_index_file: Path to the docs index used by the discovery engine.
        discover_fn: Optional discovery function. If None, lazily imports
            ``discover_findings`` from ``bluei.engine.orchestrator``.
        discovery_scope: ``"repository"`` (default) or ``"python"``. Under the
            python scope an out-of-scope (non-``.py``) finding is refused up
            front — before any custom ``discover_fn`` is injected — and the
            rescan runs the python-only discovery branch with the same
            responsible-producer gate.

    Returns:
        True if the finding is gone (fix closed), False if it still fires.
    """
    if discovery_scope not in ("repository", "python"):
        raise ValueError(
            f"unknown discovery_scope {discovery_scope!r}; "
            "expected 'repository' or 'python'"
        )

    if discovery_scope == "python":
        if str(finding.rule).casefold() == "ruff-e722":
            _append_text(log_file, "verification: raw E722 repair is outside this bounded Python scope")
            return False
        # A Python path alone cannot make an unrelated rule verifiable.
        python_producers_ran = {
            "ast-python": {"status": "ran"},
            "python-linter-ruff": {"status": "ran"},
            "plugin:plugin-python": {"status": "ran"},
        }
        if not str(finding.path).endswith(".py") or not responsible_detector_ran(
            finding.rule, python_producers_ran
        ):
            _append_text(
                log_file,
                "verification: refusing out-of-scope finding "
                f"path={finding.path} rule={finding.rule}; python discovery "
                "scope cannot verify it (path outside scope or rule has no "
                "python producer); finding remains open",
            )
            return False

        if discover_fn is not None:
            # An injected rescan carries no detector outcomes, so neither the
            # completion gate nor the responsible-producer gate can run on
            # it — its result is not closure evidence. Fail closed instead of
            # trusting the injected callback. (The default-producer path
            # above keeps its full gate.) The repository scope still accepts
            # injected callbacks for test compatibility.
            _append_text(
                log_file,
                "verification: refusing injected discover_fn under python "
                f"discovery scope (rule={finding.rule}); an ungated rescan "
                "is not closure evidence; finding remains open",
            )
            return False

    if discover_fn is None:
        from bluei.engine.orchestrator import discover_findings as _default_discover

        outcomes: Dict[str, Dict[str, Any]] = {}
        rescan_kwargs: Dict[str, Any] = (
            {"discovery_scope": discovery_scope}
            if discovery_scope != "repository"
            else {}
        )
        rescanned = _default_discover(
            worktree_path, docs_index_file=docs_index_file, outcomes=outcomes,
            **rescan_kwargs,
        )
        if any(
            outcome.get("status") in ("failed", "unavailable")
            for outcome in outcomes.values()
            if isinstance(outcome, dict)
        ):
            # Scan-wide completion gate (mirrors the runner's scan_complete
            # rule): a failed/unavailable detector means the rescan is
            # incomplete, so rule absence is not closure evidence — even when
            # the responsible detector itself ran.
            _append_text(
                log_file,
                "verification: incomplete discovery; a detector failed or was "
                f"unavailable (rule={finding.rule}); finding remains open",
            )
            return False
        if not responsible_detector_ran(finding.rule, outcomes):
            # A detector that cannot report this rule ran (or none did):
            # absence from the rescan is not closure evidence.
            _append_text(
                log_file,
                "verification: incomplete discovery; no detector responsible "
                f"for rule={finding.rule} ran; finding remains open",
            )
            return False
    else:
        if discovery_scope != "repository":
            rescanned = discover_fn(
                worktree_path,
                docs_index_file=docs_index_file,
                discovery_scope=discovery_scope,
            )
        else:
            rescanned = discover_fn(worktree_path, docs_index_file=docs_index_file)
    still_firing = [
        f
        for f in rescanned
        if (
            f.finding_id == finding.finding_id
            or (
                (canonical_markdown_rule(f.rule) or f.rule)
                == (canonical_markdown_rule(finding.rule) or finding.rule)
                and f.path == finding.path
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
    """Choose between repo-level and worktree-level baseline results.

    The worktree baseline is selected whenever the worktree checks actually
    ran — including when every worktree check was UNAVAILABLE (runtime
    ``available`` marker present, ``rc=RC_UNAVAILABLE``). A healthy
    repo-level baseline must never be borrowed as cover for a broken
    worktree environment. Legacy hand-crafted dicts with an explicit ``rc``
    keep the old selection behavior; only a worktree dict with neither the
    marker nor an rc (not actually run) falls back to the repo baseline.
    """
    if worktree_baseline_results:
        first_val = next(iter(worktree_baseline_results.values()), None)
        if isinstance(first_val, dict) and (
            "available" in first_val or first_val.get("rc") is not None
        ):
            _append_text(
                log_file, "validation-baseline: using worktree-specific baseline"
            )
            return worktree_baseline_results
    _append_text(log_file, "validation-baseline: using repo-level baseline")
    return repo_baseline_results
