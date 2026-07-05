"""Tests for bluei.engine.model_discovery (Phase 0, beta.1).

Covers AC-P2-1..P2-5:
    * AC-P2-1 — ``inject_model_flag`` correctness for claude/opencode/full-path/no-op/unknown.
    * AC-P2-2 — ``BackendModelDiscovery.discover`` (subprocess mocked): mapping
                validation (available→resolved, unavailable→warn+skip, empty-config→[]).
                ``load_model_tiers`` (missing/empty/valid/unknown-tier).
    * AC-P2-3 — source-level scan: no vendor model-id literals ship in
                ``engine/model_discovery.py`` defaults.
    * AC-P2-4 — layering: no ``bluei.engine`` → ``bluei.tools`` import.
    * AC-P2-5 — no real CLI invoked in the test suite (subprocess.run mocked
                everywhere; assertion that the real ``_list_backend_models``
                never escapes the mock boundary).
"""

from __future__ import annotations

import ast
import inspect
import subprocess
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

import bluei.engine.model_discovery as md
from bluei.engine.model_discovery import (
    BackendModelDiscovery,
    ModelDiscovery,
    ResolvedModel,
    _list_backend_models,
    inject_model_flag,
    load_model_tiers,
    resolve_model,
)
from bluei.engine.model_governor import ModelTier
from bluei.tools.benchmark.runner import MockModelDiscovery, default_mock_discovery


# ─── shared helpers ────────────────────────────────────────────────────────


def _resolved(
    tier: ModelTier = ModelTier.TIER_2,
    backend: str = "claude",
    model_id: str = "some-model",
    input_per_1k: float = 0.003,
    output_per_1k: float = 0.012,
) -> ResolvedModel:
    return ResolvedModel(
        tier=tier,
        backend=backend,
        model_id=model_id,
        input_per_1k=input_per_1k,
        output_per_1k=output_per_1k,
    )


# ─── resolve_model (relocation regression) ─────────────────────────────────


class TestResolveModel:
    def test_picks_first_match_by_tier(self):
        discovery = default_mock_discovery()
        m = resolve_model(discovery, ModelTier.TIER_1)
        assert m is not None
        assert m.tier == ModelTier.TIER_1
        assert m.model_id == "mock-tier-1"

    def test_no_match_returns_none(self):
        discovery = MockModelDiscovery({})  # empty
        assert resolve_model(discovery, ModelTier.TIER_2) is None

    def test_accepts_protocol_shape_duck_typed(self):
        # resolve_model is typed against the Protocol, not MockModelDiscovery.
        # A bare duck-typed object satisfying discover(backend) must work.
        class _Duck:
            def discover(self) -> List[ResolvedModel]:
                return [_resolved(ModelTier.TIER_0)]

        out = resolve_model(_Duck(), ModelTier.TIER_0)  # type: ignore[arg-type]
        assert out is not None and out.tier == ModelTier.TIER_0

    def test_protocol_is_a_protocol(self):
        # ModelDiscovery must be typing.Protocol so structural typing applies.
        from typing import Protocol as _P

        assert issubclass(ModelDiscovery, _P) or ModelDiscovery.__mro__[1] is _P


# ─── inject_model_flag (AC-P2-1) ───────────────────────────────────────────


class TestInjectModelFlag:
    def test_claude_template_gets_flag(self):
        out = inject_model_flag('claude --print "hi"', _resolved(model_id="abc"))
        assert out == 'claude --model abc --print "hi"'

    def test_opencode_template_gets_flag(self):
        out = inject_model_flag('opencode run "hi"', _resolved(model_id="xyz"))
        assert out == 'opencode --model xyz run "hi"'

    def test_full_path_claude(self):
        out = inject_model_flag(
            '/usr/local/bin/claude --print "hi"', _resolved(model_id="abc")
        )
        assert out == '/usr/local/bin/claude --model abc --print "hi"'

    def test_full_path_opencode(self):
        out = inject_model_flag('/opt/bin/opencode run "hi"', _resolved(model_id="xyz"))
        assert out == '/opt/bin/opencode --model xyz run "hi"'

    def test_resolved_none_is_identity(self):
        template = 'claude --print "hi"'
        assert inject_model_flag(template, None) == template

    def test_empty_model_id_is_identity(self):
        template = 'claude --print "hi"'
        assert inject_model_flag(template, _resolved(model_id="")) == template

    def test_unknown_backend_warns_and_is_identity(self, caplog):
        template = "python script.py --flag"
        with caplog.at_level("WARNING", logger="bluei.engine.model_discovery"):
            out = inject_model_flag(template, _resolved(model_id="abc"))
        assert out == template
        assert any("unrecognized backend token" in r.message for r in caplog.records)

    def test_leading_whitespace_preserved(self):
        out = inject_model_flag("   claude --print x", _resolved(model_id="m"))
        assert out == "   claude --model m --print x"

    def test_flag_after_first_token_only(self):
        # ensure flag goes right after the binary, not at the end
        out = inject_model_flag(
            'claude --dangerously-skip-permissions --print "q"', _resolved()
        )
        assert (
            out
            == 'claude --model some-model --dangerously-skip-permissions --print "q"'
        )

    def test_empty_template_unknown(self, caplog):
        # empty template → first_token "" → unknown backend → identity + warn
        with caplog.at_level("WARNING", logger="bluei.engine.model_discovery"):
            out = inject_model_flag("", _resolved(model_id="abc"))
        assert out == ""


# ─── load_model_tiers (AC-P2-2) ────────────────────────────────────────────


class TestLoadModelTiers:
    def test_none_path_returns_empty(self):
        assert load_model_tiers(None) == {}

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_model_tiers(tmp_path / "does_not_exist.yaml") == {}

    def test_empty_file_returns_empty(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        assert load_model_tiers(p) == {}

    def test_valid_two_backend_file(self, tmp_path):
        p = tmp_path / "tiers.yaml"
        p.write_text(
            "claude:\n"
            "  tier-0: cheap-id\n"
            "  tier-1: mid-id\n"
            "  tier-2: frontier-id\n"
            "opencode:\n"
            "  tier-0: oc-cheap\n"
        )
        out = load_model_tiers(p)
        assert set(out.keys()) == {"claude", "opencode"}
        assert out["claude"] == {
            ModelTier.TIER_0: "cheap-id",
            ModelTier.TIER_1: "mid-id",
            ModelTier.TIER_2: "frontier-id",
        }
        assert out["opencode"] == {ModelTier.TIER_0: "oc-cheap"}

    def test_unknown_tier_skipped_with_warning(self, tmp_path, caplog):
        p = tmp_path / "bad.yaml"
        p.write_text("claude:\n  tier-0: cheap-id\n  tier-99-bogus: nope\n")
        with caplog.at_level("WARNING", logger="bluei.engine.model_discovery"):
            out = load_model_tiers(p)
        assert out == {"claude": {ModelTier.TIER_0: "cheap-id"}}
        assert any("unknown tier" in r.message for r in caplog.records)

    def test_malformed_yaml_returns_empty(self, tmp_path, caplog):
        p = tmp_path / "broken.yaml"
        p.write_text("claude: [unclosed\n  : ::")
        with caplog.at_level("WARNING", logger="bluei.engine.model_discovery"):
            out = load_model_tiers(p)
        assert out == {}

    def test_single_backend_only(self, tmp_path):
        p = tmp_path / "one.yaml"
        p.write_text("opencode:\n  tier-2: oc-front\n")
        out = load_model_tiers(p)
        assert out == {"opencode": {ModelTier.TIER_2: "oc-front"}}


# ─── BackendModelDiscovery.discover (AC-P2-1, AC-P2-2) ─────────────────────


class TestBackendModelDiscovery:
    def test_empty_tier_config_returns_empty(self, monkeypatch):
        # subprocess must NOT be invoked even when config is empty (short-circuit
        # happens before _list_backend_models since the loop body never runs —
        # but _list_backend_models IS called once. Mock it to assert the shape.)
        calls: List[str] = []
        monkeypatch.setattr(
            md,
            "_list_backend_models",
            lambda backend: (calls.append(backend), {})[1],
        )
        d = BackendModelDiscovery(tier_config={}, backend="claude")
        assert d.discover() == []
        assert calls == ["claude"]  # queried once, no mappings to validate

    def test_available_model_resolved(self, monkeypatch):
        monkeypatch.setattr(
            md,
            "_list_backend_models",
            lambda backend: {
                "frontier-id": {"input_per_1k": 0.015, "output_per_1k": 0.075},
            },
        )
        d = BackendModelDiscovery(
            tier_config={ModelTier.TIER_2: "frontier-id"}, backend="claude"
        )
        out = d.discover()
        assert len(out) == 1
        assert out[0].tier == ModelTier.TIER_2
        assert out[0].model_id == "frontier-id"
        assert out[0].backend == "claude"
        assert out[0].input_per_1k == 0.015
        assert out[0].output_per_1k == 0.075

    def test_unavailable_model_warned_and_skipped(self, monkeypatch, caplog):
        monkeypatch.setattr(md, "_list_backend_models", lambda backend: {})
        d = BackendModelDiscovery(
            tier_config={ModelTier.TIER_0: "ghost-id"}, backend="claude"
        )
        with caplog.at_level("WARNING", logger="bluei.engine.model_discovery"):
            out = d.discover()
        assert out == []
        assert any(
            "unavailable" in r.message and "ghost-id" in r.message
            for r in caplog.records
        )

    def test_partial_availability(self, monkeypatch):
        monkeypatch.setattr(
            md,
            "_list_backend_models",
            lambda backend: {"mid-id": {"input_per_1k": 0.003}},
        )
        d = BackendModelDiscovery(
            tier_config={
                ModelTier.TIER_0: "missing-id",
                ModelTier.TIER_1: "mid-id",
                ModelTier.TIER_2: "also-missing",
            },
            backend="claude",
        )
        out = d.discover()
        assert len(out) == 1
        assert out[0].tier == ModelTier.TIER_1
        # missing rate defaults to 0.0
        assert out[0].input_per_1k == 0.003
        assert out[0].output_per_1k == 0.0

    def test_missing_rate_keys_default_zero(self, monkeypatch):
        monkeypatch.setattr(
            md,
            "_list_backend_models",
            lambda backend: {"id-only": {}},  # no rate keys
        )
        d = BackendModelDiscovery(
            tier_config={ModelTier.TIER_0: "id-only"}, backend="opencode"
        )
        out = d.discover()
        assert out[0].input_per_1k == 0.0
        assert out[0].output_per_1k == 0.0

    def test_empty_config_identity_chain(self, monkeypatch):
        # The load-bearing inert-until-configured gate: empty config → []
        # → resolve_model returns None → inject_model_flag is identity.
        monkeypatch.setattr(md, "_list_backend_models", lambda backend: {})
        d = BackendModelDiscovery(tier_config={}, backend="claude")
        discovered = d.discover()
        assert discovered == []
        assert resolve_model(d, ModelTier.TIER_2) is None
        template = 'claude --print "hi"'
        assert (
            inject_model_flag(template, resolve_model(d, ModelTier.TIER_2)) == template
        )

    def test_production_discovery_yields_model_through_resolve_model(self, monkeypatch):
        # M2 regression (Phase 8 B1): the production discovery class MUST yield a
        # model end-to-end through resolve_model. B1 was resolve_model passing a
        # hardcoded "benchmark" literal that BackendModelDiscovery honored,
        # making discover() always query the wrong backend → always []. This test
        # pins that a real BackendModelDiscovery with a configured backend + a
        # populated catalog resolves to a non-None model with the flag injected.
        monkeypatch.setattr(
            md,
            "_list_backend_models",
            lambda backend: (
                {"claude-sonnet-4": {"input_per_1k": 0.003, "output_per_1k": 0.015}}
                if backend == "claude"
                else {}
            ),
        )
        d = BackendModelDiscovery(
            tier_config={ModelTier.TIER_0: "claude-sonnet-4"}, backend="claude"
        )
        resolved = resolve_model(d, ModelTier.TIER_0)
        assert resolved is not None  # the B1 regression — was always None
        assert resolved.model_id == "claude-sonnet-4"
        assert resolved.tier == ModelTier.TIER_0
        assert resolved.backend == "claude"
        # And the flag injects end-to-end:
        tmpl = inject_model_flag("claude --print {prompt_file}", resolved)
        assert "--model claude-sonnet-4" in tmpl
        # The opencode backend (unsupported) still resolves to None:
        d_opencode = BackendModelDiscovery(
            tier_config={ModelTier.TIER_0: "claude-sonnet-4"}, backend="opencode"
        )
        assert resolve_model(d_opencode, ModelTier.TIER_0) is None


# ─── _list_backend_models robustness (AC-P2-5 boundary) ────────────────────


class TestListBackendModels:
    def test_unknown_backend_returns_empty(self):
        assert _list_backend_models("rust-binary") == {}

    def test_opencode_returns_empty_today(self):
        # Phase 5 research thread — no listing command shipped yet.
        assert _list_backend_models("opencode") == {}

    def test_claude_missing_binary_returns_empty(self, monkeypatch):
        def _raise(*a, **kw):
            raise FileNotFoundError("no claude binary")

        monkeypatch.setattr(md.subprocess, "run", _raise)
        assert _list_backend_models("claude") == {}

    def test_claude_nonzero_exit_returns_empty(self, monkeypatch):
        fake = MagicMock(returncode=2, stdout="", stderr="boom")
        monkeypatch.setattr(md.subprocess, "run", lambda *a, **kw: fake)
        assert _list_backend_models("claude") == {}

    def test_claude_unparseable_output_returns_empty(self, monkeypatch):
        fake = MagicMock(returncode=0, stdout="not json {{{", stderr="")
        monkeypatch.setattr(md.subprocess, "run", lambda *a, **kw: fake)
        assert _list_backend_models("claude") == {}

    def test_claude_list_output_parsed(self, monkeypatch):
        # The documented shape: JSON list of {"id": ..., rates...}.
        import json

        payload = json.dumps(
            [
                {"id": "alpha-model", "input_per_1k": 0.001, "output_per_1k": 0.002},
                {"id": "beta-model", "input_per_1k": 0.01, "output_per_1k": 0.05},
            ]
        )
        fake = MagicMock(returncode=0, stdout=payload, stderr="")
        monkeypatch.setattr(md.subprocess, "run", lambda *a, **kw: fake)
        out = _list_backend_models("claude")
        assert set(out.keys()) == {"alpha-model", "beta-model"}
        assert out["alpha-model"]["input_per_1k"] == 0.001

    def test_claude_dict_output_parsed(self, monkeypatch):
        # Tolerate the dict-of-models shape too.
        import json

        payload = json.dumps({"delta-model": {"input_per_1k": 0.02}})
        fake = MagicMock(returncode=0, stdout=payload, stderr="")
        monkeypatch.setattr(md.subprocess, "run", lambda *a, **kw: fake)
        out = _list_backend_models("claude")
        assert "delta-model" in out

    def test_timeout_returns_empty(self, monkeypatch):
        def _raise(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=15)

        monkeypatch.setattr(md.subprocess, "run", _raise)
        assert _list_backend_models("claude") == {}


# ─── AC-P2-3: no vendor model-id literals ship in defaults ─────────────────


class TestNoShippedModelIds:
    """AC-P2-3 — the engine discovery module ships no vendor model-id literals.

    Carve-out: backend binary names (``"claude"`` / ``"opencode"``) are NOT
    model ids — they're CLI command names. The forbidden patterns are
    model-id-shaped: ``claude-`` (with hyphen), ``gpt-``, ``sonnet``,
    ``haiku``, ``opus``, ``gemini``.
    """

    def test_no_model_id_literals_in_source(self):
        source = Path(md.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = ("claude-", "gpt-", "sonnet", "haiku", "opus", "gemini")
        offenders: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                low = node.value.lower()
                for pat in forbidden:
                    if pat in low:
                        offenders.append(
                            f"line {node.lineno}: {node.value!r} contains {pat!r}"
                        )
        assert not offenders, (
            "engine/model_discovery.py ships vendor model-id literals "
            f"(AC-P2-3 violation): {offenders}"
        )

    def test_backend_names_allowed(self):
        # Sanity: the binary names themselves ARE present (carve-out).
        source = Path(md.__file__).read_text(encoding="utf-8")
        # "claude" and "opencode" appear as command names; assert they're there
        # as bare tokens, NOT as model-id-shaped (hyphenated) substrings.
        assert '"claude"' in source or "'claude'" in source


# ─── AC-P2-4: layering — no engine → tools import ──────────────────────────


class TestLayering:
    def test_no_bluei_tools_import(self):
        """AC-P2-4 (M3 fix) — engine must not import from tools.

        ``enforce_architecture.py`` does NOT check this edge today; this test
        IS the enforcement. ``tools/benchmark`` importing FROM engine is the
        legal direction.
        """
        source = Path(md.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_prefixes = ("bluei.tools", "bluei/app/tools")
        offenders: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_prefixes):
                        offenders.append(f"line {node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith(forbidden_prefixes):
                    offenders.append(
                        f"line {node.lineno}: from {node.module} import ..."
                    )
        assert not offenders, (
            f"engine/model_discovery.py imports from tools (layering violation): {offenders}"
        )


# ─── AC-P2-5: no real subprocess escapes during the suite ──────────────────


class TestNoRealSubprocess:
    """AC-P2-5 — tests must never invoke the real CLI.

    Structural guarantee: ``_list_backend_models`` is the ONLY function that
    touches ``subprocess.run``. Tests that exercise the CLI path mock
    ``_list_backend_models`` (or ``subprocess.run``); tests that exercise the
    pure functions (``load_model_tiers`` / ``inject_model_flag`` /
    ``resolve_model``) must never reach it.
    """

    def test_pure_functions_never_touch_subprocess(self, monkeypatch):
        # If any pure function escapes into subprocess.run, explode.
        def _fail(*a, **kw):
            raise AssertionError(
                f"pure function reached subprocess.run: args={a!r} kwargs={kw!r}"
            )

        monkeypatch.setattr(md.subprocess, "run", _fail)

        # These three must be CLI-free under all inputs.
        assert load_model_tiers(None) == {}
        assert inject_model_flag("claude x", None) == "claude x"
        assert resolve_model(MockModelDiscovery({}), ModelTier.TIER_2) is None

    def test_discover_routes_through_list_backend_models_only(self, monkeypatch):
        # discover() must reach subprocess ONLY via _list_backend_models —
        # mocking that single boundary must fully isolate the suite from the CLI.
        subprocess_calls: List[Any] = []

        def _track(*a, **kw):
            subprocess_calls.append((a, kw))
            raise AssertionError(
                "subprocess.run must not be called directly; "
                "discover routes through _list_backend_models"
            )

        monkeypatch.setattr(md.subprocess, "run", _track)

        # Mock the documented boundary — discover must NOT call subprocess.run.
        monkeypatch.setattr(
            md,
            "_list_backend_models",
            lambda backend: {"some-id": {"input_per_1k": 0.001}},
        )
        d = BackendModelDiscovery(
            tier_config={ModelTier.TIER_2: "some-id"}, backend="claude"
        )
        out = d.discover()
        assert len(out) == 1
        assert subprocess_calls == []  # subprocess.run never reached

    def test_list_backend_models_is_the_only_subprocess_caller(self):
        # AST-level: the only reference to subprocess.run in the module lives
        # inside _list_backend_models. Documents the isolation boundary.
        source = Path(md.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        run_refs: List[str] = []  # enclosing function names

        def _scan(node, fn_name):
            for child in ast.walk(node):
                if isinstance(child, ast.Attribute) and child.attr == "run":
                    if (
                        isinstance(child.value, ast.Name)
                        and child.value.id == "subprocess"
                    ):
                        run_refs.append(fn_name)

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _scan(node, node.name)

        assert run_refs, "expected at least one subprocess.run reference"
        for fn_name in run_refs:
            assert fn_name == "_list_backend_models", (
                f"subprocess.run referenced outside _list_backend_models: {fn_name}"
            )
