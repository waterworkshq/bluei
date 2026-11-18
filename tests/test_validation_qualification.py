"""Acceptance boundary for validation evidence qualification.

Real-subprocess tests (no models): unavailable invocations must be recorded
as unavailable evidence (rc sentinel + runtime marker), never as executed
pass/fail proof; a genuine executed unchanged baseline failure stays
qualified; empty or hand-crafted check sets never qualify autonomous repair.
"""

import stat
from pathlib import Path

import pytest

import bluei.engine.validation as validation
from bluei.engine.constants import (
    clear_validation_config_cache,
    load_validation_config,
)
from bluei.engine.validation import (
    RC_UNAVAILABLE,
    choose_validation_baseline,
    run_named_checks,
    run_validation_gate,
)
from bluei.engine.verify import VerificationResult, run_validation


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


class TestUnavailableEvidence:
    def test_missing_executable(self, tmp_path):
        result = run_named_checks(
            _repo(tmp_path),
            {"miss": ["bluei-definitely-missing-binary", "--version"]},
            tmp_path / "log.txt",
            "baseline",
        )["miss"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "missing-executable"
        assert result["rc"] == RC_UNAVAILABLE == -1
        assert result["fingerprint"] == ""
        assert result["output"] == ""

    def test_missing_python_script(self, tmp_path):
        result = run_named_checks(
            _repo(tmp_path),
            {"script": ["python3", "bluei_definitely_missing_script.py"]},
            tmp_path / "log.txt",
            "baseline",
        )["script"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "missing-script"
        assert result["rc"] == RC_UNAVAILABLE

    def test_not_executable(self, tmp_path):
        repo = _repo(tmp_path)
        script = repo / "not_exec.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(stat.S_IRUSR | stat.S_IWUSR)
        result = run_named_checks(
            repo, {"noexec": [str(script)]}, tmp_path / "log.txt", "baseline"
        )["noexec"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "not-executable"
        assert result["rc"] == RC_UNAVAILABLE

    @pytest.mark.parametrize("cmd", ["not-a-list", [], ["sh", 7]])
    def test_invalid_argv(self, tmp_path, cmd):
        result = run_named_checks(
            _repo(tmp_path), {"bad": cmd}, tmp_path / "log.txt", "baseline"
        )["bad"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "invalid-argv"
        assert result["rc"] == RC_UNAVAILABLE

    def test_nul_byte_argv_invalid(self, tmp_path):
        result = run_named_checks(
            _repo(tmp_path),
            {"nul": ["sh", "-c", "exit 0\x00"]},
            tmp_path / "log.txt",
            "baseline",
        )["nul"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "invalid-argv"

    def test_relative_exe_resolved_against_repo_cwd(self, tmp_path):
        repo = _repo(tmp_path)
        script = repo / "rel_check.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        result = run_named_checks(
            repo, {"rel": ["./rel_check.sh"]}, tmp_path / "log.txt", "baseline"
        )["rel"]
        assert result["available"] is True
        assert result["rc"] == 0

    def test_relative_exe_missing_in_repo_cwd(self, tmp_path):
        result = run_named_checks(
            _repo(tmp_path),
            {"relmiss": ["./bluei_definitely_missing_rel.sh"]},
            tmp_path / "log.txt",
            "baseline",
        )["relmiss"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "missing-executable"

    def test_python_value_flag_does_not_hide_missing_script(self, tmp_path):
        # `-X utf8` used to shift the script position and let a missing
        # script masquerade as an executed failure.
        result = run_named_checks(
            _repo(tmp_path),
            {"gap": ["python3", "-X", "utf8", "bluei_definitely_missing_gap.py"]},
            tmp_path / "log.txt",
            "baseline",
        )["gap"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "missing-script"

    def test_python_unknown_flag_unsupported(self, tmp_path):
        result = run_named_checks(
            _repo(tmp_path),
            {"odd": ["python3", "-Q", "old", "check.py"]},
            tmp_path / "log.txt",
            "baseline",
        )["odd"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "unsupported-invocation"

    def test_python_inline_workload_available(self, tmp_path):
        repo = _repo(tmp_path)
        result = run_named_checks(
            repo,
            {"inline": ["python3", "-c", "raise SystemExit(0)"]},
            tmp_path / "log.txt",
            "baseline",
        )["inline"]
        assert result["available"] is True
        assert result["rc"] == 0

    def test_python_version_has_no_workload(self, tmp_path):
        repo = _repo(tmp_path)
        result = run_named_checks(
            repo, {"ver": ["python3", "--version"]}, tmp_path / "log.txt", "baseline"
        )["ver"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "unsupported-invocation"
        assert result["rc"] == RC_UNAVAILABLE

    def test_bare_python_repl_unsupported(self, tmp_path):
        repo = _repo(tmp_path)
        results = run_named_checks(
            repo,
            {"repl": ["python3"], "flagged": ["python3", "-u"], "stdin": ["python3", "-"]},
            tmp_path / "log.txt",
            "baseline",
        )
        for name in ("repl", "flagged", "stdin"):
            assert results[name]["available"] is False
            assert results[name]["unavailable_reason"] == "unsupported-invocation"

    def test_python_missing_module_unavailable(self, tmp_path):
        repo = _repo(tmp_path)
        result = run_named_checks(
            repo,
            {"mod": ["python3", "-m", "bluei_definitely_missing_module"]},
            tmp_path / "log.txt",
            "baseline",
        )["mod"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "missing-module"
        assert result["rc"] == RC_UNAVAILABLE

    def test_python_resolvable_module_workload_available(self, tmp_path):
        repo = _repo(tmp_path)
        result = run_named_checks(
            repo, {"mod": ["python3", "-m", "site"]}, tmp_path / "log.txt", "baseline"
        )["mod"]
        assert result["available"] is True
        assert result["rc"] == 0

    def test_python_flag_operands_missing_or_empty_invalid(self, tmp_path):
        repo = _repo(tmp_path)
        results = run_named_checks(
            repo,
            {
                "c-missing": ["python3", "-c"],
                "c-empty": ["python3", "-c", ""],
                "m-missing": ["python3", "-m"],
                "m-empty": ["python3", "-m", ""],
                "m-dash": ["python3", "-m", "-q"],
                "x-missing": ["python3", "-X"],
            },
            tmp_path / "log.txt",
            "baseline",
        )
        for name in ("c-missing", "c-empty", "m-missing", "m-empty", "m-dash", "x-missing"):
            assert results[name]["available"] is False
            assert results[name]["unavailable_reason"] == "invalid-argv"

    def test_exec_format_error_unavailable(self, tmp_path):
        repo = _repo(tmp_path)
        blob = repo / "not_a_real_elf"
        blob.write_bytes(b"not an executable format")
        blob.chmod(0o755)
        result = run_named_checks(
            repo, {"fmt": [str(blob)]}, tmp_path / "log.txt", "baseline"
        )["fmt"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "execution-unavailable"
        assert result["rc"] == RC_UNAVAILABLE

    def test_script_directory_is_not_a_script(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "toolkit").mkdir()
        result = run_named_checks(
            repo, {"dir": ["python3", "toolkit"]}, tmp_path / "log.txt", "baseline"
        )["dir"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "missing-script"

    def test_missing_script_any_suffix(self, tmp_path):
        repo = _repo(tmp_path)
        result = run_named_checks(
            repo,
            {"tool": ["python3", "bluei_definitely_missing_tool"]},
            tmp_path / "log.txt",
            "baseline",
        )["tool"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "missing-script"

    def test_timeout_is_unavailable(self, tmp_path, monkeypatch):
        real_run_capture = validation.run_capture

        def fast_capture(cmd, cwd, timeout=1):
            # Real subprocess, real TimeoutExpired — only the budget shrinks.
            return real_run_capture(cmd, cwd=cwd, timeout=1)

        monkeypatch.setattr(validation, "run_capture", fast_capture)
        result = run_named_checks(
            _repo(tmp_path),
            {"slow": ["sleep", "30"]},
            tmp_path / "log.txt",
            "baseline",
        )["slow"]
        assert result["available"] is False
        assert result["unavailable_reason"] == "timeout"
        assert result["rc"] == RC_UNAVAILABLE

    def test_unavailable_logged(self, tmp_path):
        repo = _repo(tmp_path)
        log = tmp_path / "log.txt"
        run_named_checks(
            repo, {"miss": ["bluei-definitely-missing-binary"]}, log, "phase-x"
        )
        text = log.read_text()
        assert "available=False" in text
        assert "unavailable_reason=missing-executable" in text

    def test_executed_failure_keeps_real_rc_and_fingerprint(self, tmp_path):
        result = run_named_checks(
            _repo(tmp_path),
            {"fail": ["sh", "-c", "echo boom >&2; exit 3"]},
            tmp_path / "log.txt",
            "baseline",
        )["fail"]
        assert result["available"] is True
        assert result["unavailable_reason"] is None
        assert result["rc"] == 3
        assert result["fingerprint"] != ""


class TestGateQualification:
    def test_empty_checks_passed_but_not_qualified(self, tmp_path):
        result = run_validation_gate(
            tmp_path, tmp_path, None, log_file=tmp_path / "log.txt"
        )
        assert result["passed"] is True  # no-check diagnostic contract
        assert result["qualified"] is False
        assert result["checks_configured"] == 0
        assert result["unavailable"] == []
        assert result["message"] == "unqualified: no checks configured"

    def test_observed_empty_with_configured_checks_not_qualified(self, tmp_path):
        # Pre-computed empty results with a non-empty configured set: no
        # executed evidence at all, so never qualified.
        result = run_validation_gate(
            tmp_path,
            tmp_path,
            {"fail": ["sh", "-c", "exit 3"]},
            baseline_results={},
            post_fix_results={},
            target_results={},
            log_file=tmp_path / "log.txt",
        )
        assert result["qualified"] is False
        assert result["unavailable"] == ["fail"]
        assert result["checks_available"] == 0

    def test_configured_name_missing_from_post_not_qualified(self, tmp_path):
        repo = _repo(tmp_path)
        baseline = run_named_checks(
            repo, {"fail": ["sh", "-c", "exit 3"]}, tmp_path / "log.txt", "b"
        )
        post = run_named_checks(
            repo, {"other": ["true"]}, tmp_path / "log.txt", "p"
        )
        result = run_validation_gate(
            repo,
            repo,
            {"fail": ["sh", "-c", "exit 3"]},
            baseline_results=baseline,
            post_fix_results=post,
            target_results={},
            log_file=tmp_path / "log.txt",
        )
        assert result["qualified"] is False
        assert "fail" in result["unavailable"]

    def test_executed_unchanged_failure_is_qualified(self, tmp_path):
        # A genuine executed unchanged failure is tolerated — but only
        # alongside a passing control in the same configured set.
        repo = _repo(tmp_path)
        checks = {"fail": ["sh", "-c", "exit 3"], "ctl": ["true"]}
        baseline = run_named_checks(repo, checks, tmp_path / "log.txt", "b")
        post = run_named_checks(repo, checks, tmp_path / "log.txt", "p")
        assert baseline["fail"]["fingerprint"] == post["fail"]["fingerprint"]
        result = run_validation_gate(
            repo,
            repo,
            checks,
            baseline_results=baseline,
            post_fix_results=post,
            target_results={},
            log_file=tmp_path / "log.txt",
        )
        assert result["passed"] is True
        assert result["qualified"] is True
        assert result["checks_available"] == 2
        assert result["checks_passed"] == 1
        assert result["tolerated_failures"] == ["fail"]
        assert result["message"] == (
            "checks passed with tolerated pre-existing failures: fail"
        )

    def test_all_failed_baseline_unqualified(self, tmp_path):
        # Legacy math can pass (unchanged failures tolerated), but with no
        # passing control the gate cannot distinguish "ran and failed" from
        # "wrappers never ran" — never qualified.
        repo = _repo(tmp_path)
        checks = {"fail": ["sh", "-c", "exit 3"]}
        baseline = run_named_checks(repo, checks, tmp_path / "log.txt", "b")
        post = run_named_checks(repo, checks, tmp_path / "log.txt", "p")
        result = run_validation_gate(
            repo,
            repo,
            checks,
            baseline_results=baseline,
            post_fix_results=post,
            target_results={},
            log_file=tmp_path / "log.txt",
        )
        assert result["passed"] is True  # legacy tolerated-failure math
        assert result["qualified"] is False
        assert result["checks_passed"] == 0
        assert result["message"] == "unqualified: no passing configured check"

    def test_missing_post_is_regression(self, tmp_path):
        repo = _repo(tmp_path)
        baseline = run_named_checks(
            repo, {"fail": ["sh", "-c", "exit 3"]}, tmp_path / "log.txt", "b"
        )
        result = run_validation_gate(
            repo,
            repo,
            {"fail": ["sh", "-c", "exit 3"]},
            baseline_results=baseline,
            post_fix_results={},
            target_results={},
            log_file=tmp_path / "log.txt",
        )
        assert result["passed"] is False
        assert "fail" in result["regressions"]

    def test_unavailable_check_is_regression_and_unqualified(self, tmp_path):
        repo = _repo(tmp_path)
        result = run_validation_gate(
            repo,
            repo,
            {"miss": ["bluei-definitely-missing-binary"]},
            log_file=tmp_path / "log.txt",
        )
        assert result["passed"] is False
        assert "miss" in result["regressions"]
        assert result["qualified"] is False
        assert result["unavailable"] == ["miss"]

    def test_hand_crafted_dicts_not_qualified(self, tmp_path):
        # Back-compat: hand-crafted dicts still drive the low-level passed
        # math, but a missing runtime marker is never runtime proof.
        result = run_validation_gate(
            tmp_path,
            tmp_path,
            {"lint": ["true"]},
            baseline_results={"lint": {"rc": 0, "fingerprint": ""}},
            post_fix_results={"lint": {"rc": 0, "fingerprint": ""}},
            target_results={"lint": {"rc": 0, "fingerprint": ""}},
            log_file=tmp_path / "log.txt",
        )
        assert result["passed"] is True
        assert result["qualified"] is False
        assert result["unavailable"] == ["lint"]
        assert result["message"] == "unqualified: lint(no-runtime-marker)"


class TestChooseValidationBaselineWorktree:
    def test_unavailable_worktree_not_hidden_by_repo_baseline(self, tmp_path):
        repo = _repo(tmp_path)
        worktree = run_named_checks(
            repo, {"lint": ["bluei-definitely-missing-binary"]}, tmp_path / "l", "wt"
        )
        repo_level = {"lint": {"rc": 0, "fingerprint": "", "available": True}}
        chosen = choose_validation_baseline(repo_level, worktree, tmp_path / "l")
        assert chosen is worktree
        assert chosen["lint"]["available"] is False

    def test_worktree_runtime_marker_selected(self, tmp_path):
        worktree = {"lint": {"rc": 0, "available": True, "unavailable_reason": None}}
        chosen = choose_validation_baseline({"lint": {"rc": 0}}, worktree, Path("/dev/null"))
        assert chosen is worktree

    def test_legacy_rc_dict_still_selected(self, tmp_path):
        worktree = {"lint": {"rc": 1, "fingerprint": "abc"}}
        chosen = choose_validation_baseline({"lint": {"rc": 0}}, worktree, Path("/dev/null"))
        assert chosen is worktree

    def test_not_run_worktree_falls_back_to_repo(self, tmp_path):
        worktree = {"lint": {"cmd": ["true"]}}
        repo_level = {"lint": {"rc": 0}}
        chosen = choose_validation_baseline(repo_level, worktree, Path("/dev/null"))
        assert chosen is repo_level


class TestA02NoImplicitSandboxFallback:
    def test_ordinary_repo_gets_empty_baseline_checks(self, tmp_path):
        clear_validation_config_cache()
        try:
            config = load_validation_config(tmp_path)
            assert config["baseline_validation_checks"] == {}
        finally:
            clear_validation_config_cache()

    def test_explicit_operator_config_honored(self, tmp_path):
        clear_validation_config_cache()
        try:
            (tmp_path / "config.yaml").write_text(
                "baseline_validation_checks:\n"
                "  baseline-0: [python3, run_lint.py]\n"
            )
            config = load_validation_config(tmp_path)
            assert config["baseline_validation_checks"] == {
                "baseline-0": ["python3", "run_lint.py"]
            }
        finally:
            clear_validation_config_cache()


class TestVerifyResultCarriesQualification:
    def test_run_validation_maps_fields(self, tmp_path):
        result = run_validation(
            tmp_path, tmp_path, None, log_file=tmp_path / "log.txt"
        )
        assert isinstance(result, VerificationResult)
        assert result.passed is True
        assert result.qualified is False
        assert result.checks_configured == 0
        assert result.checks_passed == 0

    def test_real_passing_check_qualified(self, tmp_path):
        result = run_validation(
            tmp_path,
            tmp_path,
            {"ok": ["true"]},
            log_file=tmp_path / "log.txt",
        )
        assert result.passed is True
        assert result.qualified is True
        assert result.checks_available == 1
        assert result.checks_passed == 1
        assert result.message == "all checks passed"
        assert result.tolerated_failures == []
