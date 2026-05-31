"""Tests for bluei.engine.linters — linter output -> Finding objects.

Verifies realistic linter output produces correct Finding objects.
External tools (ruff, xo, tsc) are mocked; coverage uses real JSON files.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.linters import (
    discover_python_linter_findings,
    discover_test_coverage_findings,
    discover_typescript_type_findings,
    discover_xo_linter_findings,
    run_xo_linter_in_container,
)
from bluei.engine.models import Finding

# ── Compact helpers ──────────────────────────────────────────────


def _log(tmp: Path) -> Path:
    """Create log file."""
    p = tmp / "t.log"
    p.touch()
    return p


def _ruff(code, row, path, msg="x", app=None):
    """Build one ruff JSON result entry."""
    r = {"code": code, "message": msg, "location": {"row": row}, "filename": path}
    if app:
        r["fix"] = {"applicability": app}
    return r


def _ruff_se(items, rc=1):
    """subprocess.run side-effect: 'which ruff' -> path, then ruff check -> JSON."""

    def run(args, **kw):
        if "which" in args:
            return MagicMock(returncode=0, stdout="/usr/bin/ruff")
        return MagicMock(returncode=rc, stdout=json.dumps(items))

    return run


def _xo_repo(tmp: Path) -> Path:
    (tmp / "package.json").write_text(json.dumps({"devDependencies": {"xo": "^0.50"}}))
    return _log(tmp)


def _xo_msg(rule, msg="x", line=1, col=1, sev=1):
    return {
        "ruleId": rule,
        "message": msg,
        "line": line,
        "column": col,
        "severity": sev,
    }


def _covf(br=None, fn=None, sm=None, hits=None):
    """Coverage data for one file (fills defaults)."""
    return {
        "branches": br or {},
        "functions": fn or {},
        "statementMap": sm or {},
        "s": hits or {},
    }


def _cov(tmp: Path, data: dict) -> Path:
    d = tmp / "coverage"
    d.mkdir(exist_ok=True)
    (d / "coverage-final.json").write_text(json.dumps(data))
    return _log(tmp)


PY = "example.py"

# ── run_xo_linter_in_container ──────────────────────────────────


class TestRunXoLinterInContainer:
    @patch("bluei.engine.linters.subprocess.run")
    def test_json_output(self, mr):
        data = [
            {
                "filePath": "src/main.ts",
                "messages": [
                    _xo_msg("max-lines", "too long"),
                    _xo_msg("complexity", "complex", 50, 5, 2),
                ],
            }
        ]
        mr.return_value = MagicMock(returncode=1, stdout=json.dumps(data))
        rc, res = run_xo_linter_in_container()
        assert rc == 1 and len(res[0]["messages"]) == 2

    @patch("bluei.engine.linters.subprocess.run")
    def test_text_output(self, mr):
        t = "src/f.ts:1:1\n  \u26a0  10:5  msg  max-lines\n  src/g.ts:1:1\n    \u26a0  5:1  msg  rule\n"
        mr.return_value = MagicMock(returncode=1, stdout=t)
        _, res = run_xo_linter_in_container()
        assert len(res) == 2 and res[0]["filePath"] == "src/f.ts"

    @pytest.mark.parametrize(
        "exc",
        [
            subprocess.TimeoutExpired(cmd="docker", timeout=120),
            OSError("no docker"),
        ],
    )
    @patch("bluei.engine.linters.subprocess.run")
    def test_failure_returns_empty(self, mr, exc):
        mr.side_effect = exc
        rc, res = run_xo_linter_in_container()
        assert rc == 1 and res == []


# ── discover_python_linter_findings ─────────────────────────────


class TestDiscoverPythonLinterFindings:
    @patch("bluei.engine.linters.subprocess.run")
    def test_ruff_json_produces_correct_findings(self, mr, tmp_path):
        (tmp_path / PY).write_text("x=1")
        p = str(tmp_path / PY)
        mr.side_effect = _ruff_se(
            [
                _ruff("B007", 10, p, "unused var", "safe"),
                _ruff("E501", 25, p, "line too long"),
            ]
        )
        f = discover_python_linter_findings(tmp_path, _log(tmp_path))
        assert len(f) == 2
        assert f[0].rule == "ruff-b007" and f[0].safe_to_autofix is True
        assert f[1].rule == "ruff-e501" and f[1].safe_to_autofix is False

    @patch("bluei.engine.linters.subprocess.run")
    def test_c408_never_autofix(self, mr, tmp_path):
        """C408 hardcoded not autofixable despite safe applicability."""
        (tmp_path / PY).write_text("d=dict()")
        mr.side_effect = _ruff_se(
            [_ruff("C408", 5, str(tmp_path / PY), "dict call", "safe")]
        )
        f = discover_python_linter_findings(tmp_path, _log(tmp_path))
        assert f[0].safe_to_autofix is False

    @patch("bluei.engine.linters.subprocess.run")
    def test_deduplicates(self, mr, tmp_path):
        (tmp_path / PY).write_text("x=1")
        d = _ruff("B007", 3, str(tmp_path / PY))
        mr.side_effect = _ruff_se([d, d, d])
        assert len(discover_python_linter_findings(tmp_path, _log(tmp_path))) == 1

    @patch("bluei.engine.linters.subprocess.run")
    def test_out_of_tree_path(self, mr, tmp_path):
        (tmp_path / PY).write_text("x=1")
        mr.side_effect = _ruff_se([_ruff("B007", 3, "/other/path.py")])
        assert (
            discover_python_linter_findings(tmp_path, _log(tmp_path))[0].path
            == "/other/path.py"
        )

    @patch("bluei.engine.linters.subprocess.run")
    def test_applicability_mapping(self, mr, tmp_path):
        """safe/generated -> autofix; unsafe -> not."""
        (tmp_path / PY).write_text("x=1")
        for app, exp in [("safe", True), ("generated", True), ("unsafe", False)]:
            mr.side_effect = _ruff_se([_ruff("B007", 3, str(tmp_path / PY), app=app)])
            assert (
                discover_python_linter_findings(tmp_path, _log(tmp_path))[
                    0
                ].safe_to_autofix
                is exp
            )

    @pytest.mark.parametrize(
        "stdout,py", [("[]", True), ("null", True), ("", True), (None, False)]
    )
    @patch("bluei.engine.linters.subprocess.run")
    def test_returns_empty_on_no_input(self, mr, tmp_path, stdout, py):
        if py:
            (tmp_path / PY).write_text("x=1")
            mr.side_effect = _ruff_se([], rc=0)
        assert discover_python_linter_findings(tmp_path, _log(tmp_path)) == []


# ── discover_xo_linter_findings ─────────────────────────────────


class TestDiscoverXoLinterFindings:
    @patch("bluei.engine.linters.subprocess.run")
    def test_xo_json_produces_findings(self, mr, tmp_path):
        xo = [
            {
                "filePath": str(tmp_path / "src/a.ts"),
                "messages": [
                    _xo_msg("complexity", "complex", 5, 1, 2),
                    _xo_msg("max-lines", "long", 200, 1, 1),
                ],
            }
        ]
        mr.return_value = MagicMock(returncode=1, stdout=json.dumps(xo))
        f = discover_xo_linter_findings(tmp_path, _xo_repo(tmp_path))
        assert len(f) == 2
        assert f[0].rule == "xo-complexity" and f[1].rule == "xo-max-lines"

    @patch("bluei.engine.linters.subprocess.run")
    def test_strips_absolute_repo_path(self, mr, tmp_path):
        xo = [
            {
                "filePath": str(tmp_path / "src/deep/f.ts"),
                "messages": [_xo_msg("complexity")],
            }
        ]
        mr.return_value = MagicMock(returncode=1, stdout=json.dumps(xo))
        assert (
            discover_xo_linter_findings(tmp_path, _xo_repo(tmp_path))[0].path
            == "src/deep/f.ts"
        )

    @patch("bluei.engine.linters.run_xo_linter_in_container")
    def test_strips_container_app_prefix(self, mc, tmp_path):
        """ky repo: /app/ prefix -> relative path."""
        ky = tmp_path / "ky-project"
        ky.mkdir()
        (ky / "package.json").write_text(
            json.dumps({"devDependencies": {"xo": "^0.50"}})
        )
        mc.return_value = (
            1,
            [
                {
                    "filePath": "/app/src/api.ts",
                    "messages": [_xo_msg("max-lines", "long", 10, 1, 1)],
                }
            ],
        )
        f = discover_xo_linter_findings(ky, _log(ky))
        assert f[0].path == "src/api.ts" and f[0].rule == "xo-max-lines"

    @patch("bluei.engine.linters.subprocess.run")
    def test_skips_no_rule_id(self, mr, tmp_path):
        xo = [
            {
                "filePath": "a.ts",
                "messages": [
                    _xo_msg("", "no rule"),
                    _xo_msg("max-lines", "has rule", 2),
                ],
            }
        ]
        mr.return_value = MagicMock(returncode=1, stdout=json.dumps(xo))
        f = discover_xo_linter_findings(tmp_path, _xo_repo(tmp_path))
        assert len(f) == 1 and f[0].rule == "xo-max-lines"

    @pytest.mark.parametrize(
        "pkg",
        [
            json.dumps({"name": "test"}),
            json.dumps({"devDependencies": {"eslint": "^8"}}),
            "not json {{{",
        ],
    )
    def test_returns_empty_without_xo(self, tmp_path, pkg):
        (tmp_path / "package.json").write_text(pkg)
        assert discover_xo_linter_findings(tmp_path, _log(tmp_path)) == []

    def test_returns_empty_without_package_json(self, tmp_path):
        assert discover_xo_linter_findings(tmp_path, _log(tmp_path)) == []


# ── discover_typescript_type_findings ────────────────────────────


class TestDiscoverTypescriptTypeFindings:
    @pytest.mark.parametrize(
        "err,rule",
        [
            (
                "error TS7005: Variable 'x' implicitly has an 'any' type",
                "type-explicit-any",
            ),
            (
                "error TS7006: Parameter 'x' implicitly has an 'any' type",
                "type-explicit-any",
            ),
            ("error TS7010: 'foo' missing return type", "type-missing-return"),
            (
                "error TS7016: could not find a declaration file for module",
                "type-untyped-import",
            ),
            (
                "error TS9999: Parameter 'x' implicitly has missing type annotation",
                "type-missing-param",
            ),
        ],
    )
    @patch("bluei.engine.linters.run_capture")
    def test_tsc_errors_mapped(self, mr, tmp_path, err, rule):
        (tmp_path / "tsconfig.json").write_text("{}")
        mr.return_value = (1, f"{tmp_path}/src/a.ts(10,5): {err}")
        f = discover_typescript_type_findings(tmp_path, _log(tmp_path))
        assert (
            len(f) == 1
            and f[0].rule == rule
            and f[0].line == 10
            and f[0].path == "src/a.ts"
        )

    @patch("bluei.engine.linters.run_capture")
    def test_multiple_errors(self, mr, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        mr.return_value = (
            1,
            f"{tmp_path}/src/a.ts(10,5): error TS7005: any type\n"
            f"{tmp_path}/src/b.ts(20,1): error TS7010: missing return\n",
        )
        f = discover_typescript_type_findings(tmp_path, _log(tmp_path))
        assert len(f) == 2 and {x.rule for x in f} == {
            "type-explicit-any",
            "type-missing-return",
        }

    @patch("bluei.engine.linters.run_capture")
    def test_unknown_errors_ignored(self, mr, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        mr.return_value = (1, f"{tmp_path}/a.ts(1,1): error TS9999: random")
        assert discover_typescript_type_findings(tmp_path, _log(tmp_path)) == []

    def test_no_tsconfig(self, tmp_path):
        assert discover_typescript_type_findings(tmp_path, _log(tmp_path)) == []

    @patch("bluei.engine.linters.run_capture")
    def test_tsc_exception(self, mr, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        mr.side_effect = Exception("boom")
        assert discover_typescript_type_findings(tmp_path, _log(tmp_path)) == []


# ── discover_test_coverage_findings ──────────────────────────────


class TestDiscoverTestCoverageFindings:
    def test_uncovered_branch(self, tmp_path):
        f = discover_test_coverage_findings(
            tmp_path, _cov(tmp_path, {"src/a.ts": _covf(br={"10,5": 0})})
        )
        assert len(f) == 1 and f[0].rule == "test-coverage-branch" and f[0].line == 10

    def test_uncovered_function_dict(self, tmp_path):
        f = discover_test_coverage_findings(
            tmp_path,
            _cov(tmp_path, {"src/u.ts": _covf(fn={"myFunc": {"hits": 0, "line": 42}})}),
        )
        assert (
            f[0].rule == "test-coverage-function"
            and f[0].line == 42
            and "myFunc" in f[0].snippet
        )

    def test_uncovered_function_int(self, tmp_path):
        """Function as plain int 0 -> line defaults to 1."""
        f = discover_test_coverage_findings(
            tmp_path, _cov(tmp_path, {"src/a.ts": _covf(fn={"f": 0})})
        )
        assert f[0].rule == "test-coverage-function"

    def test_uncovered_statement(self, tmp_path):
        f = discover_test_coverage_findings(
            tmp_path,
            _cov(
                tmp_path,
                {"src/a.ts": _covf(sm={"1": {"start": {"line": 15}}}, hits={"1": 0})},
            ),
        )
        assert (
            f[0].rule == "test-coverage-line"
            and f[0].line == 15
            and f[0].safe_to_autofix is False
        )

    def test_one_finding_per_type(self, tmp_path):
        """Multiple gaps of each type -> one finding per type (anti-spam)."""
        f = discover_test_coverage_findings(
            tmp_path,
            _cov(
                tmp_path,
                {
                    "src/a.ts": _covf(
                        br={"0,0": 0, "5,2": 0},
                        fn={"f1": 0, "f2": 0},
                        sm={"1": {"start": {"line": 5}}, "2": {"start": {"line": 10}}},
                        hits={"1": 0, "2": 0},
                    )
                },
            ),
        )
        assert {x.rule for x in f} == {
            "test-coverage-branch",
            "test-coverage-function",
            "test-coverage-line",
        }

    def test_skips_node_modules_and_tests(self, tmp_path):
        assert (
            discover_test_coverage_findings(
                tmp_path,
                _cov(
                    tmp_path,
                    {
                        "node_modules/f.ts": _covf(br={"0": 0}),
                        "src/b.test.ts": _covf(br={"0": 0}),
                    },
                ),
            )
            == []
        )

    @pytest.mark.parametrize(
        "data",
        [
            {},
            {"src/a.ts": _covf()},
        ],
    )
    def test_no_gaps_no_findings(self, tmp_path, data):
        assert discover_test_coverage_findings(tmp_path, _cov(tmp_path, data)) == []

    def test_no_coverage_file(self, tmp_path):
        assert discover_test_coverage_findings(tmp_path, _log(tmp_path)) == []

    def test_invalid_json(self, tmp_path):
        d = tmp_path / "coverage"
        d.mkdir()
        (d / "coverage-final.json").write_text("{bad")
        assert discover_test_coverage_findings(tmp_path, _log(tmp_path)) == []

    def test_non_dict_start_defaults_line_1(self, tmp_path):
        f = discover_test_coverage_findings(
            tmp_path,
            _cov(tmp_path, {"src/a.ts": _covf(sm={"1": {"start": 42}}, hits={"1": 0})}),
        )
        assert f[0].line == 1

    def test_nonzero_hits_skipped(self, tmp_path):
        assert (
            discover_test_coverage_findings(
                tmp_path,
                _cov(
                    tmp_path,
                    {
                        "src/a.ts": _covf(
                            sm={"1": {"start": {"line": 5}}}, hits={"1": 3}
                        )
                    },
                ),
            )
            == []
        )
