"""markdownlint-cli writes --json violations to stderr and stays quiet on success."""
import json
import sys

import pytest

from plugins.markdown.plugin import Plugin


@pytest.mark.parametrize("stdout,stderr,code,completed", [
    ("", json.dumps([{"fileName": "README.md", "lineNumber": 1, "ruleNames": ["MD041"],
                      "ruleDescription": "First heading"}]), 1, True),
    ("", "", 0, True),
    ("", "not-json", 1, False),
    ("", "", 1, False),
    ("", "fatal tool error", 2, False),
])
def test_markdownlint_native_streams(tmp_path, monkeypatch, stdout, stderr, code, completed):
    tools = tmp_path / "tools"
    tools.mkdir()
    tool = tools / "markdownlint"
    tool.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write({stdout!r})\n"
                    f"sys.stderr.write({stderr!r})\nraise SystemExit({code})\n")
    tool.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("plain text\n")
    monkeypatch.setenv("PATH", str(tools))
    plugin = Plugin()
    output = plugin._run_tool(repo, extra_args=["README.md"])
    assert plugin.last_tool_outcome == ("tool-ran" if completed else "tool-error")
    if not completed:
        assert output is None
    elif code == 0:
        assert json.loads(output) == []
    else:
        findings = plugin.parse_output(output, repo)
        assert len(findings) == 1
        assert findings[0].path == "README.md" and findings[0].rule == "md-MD041"


@pytest.mark.parametrize("payload", [
    "[null]",
    json.dumps([{"fileName": "README.md"}]),  # missing lineNumber
    json.dumps([{"fileName": "README.md", "lineNumber": 0}]),
    json.dumps([{"fileName": "README.md", "lineNumber": "2"}]),
    json.dumps([{"lineNumber": 2}]),  # missing file target
    json.dumps([{"fileName": "", "lineNumber": 2}]),
    json.dumps({"README.md": [None]}),  # dict-shaped null diagnostic
    json.dumps({"": [{"lineNumber": 2, "ruleNames": ["MD009"]}]}),
    # entry carries a valid fileName but the dict key is empty — the parser
    # uses the key, so the validator must judge the key, not the entry field
    json.dumps({"": [{"fileName": "README.md", "lineNumber": 2, "ruleNames": ["MD009"]}]}),
    # malformed selected rule must read as tool-error, never tool-ran
    json.dumps([{"fileName": "README.md", "lineNumber": 2, "ruleNames": [42]}]),
    json.dumps([{"fileName": "README.md", "lineNumber": 2, "ruleNames": [""]}]),
    json.dumps([{"fileName": "README.md", "lineNumber": 2, "rule": 7}]),
])
def test_malformed_diagnostics_never_read_as_clean(tmp_path, monkeypatch, payload):
    tools = tmp_path / "tools"
    tools.mkdir()
    tool = tools / "markdownlint"
    tool.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write({payload!r})\n"
                    "raise SystemExit(1)\n")
    tool.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("plain text \n")
    monkeypatch.setenv("PATH", str(tools))
    plugin = Plugin()
    output = plugin._run_tool(repo, extra_args=["README.md"])
    assert plugin.last_tool_outcome == "tool-error"
    assert output is None


def test_filename_keyed_shape_still_completes(tmp_path, monkeypatch):
    payload = json.dumps({"README.md": [{"lineNumber": 2, "ruleNames": ["MD009"],
                                         "ruleDescription": "Trailing spaces"}]})
    tools = tmp_path / "tools"
    tools.mkdir()
    tool = tools / "markdownlint"
    tool.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write({payload!r})\n"
                    "raise SystemExit(1)\n")
    tool.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Title\ntext \n")
    monkeypatch.setenv("PATH", str(tools))
    plugin = Plugin()
    output = plugin._run_tool(repo, extra_args=["README.md"])
    assert plugin.last_tool_outcome == "tool-ran"
    findings = plugin.parse_output(output, repo)
    assert [f.rule for f in findings] == ["md-MD009"]


def test_native_rule_ids_are_canonical_md_prefixed(tmp_path):
    """Native MD009 and fallback md-MD009 share one ID — no false closure."""
    plugin = Plugin()
    findings = plugin.parse_output(
        json.dumps([{"fileName": "README.md", "lineNumber": 3,
                     "ruleNames": ["MD009"], "ruleDescription": "Trailing spaces"}]),
        tmp_path,
    )
    assert findings[0].rule == "md-MD009"
    assert findings[0].rule in plugin.rules  # health manifest prefix contract


@pytest.mark.parametrize("item", [
    {"fileName": "README.md", "lineNumber": 2, "ruleNames": [42], "ruleDescription": "x"},
    {"fileName": "README.md", "lineNumber": 2, "ruleNames": [""], "ruleDescription": "x"},
    {"fileName": "README.md", "lineNumber": 2, "ruleNames": [], "rule": 7, "ruleDescription": "x"},
    {"fileName": "README.md", "lineNumber": 2, "rule": 7, "ruleDescription": "x"},
])
def test_direct_parse_drops_malformed_rule_instead_of_str_coercing(tmp_path, item):
    """Direct parse_output must not manufacture rules via str() coercion."""
    plugin = Plugin()
    assert plugin.parse_output(json.dumps([item]), tmp_path) == []


def test_direct_parse_keeps_legacy_empty_rule_names_md000_fallback(tmp_path):
    """Legacy direct parses with no rule info default to MD000 (strict wrapper
    rejects these; only the MD000 default keeps old callers working)."""
    plugin = Plugin()
    findings = plugin.parse_output(
        json.dumps([{"fileName": "a.md", "lineNumber": 1,
                     "ruleNames": [], "ruleDescription": "x"}]),
        tmp_path,
    )
    assert [f.rule for f in findings] == ["md-MD000"]
