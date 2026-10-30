"""Truthful detector outcomes for parser/grammar and string-detector applicability.

Reviews 4422195958 / 4422209763: a detector must never record ``ran`` when it
did not actually examine anything — empty repos, missing tree-sitter grammars,
and unreadable files must surface as skipped / unavailable / failed.
"""

from pathlib import Path

from bluei.engine.ast_engine.ts_matcher import TSPatternMatcher
from bluei.engine.ast_engine.ts_parser import TreeSitterAdapter
from bluei.engine.discovery import (
    LANGUAGE_SCAN_CONFIGS,
    ast_scan_pipeline,
    discover_findings,
)


def _scan(repo_path: Path, lang: str):
    outcome: dict = {}
    findings = ast_scan_pipeline(
        repo_path, repo_path.parent / "run.log", {}, LANGUAGE_SCAN_CONFIGS[lang], outcome
    )
    return outcome[f"ast-{lang}"], findings


def test_empty_repo_records_no_ran_detector(tmp_path):
    """On an actually empty repo every detector must be skipped, none ran."""
    repo = tmp_path / "empty"
    repo.mkdir()
    outcomes: dict = {}
    discover_findings(
        repo,
        log_file=tmp_path / "run.log",
        docs_index_file=tmp_path / "docs-index.yaml",
        outcomes=outcomes,
    )
    assert outcomes, "discover_findings must record per-detector outcomes"
    ran = {name: entry for name, entry in outcomes.items() if entry["status"] == "ran"}
    assert not ran, f"empty repo fabricated coverage: {ran}"
    assert outcomes["sandbox-string-detectors"]["status"] == "skipped"
    assert outcomes["python-linter-ruff"]["status"] == "skipped"
    # Plugin stage: per-plugin records (the loader falls back to bluei's own
    # plugin manifests) must all report skipped for a repo with no languages.
    plugin_keys = [k for k in outcomes if k.startswith("plugin:")]
    assert plugin_keys, "expected per-plugin outcome records"
    assert all(outcomes[k]["status"] == "skipped" for k in plugin_keys)
    for lang in ("python", "typescript", "go", "rust"):
        assert outcomes[f"ast-{lang}"]["status"] == "skipped"


def test_string_detectors_ran_only_when_configured_inputs_present(tmp_path):
    """Unrelated files do not apply; a real configured input does."""
    repo = tmp_path / "unrelated"
    repo.mkdir()
    (repo / "notes.txt").write_text("no configured detector input here\n")
    outcomes: dict = {}
    discover_findings(
        repo,
        log_file=tmp_path / "run.log",
        docs_index_file=tmp_path / "docs-index.yaml",
        outcomes=outcomes,
    )
    assert outcomes["sandbox-string-detectors"]["status"] == "skipped"

    readme_repo = tmp_path / "with-readme"
    readme_repo.mkdir()
    (readme_repo / "README.md").write_text("# project\n")
    outcomes2: dict = {}
    discover_findings(
        readme_repo,
        log_file=tmp_path / "run2.log",
        docs_index_file=tmp_path / "docs-index2.yaml",
        outcomes=outcomes2,
    )
    assert outcomes2["sandbox-string-detectors"] == {
        "status": "ran",
        "count": 0,
        "reason": "qa-sandbox fixture string detectors (path-scoped)",
    }


def test_missing_grammar_records_unavailable_not_ran(tmp_path, monkeypatch):
    """A missing per-language grammar must not silently scan nothing as ran."""

    original_get_parser = TreeSitterAdapter._get_parser

    def fake_get_parser(self, language: str):
        return None if language == "go" else original_get_parser(self, language)

    monkeypatch.setattr(TreeSitterAdapter, "_get_parser", fake_get_parser)

    repo = tmp_path / "go-repo"
    repo.mkdir()
    (repo / "main.go").write_text("package main\n\nfunc main() {}\n")

    outcome, findings = _scan(repo, "go")
    assert outcome["status"] == "unavailable"
    assert "grammar" in outcome["reason"] and "go" in outcome["reason"]
    assert findings == []


def test_missing_grammar_for_one_present_language_of_a_family(tmp_path, monkeypatch):
    """In the ts/js family only the grammars for present files are required."""

    original_get_parser = TreeSitterAdapter._get_parser

    def fake_get_parser(self, language: str):
        return None if language == "javascript" else original_get_parser(self, language)

    monkeypatch.setattr(TreeSitterAdapter, "_get_parser", fake_get_parser)

    repo = tmp_path / "ts-only"
    repo.mkdir()
    (repo / "app.ts").write_text("export const x: number = 1;\n")

    outcome, _ = _scan(repo, "typescript")
    assert outcome["status"] == "ran"

    js_repo = tmp_path / "with-js"
    js_repo.mkdir()
    (js_repo / "app.js").write_text("const x = 1;\n")
    outcome_js, _ = _scan(js_repo, "typescript")
    assert outcome_js["status"] == "unavailable"
    assert "javascript" in outcome_js["reason"]


def test_unreadable_file_records_failed_not_ran(tmp_path):
    """A file with invalid UTF-8 was never scanned; the outcome must say so."""
    repo = tmp_path / "bad-encoding"
    repo.mkdir()
    (repo / "app.ts").write_bytes(b"\xff\xfe\x00bad utf8 \xff")

    outcome, _ = _scan(repo, "typescript")
    assert outcome["status"] == "failed"
    assert "app.ts" in outcome["reason"]


def test_python_syntax_error_records_failed_not_ran(tmp_path):
    repo = tmp_path / "bad-python"
    repo.mkdir()
    (repo / "broken.py").write_text("def (:\n")

    outcome, _ = _scan(repo, "python")
    assert outcome["status"] == "failed"
    assert "broken.py" in outcome["reason"]


def test_ts_matcher_accounts_failed_parse(tmp_path, monkeypatch):
    """TSPatternMatcher follows the ASTPatternMatcher failure contract."""
    monkeypatch.setattr(TreeSitterAdapter, "parse", lambda self, source: None)
    matcher = TSPatternMatcher()
    assert matcher.find_matches("const x = 1;", "app.ts", "typescript") == []
    assert matcher.last_parse_failures == ["app.ts"]

    # The pipeline picks the failure up through the shared attribute.
    repo = tmp_path / "ts-repo"
    repo.mkdir()
    (repo / "app.ts").write_text("export const x: number = 1;\n")
    outcome, _ = _scan(repo, "typescript")
    assert outcome["status"] == "failed"
    assert "app.ts" in outcome["reason"]


def test_javascript_never_falls_back_to_typescript_grammar(tmp_path, monkeypatch):
    """JS must parse with the JS grammar or be unavailable — never the TS one."""
    import sys

    _missing = object()
    original_cache = TreeSitterAdapter._parsers.get("javascript", _missing)
    TreeSitterAdapter._parsers.pop("javascript", None)
    # A None entry in sys.modules makes ``import tree_sitter_javascript`` fail.
    monkeypatch.setitem(sys.modules, "tree_sitter_javascript", None)
    try:
        assert TreeSitterAdapter.language_available("typescript")
        assert not TreeSitterAdapter.language_available("javascript")

        repo = tmp_path / "js-repo"
        repo.mkdir()
        (repo / "app.js").write_text("const x = 1;\n")
        outcome, _ = _scan(repo, "typescript")
        assert outcome["status"] == "unavailable"
        assert "javascript" in outcome["reason"]
    finally:
        # A cached None would poison later scans; drop it and restore reality.
        TreeSitterAdapter._parsers.pop("javascript", None)
        if original_cache is not _missing:
            TreeSitterAdapter._parsers["javascript"] = original_cache


def test_tsx_uses_the_tsx_grammar():
    assert TreeSitterAdapter.language_available("tsx")
    adapter = TreeSitterAdapter("tsx")
    assert adapter.parse("const x = <div>hi</div>;") is not None


def test_syntax_error_tree_records_failed_not_clean_scan(tmp_path):
    """A tree with ERROR nodes is a failed parse, not a clean empty scan."""
    repo = tmp_path / "bad-ts"
    repo.mkdir()
    (repo / "app.ts").write_text("export const = ;\n")
    outcome, _ = _scan(repo, "typescript")
    assert outcome["status"] == "failed"
    assert "app.ts" in outcome["reason"]
