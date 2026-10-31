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
        assert findings[0].path == "README.md" and findings[0].rule == "MD041"
