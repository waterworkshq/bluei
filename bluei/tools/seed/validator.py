"""Linter validator — validates synthesized rule candidates as an oracle.

Takes a ParsedRule candidate (before/after code), runs the relevant linter
on each, and verifies: (1) the before triggers the rule, (2) the after is
clean. The linter is the ground-truth oracle — it doesn't source the code,
it verifies correctness.

ADR-0018: the linter is an oracle, not a source.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bluei.tools.seed.models import ParsedRule


@dataclass
class ValidationResult:
    """Outcome of validating a synthesized rule candidate."""

    valid: bool
    before_triggered: bool
    after_clean: bool
    before_diagnostics: int
    after_diagnostics: int
    reason: str = ""


def validate_candidate(
    candidate: ParsedRule,
    ruff_executable: str = "ruff",
    eslint_executable: str = "eslint",
) -> ValidationResult:
    """Validate a ParsedRule candidate against its declared linter.

    Dispatches to validate_with_ruff or validate_with_eslint based on
    candidate.source_linter.
    """
    if candidate.source_linter == "ruff":
        code = _ruff_code_from_rule(candidate.rule)
        return validate_with_ruff(candidate, code, ruff_executable)
    elif candidate.source_linter == "eslint":
        return validate_with_eslint(candidate, eslint_executable)
    else:
        return ValidationResult(
            valid=False,
            before_triggered=False,
            after_clean=False,
            before_diagnostics=0,
            after_diagnostics=0,
            reason=f"unknown linter: {candidate.source_linter}",
        )


def validate_with_ruff(
    candidate: ParsedRule,
    code: str,
    ruff_executable: str = "ruff",
) -> ValidationResult:
    """Validate against ruff: before must trigger the rule, after must be clean.
    Also captures diagnostic messages and auto-detects has_autofix."""
    before_diags = _run_ruff(ruff_executable, candidate.before, code)
    after_diags = (
        _run_ruff(ruff_executable, candidate.after or "", code)
        if candidate.after
        else []
    )

    before_triggered = len(before_diags) > 0
    after_clean = len(after_diags) == 0

    # Capture diagnostic messages for detector_before
    if before_diags:
        messages = [d.get("message", "") for d in before_diags[:5]]
        candidate.detector_before = "\n".join(f"{code} {m}" for m in messages)
    candidate.detector_after = ""

    # Auto-detect has_autofix from the fix field in diagnostics
    if before_diags:
        candidate.has_autofix = any("fix" in d for d in before_diags)
    valid = before_triggered and after_clean

    reason = ""
    if not before_triggered:
        reason = f"before did not trigger ruff rule {code}"
    elif not after_clean:
        reason = f"after still has {len(after_diags)} diagnostics for rule {code}"

    return ValidationResult(
        valid=valid,
        before_triggered=before_triggered,
        after_clean=after_clean,
        before_diagnostics=len(before_diags),
        after_diagnostics=len(after_diags),
        reason=reason,
    )


def validate_with_eslint(
    candidate: ParsedRule,
    eslint_executable: str = "eslint",
) -> ValidationResult:
    """Validate against eslint: before must trigger, after must be clean.
    Captures diagnostic messages and uses flat config (eslint v9+ compatible)."""
    rule_name = candidate.rule.replace("eslint-", "")
    before_diags = _run_eslint(
        eslint_executable, candidate.before, rule_name, candidate.language
    )
    after_diags = (
        _run_eslint(
            eslint_executable, candidate.after or "", rule_name, candidate.language
        )
        if candidate.after
        else []
    )

    before_triggered = len(before_diags) > 0
    after_clean = len(after_diags) == 0

    # Capture diagnostic messages
    if before_diags:
        messages = [
            f"{d.get('ruleId', rule_name)} {d.get('message', '')}"
            for d in before_diags[:5]
        ]
        candidate.detector_before = "\n".join(messages)
    candidate.detector_after = ""

    # Auto-detect has_autofix from the fix field in diagnostics
    if before_diags:
        candidate.has_autofix = any("fix" in d for d in before_diags)

    valid = before_triggered and after_clean
    reason = ""
    if not before_triggered:
        reason = f"before did not trigger eslint rule {rule_name}"
    elif not after_clean:
        reason = f"after still has {len(after_diags)} diagnostics"

    return ValidationResult(
        valid=valid,
        before_triggered=before_triggered,
        after_clean=after_clean,
        before_diagnostics=len(before_diags),
        after_diagnostics=len(after_diags),
        reason=reason,
    )


def _run_ruff(ruff: str, code_text: str, rule_code: str) -> list:
    """Run ruff on code text, return diagnostics list."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(code_text)
        path = f.name
    try:
        cmd = [
            ruff,
            "check",
            "--select",
            rule_code,
            "--no-cache",
            "--output-format",
            "json",
            "--preview",
            path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.stdout.strip():
            return json.loads(proc.stdout)
        return []
    except Exception:
        return []
    finally:
        Path(path).unlink(missing_ok=True)


def _run_eslint(eslint: str, code_text: str, rule_name: str, language: str) -> list:
    """Run eslint on code text with a specific rule enabled via flat config.

    Uses a temp flat config file (eslint v9+ compatible) instead of
    --no-eslintrc/--rule which were removed in eslint v10.
    """
    ext = ".ts" if "typescript" in language else ".js"
    with tempfile.TemporaryDirectory() as tmpdir:
        code_path = Path(tmpdir) / f"test{ext}"
        code_path.write_text(code_text, encoding="utf-8")

        config_path = Path(tmpdir) / "eslint.config.mjs"
        config_path.write_text(
            f'export default [{{ rules: {{ "{rule_name}": "error" }} }}];\n',
            encoding="utf-8",
        )

        cmd = [eslint, "--config", str(config_path), "--format", "json", str(code_path)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15, cwd=tmpdir
            )
        except Exception:
            return []

        if not proc.stdout.strip():
            return []
        try:
            data = json.loads(proc.stdout)
            if data and isinstance(data, list):
                return data[0].get("messages", [])
        except json.JSONDecodeError:
            return []
    return []


def _ruff_code_from_rule(rule: str) -> str:
    """ruff-b904 -> B904."""
    parts = rule.split("-", 1)
    return parts[1].upper() if len(parts) == 2 else rule.upper()
