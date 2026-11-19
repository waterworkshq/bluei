"""Canonical Ruff discovery: the python plugin and the native ruff producer
emit identical identities for the same diagnostic.

Contract under test (A05 minimal canonical Ruff):
- rules are lowercase ``ruff-<code>`` in both lanes (``raw_rule_ids`` stays
  for signature compatibility only);
- E501 is never trailing-whitespace, E722/B001/S108 keep their ruff
  identity (no semantic map to internal rule names); internal text-scan
  rules still exist for what they actually detect;
- confidence comes from DETECTOR_CATALOG for catalog codes (0.88 fallback
  for plugin-only codes), identical across producers;
- ``safe_to_autofix`` follows the per-instance ``fix.applicability``
  (safe/generated) with the ruff-c408 false override, in both producers;
- the finding id hashes the same 200-truncated message in both producers,
  so a true duplicate gets one finding (prefer native) while plugin-only
  instances always survive python-scope dedupe.

Tests that exercise the real ruff binary REQUIRE it on PATH (a missing
tool fails with a clear assertion instead of skipping); the rest are
static fixture parses.
"""

import json
import shutil
import subprocess
from types import SimpleNamespace

from bluei.engine.discovery import discover_findings
from bluei.engine.models import stable_finding_id


def _require_ruff():
    """Qualification tests need the real ruff binary: a missing tool must
    FAIL, not skip — silence would hide the very regression class these
    tests guard (empty discovery fabricating clean health)."""
    path = shutil.which("ruff")
    assert path is not None, (
        "canonical ruff qualification requires the real ruff binary on PATH; "
        "tool absence must fail the qualification, not skip it"
    )
    return path


def _load_plugin():
    from plugins.python.plugin import Plugin

    return Plugin()


def _ruff_json(items):
    """One JSON diagnostic per (code, filename, row, message, fix) tuple."""
    return json.dumps([
        {
            "code": code,
            "filename": filename,
            "location": {"row": row},
            "message": message,
            **({"fix": {"applicability": fix}} if fix is not None else {}),
        }
        for code, filename, row, message, fix in items
    ])


# ---------------------------------------------------------------------------
# Static parity: plugin vs native producer on identical JSON
# ---------------------------------------------------------------------------


def test_plugin_parse_emits_canonical_lowercase_rules(tmp_path):
    plugin = _load_plugin()
    raw = _ruff_json([
        ("E501", "app.py", 1, "line too long (90 > 88)", None),
        ("E722", "app.py", 2, "Do not use bare `except`", None),
        ("B001", "app.py", 3, "Do not use bare `except`", None),
        ("S108", "app.py", 4, "Probable insecure usage of temp file", None),
        ("F821", "app.py", 5, "Undefined name `ghost`", None),
        ("B904", "app.py", 6, "raise without from inside except", None),
    ])
    rules = [f.rule for f in plugin.parse_output(raw, tmp_path)]
    assert rules == [
        "ruff-e501", "ruff-e722", "ruff-b001", "ruff-s108",
        "ruff-f821", "ruff-b904",
    ]
    # E501 is line-too-long: it must never masquerade as trailing
    # whitespace, and E722/B001/S108 keep their ruff identity.
    assert "trailing-whitespace" not in rules
    assert "broad-except" not in rules
    assert "hardcoded-tmp-path" not in rules


def test_plugin_and_native_share_finding_id_for_same_diagnostic(
    tmp_path, monkeypatch
):
    """The core cross-producer guarantee: identical diagnostic JSON from the
    plugin parse and the native linters parse yields the same finding id,
    rule, confidence, and autofix safety."""
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "app.py").write_text("x = 1\n")
    message = "line too long (90 > 88) " + "y" * 300  # force truncation
    raw = _ruff_json([("E501", str(tmp_path / "app.py"), 7, message, None)])

    plugin_findings = _load_plugin().parse_output(raw, tmp_path)

    from bluei.engine import linters

    monkeypatch.setattr(linters, "_resolve_repository_tool", lambda *a, **k: "/usr/bin/ruff")
    monkeypatch.setattr(
        linters.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout=raw),
    )
    native_findings = linters.discover_python_linter_findings(
        tmp_path, tmp_path / "log.txt"
    )

    assert len(plugin_findings) == 1 and len(native_findings) == 1
    pf, nf = plugin_findings[0], native_findings[0]
    assert pf.finding_id == nf.finding_id
    assert pf.rule == nf.rule == "ruff-e501"
    assert pf.confidence == nf.confidence == 0.65  # catalog confidence
    assert pf.safe_to_autofix == nf.safe_to_autofix is False  # no fix
    # Both hash the 200-truncated message.
    assert pf.finding_id == stable_finding_id(
        str(tmp_path), "app.py", 7, "ruff-e501", message[:200]
    )


def test_applicability_matrix_and_c408_override(tmp_path):
    plugin = _load_plugin()
    raw = _ruff_json([
        ("W291", "a.py", 1, "trailing whitespace", "safe"),
        ("UP006", "a.py", 2, "use dict", "generated"),
        ("F401", "a.py", 3, "unused import", "unsafe"),
        ("C408", "a.py", 4, "dict() rewrite", "safe"),  # override -> False
        ("B904", "a.py", 5, "raise without from", None),
        ("F821", "a.py", 6, "undefined name", None),
    ])
    findings = _load_plugin().parse_output(raw, tmp_path)
    by_rule = {f.rule: f for f in findings}
    assert by_rule["ruff-w291"].safe_to_autofix is True
    assert by_rule["ruff-w291"].quick_win is True
    assert by_rule["ruff-up006"].safe_to_autofix is True  # generated == safe
    assert by_rule["ruff-f401"].safe_to_autofix is False  # unsafe fix
    assert by_rule["ruff-c408"].safe_to_autofix is False  # forced override
    assert by_rule["ruff-c408"].quick_win is False
    assert by_rule["ruff-b904"].safe_to_autofix is False  # unsupported
    assert by_rule["ruff-f821"].safe_to_autofix is False  # unsupported


def test_confidence_catalog_vs_plugin_only_consistency(tmp_path):
    raw = _ruff_json([
        ("E501", "a.py", 1, "m", None),
        ("B904", "a.py", 2, "m", None),
        ("B007", "a.py", 3, "m", None),
        ("F821", "a.py", 4, "m", None),
        ("E722", "a.py", 5, "m", None),
    ])
    by_rule = {f.rule: f for f in _load_plugin().parse_output(raw, tmp_path)}
    # Catalog codes take the catalog confidence — identical to what the
    # native producer reports for the same rule.
    assert by_rule["ruff-e501"].confidence == 0.65
    assert by_rule["ruff-b904"].confidence == 0.8
    assert by_rule["ruff-b007"].confidence == 0.75
    # Plugin-only codes keep the single plugin-only fallback.
    assert by_rule["ruff-f821"].confidence == 0.88
    assert by_rule["ruff-e722"].confidence == 0.88


def test_malformed_code_skipped(tmp_path):
    raw = _ruff_json([
        ("", "a.py", 1, "empty code", None),
        ("NOT-A-RULE", "a.py", 2, "malformed", None),
        ("E501", "a.py", 3, "fine", None),
    ])
    findings = _load_plugin().parse_output(raw, tmp_path)
    assert [f.rule for f in findings] == ["ruff-e501"]


def test_raw_rule_ids_flag_keeps_canonical_output(tmp_path):
    plugin = _load_plugin()
    raw = _ruff_json([("F821", "a.py", 1, "Undefined name `x`", None)])
    assert [f.rule for f in plugin.parse_output(raw, tmp_path)] == \
        [f.rule for f in plugin.parse_output(raw, tmp_path, raw_rule_ids=True)]


# ---------------------------------------------------------------------------
# Real ruff: repeated diagnostics across scopes
# ---------------------------------------------------------------------------


def _make_canonical_repo(tmp_path):
    """Python repo whose ruff config selects the fixture rules for both
    producers (plugin honors repo config; native --select=B,E,W,F,S,C4
    covers them all)."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\nexclude = []\n[tool.ruff.lint]\nselect = ["B904", "E501", "F821", "W291"]\n'
    )
    # app.py: two F821 diagnostics on one line (1), a long line (2, E501),
    # trailing whitespace (3, W291), and a raise-without-from (8, B904).
    (tmp_path / "app.py").write_text(
        "print(alpha_undefined, beta_undefined)\n"
        "value = '" + "a" * 120 + "'\n"
        "x = 1   \n"
        "def work():\n"
        "    pass\n"
        "try:\n"
        "    work()\n"
        "except Exception:\n"
        "    raise ValueError('boom')\n"
    )
    # src/more.py: repeated B904 (line 6) and E501 (line 7).
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "more.py").write_text(
        "def work():\n"
        "    pass\n"
        "try:\n"
        "    work()\n"
        "except Exception:\n"
        "    raise ValueError('again')\n"
        "label = '" + "b" * 120 + "'\n"
    )
    return tmp_path


def test_real_ruff_raw_json_parse_output_is_non_empty(tmp_path):
    """Regression guard for the helper-contract bug class: feeding real
    ruff JSON through parse_output must yield findings, never a silently
    empty list (empty discovery would fabricate clean health)."""
    ruff = _require_ruff()
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "app.py").write_text("value = ghost\n")
    result = subprocess.run(
        [ruff, "check", "--isolated", "--no-cache",
         "--select", "F821", "--output-format", "json", "--", "app.py"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1, result.stderr
    findings = _load_plugin().parse_output(result.stdout, tmp_path)
    assert findings, "parse_output returned no findings for real ruff JSON"
    assert findings[0].rule == "ruff-f821"
    assert findings[0].path == "app.py"


def test_real_ruff_repeated_diagnostics_python_scope(tmp_path):
    _require_ruff()
    repo = _make_canonical_repo(tmp_path)
    findings = discover_findings(
        repo,
        log_file=repo / "scan.log",
        docs_index_file=repo / "docs.json",
        outcomes={},
        discovery_scope="python",
    )
    # Everything canonical lowercase; no legacy uppercase anywhere.
    ruff_findings = [f for f in findings if f.rule.startswith("ruff-")]
    assert ruff_findings
    assert all(f.rule == f.rule.lower() for f in ruff_findings)

    # Repeated F821 on one line: both diagnostics survive as distinct ids.
    f821 = [f for f in findings if f.rule == "ruff-f821"]
    assert len(f821) == 2 and {f.line for f in f821} == {1}
    assert len({f.finding_id for f in f821}) == 2

    # Repeated B904 across two files: one instance per raise, never merged
    # by rule alone.
    b904 = [f for f in findings if f.rule == "ruff-b904"]
    assert len(b904) == 2
    assert {(f.path, f.line) for f in b904} == {("app.py", 9), ("src/more.py", 6)}
    # Unsupported: B904 has no fix — never autofix, never quick win.
    assert all(f.safe_to_autofix is False and f.quick_win is False for f in b904)
    assert all(f.confidence == 0.8 for f in b904)

    # Repeated E501 across files: one instance per long line, and never
    # mislabeled as trailing-whitespace.
    e501 = [f for f in findings if f.rule == "ruff-e501"]
    assert {(f.path, f.line) for f in e501} == {("app.py", 2), ("src/more.py", 7)}
    assert all(f.rule != "trailing-whitespace" for f in e501)

    # W291 carries a safe fix: safe_to_autofix True from actual
    # applicability (plugin lane; the catalog has no ruff-w291 entry).
    w291 = [f for f in findings if f.rule == "ruff-w291"]
    assert len(w291) == 1 and w291[0].safe_to_autofix is True

    # Cross-producer dedupe: identical diagnostics appear exactly once even
    # though both native ruff and plugin-python ran (outcomes prove both).
    # E501 at app.py:2 is in the native catalog: exactly one finding.
    assert len([f for f in e501 if f.path == "app.py"]) == 1


def test_real_ruff_repo_scope_keeps_internal_text_scan_rules(tmp_path):
    _require_ruff()
    repo = _make_canonical_repo(tmp_path)
    findings = discover_findings(
        repo,
        log_file=repo / "scan.log",
        docs_index_file=repo / "docs.json",
        outcomes={},
    )
    rules = {f.rule for f in findings}
    # Canonical tool identity present in the default scope too.
    assert "ruff-w291" in rules
    assert "ruff-e501" in rules
    assert not any(r[0].isupper() for r in rules if r.startswith("ruff-"))
    # The internal text-scan rules still exist for what they really detect
    # (the W291 line also trips the regex scanner) — distinct identity from
    # the tool finding at the same line.
    assert "trailing-whitespace" in rules
    same_line = [
        f for f in findings
        if f.path == "app.py" and f.line == 3
        and f.rule in ("ruff-w291", "trailing-whitespace")
    ]
    assert {f.rule for f in same_line} == {"ruff-w291", "trailing-whitespace"}
    # Repository scope runs both the native ruff producer and the plugin:
    # identical diagnostics must appear exactly once (canonical merge key).
    identities = [(f.path, f.line, f.rule, f.snippet) for f in findings]
    assert len(identities) == len(set(identities))
    e501 = [f for f in findings if f.rule == "ruff-e501"]
    assert len(e501) == 2  # one per long line: native + plugin merged
    assert len({f.finding_id for f in e501}) == 2


def test_real_ruff_repo_scope_ast_guard_prefers_broad_except(tmp_path):
    _require_ruff()
    # Same AST location guard as the python scope: where the AST scan
    # already reported broad-except, the plugin's ruff-e722 for that bare
    # except is dropped instead of double-reporting it.
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\nexclude = []\n[tool.ruff.lint]\nselect = ["E722"]\n'
    )
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "app.py").write_text("try:\n    work\nexcept:\n    pass\n")
    findings = discover_findings(
        tmp_path,
        log_file=tmp_path / "scan.log",
        docs_index_file=tmp_path / "docs.json",
        outcomes={},
    )
    bare = [f for f in findings if f.rule == "broad-except"]
    assert bare, "AST broad-except expected"
    assert not [
        f for f in findings if f.rule.casefold() == "ruff-e722"
    ], "plugin ruff-e722 must yield to the AST broad-except identity"
