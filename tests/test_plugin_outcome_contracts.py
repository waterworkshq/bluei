"""Use representative native tool output through actual shared adapters."""

import json
import sys

from bluei.app.plugins import ToolWrapperPlugin
from plugins.markdown.plugin import Plugin as MarkdownPlugin


def _tool(path, output):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write({output!r})\nraise SystemExit(1)\n")
    path.chmod(0o755)


def test_markdown_filename_keyed_json_is_completed_and_retained(tmp_path, monkeypatch):
    bin_dir = tmp_path / "tools"
    payload = {"README.md": [{"lineNumber": 2, "ruleNames": ["MD009"],
                               "ruleDescription": "Trailing spaces"}]}
    _tool(bin_dir / "markdownlint", json.dumps(payload))
    monkeypatch.setenv("PATH", str(bin_dir))
    (tmp_path / "README.md").write_text("# Title\ntext  \n")
    plugin = MarkdownPlugin()
    findings = plugin.discover(tmp_path, {})
    assert plugin.last_tool_outcome == "tool-ran"
    assert any(finding.path == "README.md" and finding.rule == "MD009" for finding in findings)


def test_npx_wrapper_uses_already_installed_local_tool_without_installing(tmp_path, monkeypatch):
    local = tmp_path / "node_modules/.bin/xo"
    _tool(local, "[]")
    monkeypatch.setenv("PATH", str(tmp_path / "no-global-tools"))
    class LocalPlugin(ToolWrapperPlugin):
        id = "local-xo"
        name = "Local tool"
        languages = ["javascript"]
        rules = []
        def parse_output(self, output, repo_path):
            return []
    plugin = LocalPlugin({"id": "local-xo", "languages": ["javascript"],
                          "discovery": {"tool": "npx", "tool_args": ["xo", "--reporter=json"]}})
    assert plugin._run_tool(tmp_path) == "[]"
    assert plugin.last_tool_outcome == "tool-ran"
    local.unlink()
    assert plugin._run_tool(tmp_path) is None
    assert plugin.last_tool_outcome == "tool-missing"
