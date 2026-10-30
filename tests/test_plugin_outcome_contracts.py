"""Use representative native tool output through actual shared adapters."""

import json
import sys

import pytest

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
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Title\ntext  \n")
    plugin = MarkdownPlugin()
    findings = plugin.discover(repo, {})
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
    assert plugin._run_tool(tmp_path) is None
    assert plugin.last_tool_outcome == "tool-missing"
    monkeypatch.setenv("BLUEI_TRUST_REPOSITORY_TOOLS", "1")
    assert plugin._run_tool(tmp_path) == "[]"
    assert plugin.last_tool_outcome == "tool-ran"
    local.unlink()
    assert plugin._run_tool(tmp_path) is None
    assert plugin.last_tool_outcome == "tool-missing"


@pytest.mark.parametrize("tool", ["ruff", "shellcheck", "hadolint"])
def test_non_npx_repository_executable_requires_trust(tmp_path, monkeypatch, tool):
    from plugins.python.plugin import Plugin as PythonPlugin
    repo = tmp_path / "repo"
    repo.mkdir()
    binary = repo / "bin" / tool
    marker = tmp_path / "executed"
    _tool(binary, "[]")
    binary.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\nprint('[]')\n")
    plugin = PythonPlugin()
    plugin._manifest["discovery"] = {"tool": tool, "tool_args": []}
    monkeypatch.delenv("BLUEI_TRUST_REPOSITORY_TOOLS", raising=False)
    monkeypatch.setenv("PATH", str(binary.parent))
    assert plugin._run_tool(repo) is None
    assert plugin.last_tool_outcome == "tool-missing"
    assert not marker.exists()
    monkeypatch.setenv("BLUEI_TRUST_REPOSITORY_TOOLS", "1")
    assert plugin._run_tool(repo) == "[]\n"
    assert marker.exists()


def test_external_xo_requires_repository_configuration_trust(tmp_path, monkeypatch):
    from plugins.typescript.plugin import Plugin as TypeScriptPlugin
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "executed"
    binary = tmp_path / "tools/xo"
    _tool(binary, "[]")
    binary.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\nprint('[]')\n")
    (repo / "xo.config.js").write_text("export default {};\n")
    monkeypatch.delenv("BLUEI_TRUST_REPOSITORY_TOOLS", raising=False)
    monkeypatch.setenv("PATH", str(binary.parent))
    plugin = TypeScriptPlugin()
    assert plugin._run_tool(repo) is None
    assert not marker.exists()
    monkeypatch.setenv("BLUEI_TRUST_REPOSITORY_TOOLS", "1")
    assert plugin._run_tool(repo) == "[]\n"
    assert marker.exists()
