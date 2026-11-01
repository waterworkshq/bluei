"""Python-only discovery scope: early python branch in discover_findings,
manifest filtering in the plugin loader, and scoped verify_fix_closed."""

import shutil

import pytest

import bluei.engine.orchestrator as orchestrator
import bluei.engine.verify as verify_module
import bluei.engine.discovery as discovery_module
from bluei.engine.discovery import discover_findings
from bluei.engine.plugin_loader import run_plugin_discovery
from bluei.engine.validation import responsible_detector_ran, verify_fix_closed
from bluei.engine.verify import verify_fix_closed as verify_fix_closed_wrapped

needs_ruff = pytest.mark.skipif(
    shutil.which("ruff") is None, reason="ruff is not installed"
)


def _make_mixed_repo(tmp_path):
    """A repo with Python, docs, and Go inputs so every out-of-scope
    detector would have something to chew on under the default scope."""
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "app.py").write_text(
        "def calculate_total(first, second):\n"
        "    return first + second\n\n"
        "if __name__ == '__main__':\n    print(missing_name)\n"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "OPERATIONS.md").write_text("# Operations\nno runbook\n")
    (tmp_path / "README.md").write_text("# demo\n\nrun `pytest -q` to test\n")
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n")
    return tmp_path


@needs_ruff
def test_python_scope_runs_only_python_detectors(tmp_path):
    repo = _make_mixed_repo(tmp_path)
    outcomes = {}
    findings = discover_findings(
        repo,
        log_file=repo / "scan.log",
        docs_index_file=repo / "docs.json",
        outcomes=outcomes,
        discovery_scope="python",
    )

    # No docs/sandbox/non-Python findings even though the fixtures would fire.
    assert findings, "expected ruff findings on app.py"
    assert all(f.path.endswith(".py") for f in findings)
    assert not [f for f in findings if f.rule.startswith("docs-")]
    assert not [f for f in findings if f.rule.startswith("test-gap-")]

    # Python detectors ran; everything else skipped with an explicit reason.
    assert outcomes["ast-python"]["status"] == "ran"
    assert outcomes["python-linter-ruff"]["status"] == "ran"
    for detector in (
        "sandbox-string-detectors",
        "ast-typescript",
        "ast-go",
        "ast-rust",
        "typescript-types",
        "xo-linter",
        "test-coverage",
    ):
        assert outcomes[detector]["status"] == "skipped", detector
        assert "outside python discovery scope" in outcomes[detector]["reason"]

    # Non-Python plugin manifests are filtered with the scope reason.
    assert "outside python discovery scope" in outcomes["plugin:plugin-go"]["reason"]


@needs_ruff
def test_default_scope_still_runs_docs_detectors(tmp_path):
    repo = _make_mixed_repo(tmp_path)
    outcomes = {}
    findings = discover_findings(
        repo,
        log_file=repo / "scan.log",
        docs_index_file=repo / "docs.json",
        outcomes=outcomes,
    )

    assert any(f.rule == "docs-quickstart-gap" for f in findings)
    assert any(f.rule == "docs-missing-rollback" for f in findings)
    assert outcomes["sandbox-string-detectors"]["status"] == "ran"


@needs_ruff
def test_run_plugin_discovery_scope_filters_manifests(tmp_path):
    repo = _make_mixed_repo(tmp_path)

    scoped = {}
    run_plugin_discovery(repo, outcomes=scoped, discovery_scope="python")
    assert scoped["plugin:plugin-python"]["status"] == "ran"
    assert scoped["plugin:plugin-go"]["status"] == "skipped"
    assert "outside python discovery scope" in scoped["plugin:plugin-go"]["reason"]

    default = {}
    run_plugin_discovery(repo, outcomes=default)
    assert "not detected in repo" in default["plugin:plugin-go"]["reason"]


def test_unknown_discovery_scope_rejected_before_branches(tmp_path):
    with pytest.raises(ValueError, match="unknown discovery_scope"):
        discover_findings(
            tmp_path,
            log_file=tmp_path / "scan.log",
            docs_index_file=tmp_path / "docs.json",
            discovery_scope="typescript",
        )


def test_skip_internal_discovery_optout_precedes_python_scope(
    tmp_path, monkeypatch
):
    repo = _make_mixed_repo(tmp_path)
    monkeypatch.setenv("--skip-internal-discovery", "1")
    outcomes = {}
    findings = discover_findings(
        repo,
        log_file=repo / "scan.log",
        docs_index_file=repo / "docs.json",
        outcomes=outcomes,
        discovery_scope="python",
    )
    assert findings == []
    assert outcomes["internal-discovery"]["status"] == "skipped"
    assert "ast-python" not in outcomes  # python branch never ran


def test_verify_fix_closed_python_scope_refuses_rule_without_python_producer(
    tmp_path, make_finding
):
    # A .py path is not enough: a typescript-family (or unknown-producer) rule
    # must be refused before a trusted custom discover_fn could close it.
    finding = make_finding(path="app.py", rule="type-untyped-import")
    calls = []

    def spy_discover(worktree, docs_index_file=None, **kwargs):
        calls.append(kwargs)
        return []

    closed = verify_fix_closed(
        tmp_path,
        finding,
        tmp_path / "log.txt",
        discover_fn=spy_discover,
        discovery_scope="python",
    )
    assert closed is False
    assert calls == []


def test_unknown_scope_rejected_at_validation_and_plugin_loader(tmp_path, make_finding):
    finding = make_finding(path="app.py", rule="broad-except")
    with pytest.raises(ValueError, match="unknown discovery_scope"):
        verify_fix_closed(
            tmp_path, finding, tmp_path / "log.txt", discovery_scope="go"
        )
    with pytest.raises(ValueError, match="unknown discovery_scope"):
        run_plugin_discovery(tmp_path, discovery_scope="go")
    with pytest.raises(ValueError, match="unknown discovery_scope"):
        from bluei.engine.plugin_loader import load_applicable_plugins

        load_applicable_plugins(tmp_path, discovery_scope="go")


def test_verify_fix_closed_python_scope_refuses_non_python_finding(
    tmp_path, make_finding
):
    finding = make_finding(path="docs/OPERATIONS.md", rule="docs-missing-rollback")
    calls = []

    def spy_discover(worktree, docs_index_file=None, **kwargs):
        calls.append(kwargs)
        return []

    closed = verify_fix_closed(
        tmp_path,
        finding,
        tmp_path / "log.txt",
        discover_fn=spy_discover,
        discovery_scope="python",
    )
    assert closed is False
    assert calls == []  # refused before the custom discover_fn was injected


def test_verify_fix_closed_python_scope_rescan_passes_scope_and_gates(
    tmp_path, monkeypatch, make_finding
):
    finding = make_finding(path="app.py", rule="broad-except")
    captured = {}

    def fake_discover(worktree, docs_index_file=None, outcomes=None, **kwargs):
        captured["scope"] = kwargs.get("discovery_scope")
        if outcomes is not None and kwargs.get("discovery_scope") == "python":
            outcomes["ast-python"] = {"status": "ran", "count": 0}
        return []

    monkeypatch.setattr(orchestrator, "discover_findings", fake_discover)
    assert verify_fix_closed(
        tmp_path, finding, tmp_path / "log.txt", discovery_scope="python"
    )
    assert captured["scope"] == "python"

    # Producer gate: absence from a rescan whose responsible detector did not
    # run is not closure evidence, even under the python scope.
    def fake_no_detector(worktree, docs_index_file=None, outcomes=None, **kwargs):
        return []

    monkeypatch.setattr(orchestrator, "discover_findings", fake_no_detector)
    assert not verify_fix_closed(
        tmp_path, finding, tmp_path / "log.txt", discovery_scope="python"
    )


def test_verify_wrapper_forwards_python_scope(tmp_path, make_finding, monkeypatch):
    finding = make_finding(path="app.py", rule="broad-except")
    captured = {}

    def fake_verify(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(verify_module, "_verify_fix_closed", fake_verify)

    result = verify_fix_closed_wrapped(
        tmp_path, finding, tmp_path / "log.txt", discovery_scope="python"
    )
    assert result.is_closed is True
    assert captured["discovery_scope"] == "python"

    captured.clear()
    verify_fix_closed_wrapped(tmp_path, finding, tmp_path / "log.txt")
    assert captured == {"worktree_path": tmp_path, "finding": finding,
                        "log_file": tmp_path / "log.txt"}


def test_python_scope_keeps_only_ruff_produced_plugin_rules(
    tmp_path, monkeypatch, make_finding
):
    # Regex text-scan sandbox rules are dropped; E501/E722/B001/S108-mapped
    # internal names and verbatim ruff-<code> rules survive, the outcome
    # counter documents the filtered count, and native producers win shared
    # (path, line, rule) keys over plugin duplicates.
    repo = _make_mixed_repo(tmp_path)
    calls = {}

    def fake_ast(repo_path, log_file, rule_meta, outcome=None):
        outcome["ast-python"] = {"status": "ran", "count": 1}
        return [make_finding(path="app.py", line=10, rule="broad-except",
                             snippet="native-ast")]

    def fake_ruff(repo_path, log_file, outcome=None):
        outcome["python-linter-ruff"] = {"status": "ran", "count": 1}
        return [make_finding(path="app.py", line=10, rule="ruff-f821",
                             snippet="native-ruff")]

    def fake_plugin(repo_path, outcomes=None, discovery_scope=None, **kwargs):
        calls["scope"] = discovery_scope
        outcomes["plugin:plugin-python"] = {
            "status": "ran", "count": 4, "reason": "ruff ran",
        }
        return [
            make_finding(path="app.py", line=10, rule="broad-except",
                         snippet="plugin-dup"),
            make_finding(path="app.py", line=10, rule="ruff-F821",
                         snippet="plugin-dup"),
            make_finding(path="notes.py", line=1, rule="debt-todo-marker"),
            make_finding(path="notes.py", line=2, rule="trailing-whitespace"),
        ]

    monkeypatch.setattr(discovery_module, "_ast_scan_python_files", fake_ast)
    monkeypatch.setattr(
        discovery_module, "discover_python_linter_findings", fake_ruff
    )
    monkeypatch.setattr(discovery_module, "run_plugin_discovery", fake_plugin)

    outcomes = {}
    findings = discover_findings(
        repo,
        log_file=repo / "scan.log",
        docs_index_file=repo / "docs.json",
        outcomes=outcomes,
        discovery_scope="python",
    )
    assert calls["scope"] == "python"

    keys = [(f.path, f.line, f.rule.casefold()) for f in findings]
    assert len(keys) == len(set(keys)), "python findings must be deduplicated"
    assert ("notes.py", 1, "debt-todo-marker") not in keys  # regex text-scan rule
    assert ("notes.py", 2, "trailing-whitespace") in keys  # E501-mapped ruff rule
    # Native producers win shared keys; plugin duplicates are dropped.
    assert [f.snippet for f in findings if f.rule == "broad-except"] == ["native-ast"]
    assert [f.snippet for f in findings
            if f.rule.casefold() == "ruff-f821"] == ["native-ruff"]
    # The plugin outcome counter documents the filtered output.
    plugin_outcome = outcomes["plugin:plugin-python"]
    assert plugin_outcome["count"] == 3  # 4 scanned - 1 dropped text-scan rule
    assert "dropped" in plugin_outcome["reason"]


def test_verify_fix_closed_python_scope_rejects_injected_discover_fn(
    tmp_path, make_finding
):
    finding = make_finding(path="app.py", rule="broad-except")
    calls = []

    def injected(worktree, docs_index_file=None, **kwargs):
        calls.append(kwargs)
        return []  # claims the finding is gone

    # Ungated injected rescans carry no outcomes, so they must never close a
    # python-scoped finding — and must never be invoked at all.
    assert verify_fix_closed(
        tmp_path, finding, tmp_path / "log.txt",
        discover_fn=injected, discovery_scope="python",
    ) is False
    assert calls == []

    # Repository scope keeps the trusted-callback contract (test compat).
    assert verify_fix_closed(
        tmp_path, finding, tmp_path / "log2.txt", discover_fn=injected
    ) is True
    assert len(calls) == 1


def test_python_producer_gate_accepts_plugin_mapped_internal_rules():
    python_ran = {
        "ast-python": {"status": "ran"},
        "python-linter-ruff": {"status": "ran"},
        "plugin:plugin-python": {"status": "ran"},
    }
    # E501/E722/B001/S108 surface as these internal names from plugin-python's
    # ruff output; previously trailing-whitespace mapped only to the
    # (python-skipped) sandbox-string-detectors and could never verify.
    assert responsible_detector_ran("trailing-whitespace", python_ran)
    assert responsible_detector_ran("broad-except", python_ran)
    assert responsible_detector_ran("hardcoded-tmp-path", python_ran)
    # Repository scope is unchanged: the sandbox producer still qualifies.
    assert responsible_detector_ran(
        "trailing-whitespace", {"sandbox-string-detectors": {"status": "ran"}}
    )
    # No capable producer ran -> still fails closed.
    assert not responsible_detector_ran("trailing-whitespace", {
        "sandbox-string-detectors": {"status": "skipped"},
        "plugin:plugin-python": {"status": "skipped"},
    })


def test_verify_fix_closed_duplicate_rule_path_still_fails_closed(
    tmp_path, make_finding
):
    # Even with deduplicated discovery, a rescan that returns the same rule
    # on the same path (fresh line, duplicate identity) must not close.
    finding = make_finding(path="app.py", rule="broad-except", line=3)

    def dup_discover(worktree, docs_index_file=None, **kwargs):
        return [
            make_finding(path="app.py", rule="broad-except", line=3),
            make_finding(path="app.py", rule="broad-except", line=9),
        ]

    assert verify_fix_closed(
        tmp_path, finding, tmp_path / "log.txt", discover_fn=dup_discover
    ) is False
