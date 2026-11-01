"""Python-only discovery scope: early python branch in discover_findings,
manifest filtering in the plugin loader, and scoped verify_fix_closed."""

import json
import shutil

import pytest

import bluei.engine.orchestrator as orchestrator
import bluei.engine.verify as verify_module
import bluei.engine.discovery as discovery_module
from bluei.engine.discovery import discover_findings
from bluei.engine.models import stable_finding_id
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


def test_python_scope_runs_plugin_ruff_only_and_dedupes_catalog_codes(
    tmp_path, monkeypatch, make_finding
):
    # Scoped discovery asks plugin-python for a provenance-complete ruff-only
    # run: the config flag reaches the plugin, its raw ruff-<CODE> rules are
    # trusted without a rule-list filter or outcome-counter rewrite, and only
    # codes the native core ruff producer's catalog covers are dropped once
    # that core producer ran. Plugin-only codes keep every diagnostic.
    repo = _make_mixed_repo(tmp_path)
    calls = {}

    def fake_ast(repo_path, log_file, rule_meta, outcome=None):
        outcome["ast-python"] = {"status": "ran", "count": 0}
        return []

    def fake_ruff(repo_path, log_file, outcome=None):
        outcome["python-linter-ruff"] = {"status": "ran", "count": 1}
        return [make_finding(path="app.py", line=10, rule="ruff-e501",
                             snippet="native-ruff")]

    def fake_plugin(repo_path, outcomes=None, discovery_scope=None,
                    config=None, **kwargs):
        calls["scope"] = discovery_scope
        calls["config"] = config
        outcomes["plugin:plugin-python"] = {
            "status": "ran", "count": 3, "reason": "ruff ran",
        }
        return [
            make_finding(path="app.py", line=10, rule="ruff-E501",
                         snippet="plugin-dup"),
            make_finding(path="app.py", line=4, rule="ruff-F821",
                         finding_id="f821a",
                         snippet="Undefined name `alpha`"),
            make_finding(path="app.py", line=4, rule="ruff-F821",
                         finding_id="f821b",
                         snippet="Undefined name `beta`"),
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
    assert calls["config"] == {"plugin-python": {"ruff_only": True}}

    rules = [f.rule for f in findings]
    # E501 is native-catalog-covered: the plugin duplicate is dropped and the
    # native finding is the one that survives.
    assert rules.count("ruff-e501") == 1
    assert "ruff-E501" not in rules
    # Plugin-only code: both same-line F821 diagnostics are retained.
    f821 = [f for f in findings if f.rule == "ruff-F821"]
    assert [f.snippet for f in f821] == [
        "Undefined name `alpha`", "Undefined name `beta`",
    ]
    # No outcome-counter rewrite: the plugin outcome reports its own scan.
    assert outcomes["plugin:plugin-python"]["count"] == 3
    assert "dropped" not in outcomes["plugin:plugin-python"].get("reason", "")


def test_python_scope_keeps_plugin_ruff_rules_when_core_ruff_not_ran(
    tmp_path, monkeypatch, make_finding
):
    # Exclusions are provenance-based, not blanket: with the native core ruff
    # producer not having run, the plugin's raw rules — even catalog-covered
    # ones — are the only ruff coverage and must not be dropped.
    repo = _make_mixed_repo(tmp_path)

    def fake_ast(repo_path, log_file, rule_meta, outcome=None):
        outcome["ast-python"] = {"status": "ran", "count": 0}
        return []

    def fake_ruff(repo_path, log_file, outcome=None):
        outcome["python-linter-ruff"] = {
            "status": "unavailable", "count": 0,
            "reason": "ruff not found on PATH",
        }
        return []

    def fake_plugin(repo_path, outcomes=None, discovery_scope=None,
                    config=None, **kwargs):
        outcomes["plugin:plugin-python"] = {"status": "ran", "count": 1}
        return [make_finding(path="app.py", line=7, rule="ruff-E501")]

    monkeypatch.setattr(discovery_module, "_ast_scan_python_files", fake_ast)
    monkeypatch.setattr(
        discovery_module, "discover_python_linter_findings", fake_ruff
    )
    monkeypatch.setattr(discovery_module, "run_plugin_discovery", fake_plugin)

    findings = discover_findings(
        repo,
        log_file=repo / "scan.log",
        docs_index_file=repo / "docs.json",
        outcomes={},
        discovery_scope="python",
    )
    assert [f.rule for f in findings] == ["ruff-E501"]


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


def test_python_producer_gate_after_mapped_names_left_python_scope():
    python_ran = {
        "ast-python": {"status": "ran"},
        "python-linter-ruff": {"status": "ran"},
        "plugin:plugin-python": {"status": "ran"},
    }
    # Under the python scope plugin-python emits verbatim ruff-<CODE> rules
    # only; mapped internal names are no longer plugin candidates. Internal
    # names verify through their actual producers: broad-except and
    # hardcoded-tmp-path via ast-python.
    assert responsible_detector_ran("broad-except", python_ran)
    assert responsible_detector_ran("hardcoded-tmp-path", python_ran)
    # trailing-whitespace is a sandbox string rule: the (python-skipped)
    # sandbox detector is its producer, so python scope cannot verify it.
    assert not responsible_detector_ran("trailing-whitespace", python_ran)
    # Repository scope is unchanged: the sandbox producer still qualifies.
    assert responsible_detector_ran(
        "trailing-whitespace", {"sandbox-string-detectors": {"status": "ran"}}
    )
    # No capable producer ran -> still fails closed.
    assert not responsible_detector_ran("trailing-whitespace", {
        "sandbox-string-detectors": {"status": "skipped"},
        "plugin:plugin-python": {"status": "skipped"},
    })
    # Raw plugin rules verify through plugin-python (core catalog rules
    # accept either producer; plugin-only codes require the plugin).
    assert responsible_detector_ran("ruff-f821", python_ran)
    assert responsible_detector_ran(
        "ruff-f821", {"plugin:plugin-python": {"status": "ran"}}
    )
    assert not responsible_detector_ran("ruff-f821", {
        "python-linter-ruff": {"status": "ran"}
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


# ---------------------------------------------------------------------------
# Producer contract: plugin-python ruff-only mode
# ---------------------------------------------------------------------------


def _load_python_plugin(repo_path):
    from bluei.engine import plugin_loader

    plugins_dir = plugin_loader._find_plugins_dir(repo_path)
    assert plugins_dir is not None, "plugins directory not found"
    return plugin_loader._load_plugin_module("plugin-python", plugins_dir / "python")


def _ruff_json(repo_path, items):
    return json.dumps([
        {"code": code, "filename": str(repo_path / "app.py"),
         "location": {"row": row}, "message": message}
        for code, row, message in items
    ])


def test_plugin_ruff_only_mode_emits_no_text_scan_findings(
    tmp_path, monkeypatch
):
    # Comments that would fool the broad-except/TODO/whitespace regexes plus
    # a clean real-tool run: ruff-only mode must emit nothing.
    plugin = _load_python_plugin(tmp_path)
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "app.py").write_text(
        "# except: regex bait the text scanner would flag\n"
        "# TODO: bait\n"
        "x = 1\n"
    )
    monkeypatch.setattr(
        plugin, "_run_tool", lambda repo_path, extra_args=None: "[]"
    )
    assert plugin.discover(tmp_path, {"ruff_only": True}) == []
    # Default repository behavior is unchanged: same repo, same clean tool
    # run, text-scan fallback still fires.
    assert {f.rule for f in plugin.discover(tmp_path, {})} == {
        "broad-except", "debt-todo-marker",
    }


def test_plugin_ruff_only_mode_preserves_raw_rule_ids(tmp_path, monkeypatch):
    plugin = _load_python_plugin(tmp_path)
    (tmp_path / "setup.py").write_text("")
    raw = _ruff_json(tmp_path, [
        ("E501", 1, "line too long"),
        ("F821", 1, "Undefined name `a`"),
        ("F821", 1, "Undefined name `b`"),
    ])
    monkeypatch.setattr(plugin, "_run_tool",
                        lambda repo_path, extra_args=None: raw)

    findings = plugin.discover(tmp_path, {"ruff_only": True})
    # No mapped internal names in this mode; same-line F821 diagnostics are
    # distinct findings.
    assert [f.rule for f in findings] == [
        "ruff-E501", "ruff-F821", "ruff-F821",
    ]
    assert [f.snippet for f in findings if f.rule == "ruff-F821"] == [
        "Undefined name `a`", "Undefined name `b`",
    ]
    # Default parse keeps mapping codes to internal rule names.
    assert [f.rule for f in plugin.parse_output(raw, tmp_path)] == [
        "trailing-whitespace", "ruff-F821", "ruff-F821",
    ]


def test_plugin_raw_f821_finding_id_is_verbatim_rule_hash(tmp_path):
    # The actual F821 identity is unchanged: the deterministic id hashes the
    # verbatim "ruff-F821" rule and the diagnostic message.
    plugin = _load_python_plugin(tmp_path)
    raw = _ruff_json(tmp_path, [("F821", 3, "Undefined name `ghost`")])
    finding = plugin.parse_output(raw, tmp_path, raw_rule_ids=True)[0]
    assert finding.rule == "ruff-F821"
    assert finding.finding_id == stable_finding_id(
        str(tmp_path), "app.py", 3, "ruff-F821", "Undefined name `ghost`"
    )


@needs_ruff
def test_python_scope_clean_tool_run_emits_no_regex_findings(tmp_path):
    # End-to-end regression: comment text the old regex text-scan branch
    # matched is not a finding when the actual ruff tool run is clean.
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "app.py").write_text(
        "# except: regex bait the text scanner would flag\n"
        "# TODO: bait\n"
        "x = 1\n"
    )
    outcomes = {}
    findings = discover_findings(
        tmp_path,
        log_file=tmp_path / "scan.log",
        docs_index_file=tmp_path / "docs.json",
        outcomes=outcomes,
        discovery_scope="python",
    )
    assert findings == []
    assert outcomes["plugin:plugin-python"]["status"] == "ran"
    assert outcomes["plugin:plugin-python"].get("tool_outcome") == "tool-ran"


@needs_ruff
def test_python_scope_identical_f821_ids_are_deduplicated(tmp_path):
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "app.py").write_text("value = ghost + ghost + other\n")
    findings = discover_findings(tmp_path, log_file=tmp_path / "scan.log", outcomes={}, discovery_scope="python")
    undefined = [finding for finding in findings if finding.rule == "ruff-F821"]
    assert len(undefined) == 2
    assert {finding.snippet for finding in undefined} == {"Undefined name `ghost`", "Undefined name `other`"}


@needs_ruff
def test_python_scope_bare_except_keeps_contextual_ast_identity(tmp_path):
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "app.py").write_text("try:\n    work()\nexcept:\n    pass\n")
    findings = discover_findings(tmp_path, log_file=tmp_path / "scan.log", outcomes={}, discovery_scope="python")
    assert len([finding for finding in findings if finding.rule == "broad-except"]) == 1
    assert not any(finding.rule.casefold() == "ruff-e722" for finding in findings)


@needs_ruff
def test_python_scope_e501_native_not_duplicated_by_plugin(tmp_path):
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "app.py").write_text("x = '" + "a" * 100 + "'\n")
    findings = discover_findings(
        tmp_path,
        log_file=tmp_path / "scan.log",
        docs_index_file=tmp_path / "docs.json",
        outcomes={},
        discovery_scope="python",
    )
    e501 = [f for f in findings if f.rule.casefold() == "ruff-e501"]
    # Exactly one E501 finding: the native core catalog producer's.
    assert len(e501) == 1
    assert e501[0].rule == "ruff-e501"


@needs_ruff
def test_python_scope_keeps_both_f821_diagnostics_on_one_line(tmp_path):
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "app.py").write_text(
        "print(alpha_undefined, beta_undefined)\n"
    )
    findings = discover_findings(
        tmp_path,
        log_file=tmp_path / "scan.log",
        docs_index_file=tmp_path / "docs.json",
        outcomes={},
        discovery_scope="python",
    )
    f821 = [f for f in findings if f.rule == "ruff-F821"]
    assert len(f821) == 2
    assert {f.line for f in f821} == {1}
    assert len({f.finding_id for f in f821}) == 2
    assert len({f.snippet for f in f821}) == 2
