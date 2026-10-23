"""Tests for the ADR-0020 authoring-template gate in scripts/run_foundry.py.

D3-05: the Foundry's LLM authoring command must come from the configured
``foundry.claude_author_cmd_template``; absent/empty template -> Foundry
DISABLED (stderr message + exit 0), never a hardcoded ``["claude", "-p"]``.

The script is not part of the ``bluei`` package, so it is loaded from its
file path via importlib; every behavior under test is a plain function or
``main(config=...)`` with an injected config, so no subprocess or real
workspace config is touched.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import MappingProxyType

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "run_foundry.py"
)


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("run_foundry_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestResolveAuthorCmdTemplate:
    def test_present_template_returned(self, script):
        config = {"foundry": {"claude_author_cmd_template": "claude -p {prompt}"}}
        assert script.resolve_author_cmd_template(config) == "claude -p {prompt}"

    def test_absent_foundry_section_returns_none(self, script):
        assert script.resolve_author_cmd_template({}) is None

    def test_absent_key_returns_none(self, script):
        assert script.resolve_author_cmd_template({"foundry": {}}) is None

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_blank_template_returns_none(self, script, blank):
        config = {"foundry": {"claude_author_cmd_template": blank}}
        assert script.resolve_author_cmd_template(config) is None

    def test_non_string_template_returns_none(self, script):
        config = {"foundry": {"claude_author_cmd_template": 42}}
        assert script.resolve_author_cmd_template(config) is None

    def test_non_mapping_config_returns_none(self, script):
        assert script.resolve_author_cmd_template(MappingProxyType({})) is None


class TestBuildAuthorCommand:
    def test_placeholder_token_replaced_by_whole_prompt(self, script):
        prompt = "line one\nline two with 'quotes' and $hell"
        argv = script.build_author_command("claude -p {prompt}", prompt)
        assert argv == ["claude", "-p", prompt]

    def test_prompt_appended_as_last_argument_without_placeholder(self, script):
        prompt = "author a recipe"
        argv = script.build_author_command("claude -p", prompt)
        assert argv == ["claude", "-p", prompt]

    def test_placeholder_must_be_standalone_token(self, script):
        # "{prompt}" embedded in a larger token is NOT substituted (it would
        # be unshellable); the prompt is appended instead.
        argv = script.build_author_command("echo x{prompt}y", "p")
        assert argv == ["echo", "x{prompt}y", "p"]

    def test_multiline_prompt_never_resplit(self, script):
        prompt = "a\nb c\nd"
        argv = script.build_author_command("claude -p {prompt}", prompt)
        assert len(argv) == 3
        assert argv[2] == prompt


class TestMainGate:
    def test_no_template_disables_foundry_exit_zero(self, script, capsys):
        rc = script.main(config={})
        assert rc == 0
        err = capsys.readouterr().err
        assert (
            "foundry: no claude_author_cmd_template configured — "
            "Foundry disabled (ADR-0020)" in err
        )

    def test_blank_template_disables_foundry_exit_zero(self, script, capsys):
        rc = script.main(
            config={"foundry": {"claude_author_cmd_template": "   "}}
        )
        assert rc == 0
        assert "Foundry disabled (ADR-0020)" in capsys.readouterr().err

    def test_disabled_gate_does_no_work(self, script, monkeypatch):
        # Disabled means disabled: no pattern loading, no LLM, no writes.
        def _boom(*args, **kwargs):
            raise AssertionError("disabled Foundry must not do any work")

        monkeypatch.setattr(script, "load_seeded_patterns", _boom)
        monkeypatch.setattr(script, "make_claude_callback", _boom)
        assert script.main(config={}) == 0


class TestGovernanceConfigPassthrough:
    def test_main_passes_loaded_config_to_pipeline(self, script, monkeypatch):
        # An empty config at the pipeline call silently resolves every
        # governance gate to its permissive default — learning.mode=paused
        # or a gate_closed policy would not hold. The loaded config must
        # flow through.
        paused_cfg = {"learning": {"mode": "paused"}}

        captured = {}

        class _Summary:
            proposed = written = rejected = held_paused = auto_promoted = 0
            rejected_topics = []

        def _fake_run_foundry(**kwargs):
            captured.update(kwargs)
            summary = _Summary()
            summary.written = 1  # productive run -> exit 0
            return summary

        monkeypatch.setattr(script, "load_seeded_patterns", lambda: [])
        monkeypatch.setattr(script, "load_bundles", lambda: [])
        monkeypatch.setattr(script, "resolve_author_cmd_template", lambda c: "cmd {prompt}")
        monkeypatch.setattr(script, "make_claude_callback", lambda t: None)
        monkeypatch.setattr(script, "run_foundry", _fake_run_foundry)

        rc = script.main(config=paused_cfg)
        assert rc == 0
        assert captured["config"] == paused_cfg
