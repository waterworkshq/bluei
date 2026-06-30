#!/usr/bin/env python3
"""Tests for the build-time plugin-skeleton generator (alpha.5 Pillar 3).

Covers T3.1-T3.3: ``generate_plugin_pack`` writes the 3 expected files, the
generated pack loads via the UNCHANGED ``PluginLoader``, ``discover()`` finds
planted violations for REGEX + STRUCTURAL patterns, and the ACTIVE-only /
COMPOSITE / non-Python-STRUCTURAL refusals hold. The committed worked example
(``plugins/graduated-eslint/``) loads + detects ``console.*`` calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluei.app.emergent_rules import (
    DetectionPattern,
    DetectionType,
    EmergentRule,
    EmergentRuleStatus,
    extract_detection_ast,
)
from bluei.app.plugins import PluginLoader
from bluei.tools.graduator import generate_plugin_pack

PLUGINS_DIR = Path(__file__).resolve().parents[1] / "plugins"


# ---------------------------------------------------------------------------
# Synthetic fixture rule builders (no real graduated rules exist pre-stable)
# ---------------------------------------------------------------------------


def _regex_rule(
    *,
    rule_id: str = "er-fixture-regex",
    status: EmergentRuleStatus = EmergentRuleStatus.ACTIVE,
) -> EmergentRule:
    return EmergentRule(
        rule_id=rule_id,
        header=f"Graduated Rule {rule_id}",
        detection_pattern=DetectionPattern(
            detection_type=DetectionType.REGEX_PATTERN,
            search_pattern=r"console\.(log|debug|info|warn|error)\b",
            file_glob="**/*.js",
        ),
        language="javascript",
        category="lint",
        status=status,
        confidence=0.85,
        observation_count=14,
    )


def _text_rule(
    *,
    rule_id: str = "er-fixture-text",
    status: EmergentRuleStatus = EmergentRuleStatus.ACTIVE,
) -> EmergentRule:
    return EmergentRule(
        rule_id=rule_id,
        header=f"Graduated Rule {rule_id}",
        detection_pattern=DetectionPattern(
            detection_type=DetectionType.TEXT_PATTERN,
            search_pattern="TODO",
            file_glob="**/*.py",
        ),
        language="python",
        category="todo/debt",
        status=status,
        confidence=0.7,
        observation_count=3,
    )


def _structural_rule(
    *,
    rule_id: str = "er-fixture-struct",
    status: EmergentRuleStatus = EmergentRuleStatus.ACTIVE,
    language: str = "python",
    ast_pattern: str | None = None,
) -> EmergentRule:
    if ast_pattern is None:
        ast_pattern = extract_detection_ast('raise ValueError("bad input")', "python")
    return EmergentRule(
        rule_id=rule_id,
        header=f"Graduated Rule {rule_id}",
        detection_pattern=DetectionPattern(
            detection_type=DetectionType.STRUCTURAL,
            search_pattern="",
            file_glob="**/*.py",
            ast_pattern=ast_pattern,
        ),
        language=language,
        category="bug",
        status=status,
        confidence=0.9,
        observation_count=5,
    )


# ---------------------------------------------------------------------------
# T3.1 — generator writes 3 files for an ACTIVE rule
# ---------------------------------------------------------------------------


def test_generates_three_files_regex(tmp_path):
    rule = _regex_rule()
    res = generate_plugin_pack(rule, tmp_path)

    assert res["generated"] is True
    pack_dir = Path(res["pack_dir"])
    assert pack_dir.name == "graduated-er-fixture-regex"
    assert pack_dir.is_dir()
    assert (pack_dir / "plugin.py").is_file()
    assert (pack_dir / "plugin.yaml").is_file()
    test_files = list(pack_dir.glob("test_*.py"))
    assert len(test_files) == 1
    assert res["written"] == ["plugin.py", "plugin.yaml", test_files[0].name]


def test_generates_three_files_structural(tmp_path):
    rule = _structural_rule()
    res = generate_plugin_pack(rule, tmp_path)
    assert res["generated"] is True
    pack_dir = Path(res["pack_dir"])
    assert (pack_dir / "plugin.py").is_file()
    assert (pack_dir / "plugin.yaml").is_file()
    assert len(list(pack_dir.glob("test_*.py"))) == 1


def test_generates_three_files_text(tmp_path):
    rule = _text_rule()
    res = generate_plugin_pack(rule, tmp_path)
    assert res["generated"] is True
    pack_dir = Path(res["pack_dir"])
    assert (pack_dir / "plugin.py").is_file()
    assert (pack_dir / "plugin.yaml").is_file()
    assert len(list(pack_dir.glob("test_*.py"))) == 1


def test_default_pack_id_convention(tmp_path):
    res = generate_plugin_pack(_regex_rule(rule_id="er-xyz"), tmp_path)
    assert res["pack_id"] == "graduated-er-xyz"


def test_custom_pack_id(tmp_path):
    res = generate_plugin_pack(_regex_rule(), tmp_path, pack_id="graduated-custom")
    assert res["pack_id"] == "graduated-custom"
    assert Path(res["pack_dir"]).name == "graduated-custom"


# ---------------------------------------------------------------------------
# T3.1 — generated plugin.yaml conforms to the manifest schema
# ---------------------------------------------------------------------------


def test_generated_manifest_has_no_tool(tmp_path):
    import yaml

    res = generate_plugin_pack(_regex_rule(), tmp_path)
    manifest = yaml.safe_load((Path(res["pack_dir"]) / "plugin.yaml").read_text())

    # 7 top-level keys
    for key in (
        "id",
        "name",
        "version",
        "author",
        "languages",
        "requires_container",
        "discovery",
    ):
        assert key in manifest, f"missing manifest key: {key}"

    # Self-contained — no tool/tool_args
    assert "tool" not in manifest["discovery"]
    assert "tool_args" not in manifest["discovery"]

    # Exactly one rule entry derived from the graduated rule
    rules = manifest["discovery"]["rules"]
    assert len(rules) == 1
    assert rules[0]["id"] == "er-fixture-regex"
    assert rules[0]["promoted_from"] == "emergent"
    assert rules[0]["autofix"] is False
    assert rules[0]["evidence_count"] == 14

    assert manifest["detection"]["files"] == ["**/*.js"]


# ---------------------------------------------------------------------------
# AC-P3-2 + AC-P3-4 — generated discover() runs; PluginLoader loads it
# ---------------------------------------------------------------------------


def test_generated_regex_pack_loads_and_detects(tmp_path):
    rule = _regex_rule()
    res = generate_plugin_pack(rule, tmp_path)
    pack_id = res["pack_id"]

    loader = PluginLoader(tmp_path)
    plugin = loader.load(pack_id)
    assert plugin is not None
    assert plugin.id == pack_id
    assert plugin.rules == ["er-fixture-regex"]
    assert "javascript" in plugin.languages

    # Plant a fixture repo with violations
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.js").write_text(
        'console.log("hi")\nconst x = 1\nconsole.error("boom")\n'
    )
    (repo / "readme.md").write_text("console.log is fine in docs\n")

    assert plugin.detect(repo) is True
    findings = plugin.discover(repo, {})
    assert len(findings) == 2
    assert all(f.rule == "er-fixture-regex" for f in findings)
    assert all(f.path == "app.js" for f in findings)
    lines = sorted(f.line for f in findings)
    assert lines == [1, 3]
    assert all(f.confidence == 0.85 for f in findings)
    assert all(f.category == "lint" for f in findings)


def test_generated_text_pack_detects_substring(tmp_path):
    rule = _text_rule()
    res = generate_plugin_pack(rule, tmp_path)
    pack_id = res["pack_id"]

    loader = PluginLoader(tmp_path)
    plugin = loader.load(pack_id)
    assert plugin is not None

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("x = 1\n# TODO fix this\ny = 2\n")

    findings = plugin.discover(repo, {})
    assert len(findings) == 1
    assert findings[0].line == 2
    assert "TODO" in findings[0].snippet


def test_generated_structural_pack_detects_shape(tmp_path):
    rule = _structural_rule()
    res = generate_plugin_pack(rule, tmp_path)
    pack_id = res["pack_id"]

    loader = PluginLoader(tmp_path)
    plugin = loader.load(pack_id)
    assert plugin is not None

    repo = tmp_path / "repo"
    repo.mkdir()
    # `raise RuntimeError(...)` is the same shape as `raise ValueError(...)`
    # (rename-invariant) → exact structural-hash match.
    (repo / "svc.py").write_text('def f():\n    raise RuntimeError("oops")\n')

    findings = plugin.discover(repo, {})
    assert len(findings) == 1
    assert findings[0].line == 2
    assert "raise" in findings[0].snippet
    assert findings[0].category == "bug"


def test_generated_pack_in_detector_catalog(tmp_path):
    res = generate_plugin_pack(_regex_rule(), tmp_path)
    loader = PluginLoader(tmp_path)
    loader.discover()
    catalog = loader.detector_catalog()
    matching = [e for e in catalog if e["rule"] == "er-fixture-regex"]
    assert len(matching) == 1
    assert matching[0]["category"] == "lint"
    assert matching[0]["autofix"] is False


# ---------------------------------------------------------------------------
# AC-P3-6 — ACTIVE-only gate; COMPOSITE refused; non-Python STRUCTURAL refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        EmergentRuleStatus.PROPOSED,
        EmergentRuleStatus.CANDIDATE,
        EmergentRuleStatus.TENTATIVE,
    ],
)
def test_refuses_non_active(tmp_path, status):
    res = generate_plugin_pack(_regex_rule(status=status), tmp_path)
    assert res["generated"] is False
    assert "ACTIVE" in res["reason"] or "active" in res["reason"]


def test_refuses_composite(tmp_path):
    rule = EmergentRule(
        rule_id="er-composite",
        header="x",
        detection_pattern=DetectionPattern(
            detection_type=DetectionType.COMPOSITE,
            search_pattern="x",
            file_glob="**/*",
            composite_signals=["a", "b"],
        ),
        language="python",
        category="lint",
        status=EmergentRuleStatus.ACTIVE,
    )
    res = generate_plugin_pack(rule, tmp_path)
    assert res["generated"] is False
    assert "COMPOSITE" in res["reason"]


def test_refuses_non_python_structural(tmp_path):
    rule = _structural_rule(language="javascript")
    res = generate_plugin_pack(rule, tmp_path)
    assert res["generated"] is False
    assert "STRUCTURAL" in res["reason"]
    assert "python-only" in res["reason"]


def test_refusal_writes_nothing(tmp_path):
    res = generate_plugin_pack(
        _regex_rule(status=EmergentRuleStatus.TENTATIVE), tmp_path
    )
    assert res["generated"] is False
    assert not list(tmp_path.iterdir())


# ---------------------------------------------------------------------------
# AC-P3-5 — committed worked example loads + detects
# ---------------------------------------------------------------------------


def test_worked_example_loads():
    loader = PluginLoader(PLUGINS_DIR)
    plugin = loader.load("graduated-eslint")
    assert plugin is not None
    assert plugin.id == "graduated-eslint"
    assert "javascript" in plugin.languages
    assert plugin.rules == ["er-eslint-no-console"]


def test_worked_example_in_detector_catalog():
    loader = PluginLoader(PLUGINS_DIR)
    loader.discover()
    catalog = loader.detector_catalog()
    assert any(e["rule"] == "er-eslint-no-console" for e in catalog)


def test_worked_example_detects_console_calls(tmp_path):
    loader = PluginLoader(PLUGINS_DIR)
    plugin = loader.load("graduated-eslint")

    repo = tmp_path / "js-project"
    repo.mkdir()
    (repo / "index.js").write_text(
        'console.log("boot")\nfunction go() {\n  console.warn("slow")\n}\n'
    )

    findings = plugin.discover(repo, {})
    assert len(findings) == 2
    lines = sorted(f.line for f in findings)
    assert lines == [1, 3]
    assert all(f.rule == "er-eslint-no-console" for f in findings)


# ---------------------------------------------------------------------------
# PluginLoader.load + DiscoveryPlugin contract (unchanged — read-only verify)
# ---------------------------------------------------------------------------


def test_generated_plugin_zero_arg_constructible(tmp_path):
    import importlib.util

    res = generate_plugin_pack(_regex_rule(), tmp_path)
    plugin_file = Path(res["pack_dir"]) / "plugin.py"
    spec = importlib.util.spec_from_file_location("probe", plugin_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # PluginLoader.load does getattr(module, "Plugin") + Plugin() — zero args.
    instance = module.Plugin()
    assert instance.id == res["pack_id"]
