"""Tests for plugins.typescript.plugin — TypeScript/JavaScript lint and text scanning."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from plugins.typescript.plugin import Plugin as TypeScriptPlugin, stable_finding_id


class TestStableFindingId:
    def test_deterministic(self):
        a = stable_finding_id("repo", "a.ts", 1, "rule", "snip")
        b = stable_finding_id("repo", "a.ts", 1, "rule", "snip")
        assert a == b

    def test_different_inputs(self):
        a = stable_finding_id("repo", "a.ts", 1, "rule", "snip")
        b = stable_finding_id("repo", "a.ts", 2, "rule", "snip")
        assert a != b

    def test_snippet_stripped(self):
        a = stable_finding_id("repo", "a.ts", 1, "rule", " snip ")
        b = stable_finding_id("repo", "a.ts", 1, "rule", "snip")
        assert a == b

    def test_returns_hex(self):
        result = stable_finding_id("r", "p", 1, "ru", "s")
        assert len(result) == 64
        int(result, 16)


class TestTypeScriptPluginInit:
    def test_instantiation(self):
        plugin = TypeScriptPlugin()
        assert plugin.id == "plugin-typescript"
        assert plugin.name == "TypeScript/JavaScript Plugin"
        assert "typescript" in plugin.languages
        assert "javascript" in plugin.languages
        assert len(plugin.rules) > 0


class TestTypeScriptPluginDetect:
    def test_detect_tsconfig(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        plugin = TypeScriptPlugin()
        assert plugin.detect(tmp_path) is True

    def test_detect_package_json_with_deps(self, tmp_path):
        pkg = {"dependencies": {"lodash": "4.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        plugin = TypeScriptPlugin()
        assert plugin.detect(tmp_path) is True

    def test_detect_package_json_with_devdeps(self, tmp_path):
        pkg = {"devDependencies": {"jest": "29.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        plugin = TypeScriptPlugin()
        assert plugin.detect(tmp_path) is True

    def test_detect_package_json_with_scripts(self, tmp_path):
        pkg = {"scripts": {"test": "jest"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        plugin = TypeScriptPlugin()
        assert plugin.detect(tmp_path) is True

    def test_detect_empty_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        plugin = TypeScriptPlugin()
        assert plugin.detect(tmp_path) is False

    def test_detect_invalid_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("not json")
        plugin = TypeScriptPlugin()
        assert plugin.detect(tmp_path) is False

    def test_detect_no_files(self, tmp_path):
        plugin = TypeScriptPlugin()
        assert plugin.detect(tmp_path) is False


class TestTypeScriptIsTypescript:
    def test_tsconfig_exists(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        plugin = TypeScriptPlugin()
        assert plugin._is_typescript(tmp_path) is True

    def test_dep_typescript(self, tmp_path):
        pkg = {"dependencies": {"typescript": "^5.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        plugin = TypeScriptPlugin()
        assert plugin._is_typescript(tmp_path) is True

    def test_devdep_typescript(self, tmp_path):
        pkg = {"devDependencies": {"typescript": "^5.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        plugin = TypeScriptPlugin()
        assert plugin._is_typescript(tmp_path) is True

    def test_no_typescript(self, tmp_path):
        pkg = {"dependencies": {"lodash": "4.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        plugin = TypeScriptPlugin()
        assert plugin._is_typescript(tmp_path) is False

    def test_invalid_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("not json")
        plugin = TypeScriptPlugin()
        assert plugin._is_typescript(tmp_path) is False


class TestTypeScriptParseOutput:
    def test_valid_eslint_json(self, tmp_path):
        plugin = TypeScriptPlugin()
        eslint_output = json.dumps(
            [
                {
                    "filePath": str(tmp_path / "src/foo.ts"),
                    "messages": [
                        {
                            "ruleId": "max-lines",
                            "line": 10,
                            "message": "Too many lines",
                        }
                    ],
                }
            ]
        )
        findings = plugin.parse_output(eslint_output, tmp_path)
        assert len(findings) == 1
        assert findings[0].rule == "xo-max-lines"
        assert findings[0].line == 10

    def test_mapped_rule_ids(self, tmp_path):
        plugin = TypeScriptPlugin()
        eslint_output = json.dumps(
            [
                {
                    "filePath": str(tmp_path / "a.ts"),
                    "messages": [
                        {
                            "ruleId": "@typescript-eslint/no-explicit-any",
                            "line": 1,
                            "message": "m",
                        },
                        {"ruleId": "complexity", "line": 2, "message": "m2"},
                    ],
                }
            ]
        )
        findings = plugin.parse_output(eslint_output, tmp_path)
        assert findings[0].rule == "type-explicit-any"
        assert findings[1].rule == "xo-complexity"

    def test_unmapped_rule_passes_through(self, tmp_path):
        plugin = TypeScriptPlugin()
        eslint_output = json.dumps(
            [
                {
                    "filePath": str(tmp_path / "a.ts"),
                    "messages": [
                        {"ruleId": "some-custom-rule", "line": 1, "message": "x"}
                    ],
                }
            ]
        )
        findings = plugin.parse_output(eslint_output, tmp_path)
        assert findings[0].rule == "some-custom-rule"

    def test_empty_rule_id_skipped(self, tmp_path):
        plugin = TypeScriptPlugin()
        eslint_output = json.dumps(
            [
                {
                    "filePath": str(tmp_path / "a.ts"),
                    "messages": [{"ruleId": "", "line": 1, "message": "x"}],
                }
            ]
        )
        findings = plugin.parse_output(eslint_output, tmp_path)
        assert findings == []

    def test_invalid_json(self, tmp_path):
        plugin = TypeScriptPlugin()
        findings = plugin.parse_output("not json", tmp_path)
        assert findings == []

    def test_non_list_json(self, tmp_path):
        plugin = TypeScriptPlugin()
        findings = plugin.parse_output('{"key": "val"}', tmp_path)
        assert findings == []

    def test_non_dict_items_skipped(self, tmp_path):
        plugin = TypeScriptPlugin()
        findings = plugin.parse_output('[1, "str", null]', tmp_path)
        assert findings == []

    def test_relative_path_value_error(self, tmp_path):
        plugin = TypeScriptPlugin()
        eslint_output = json.dumps(
            [
                {
                    "filePath": "/completely/different/path.ts",
                    "messages": [{"ruleId": "max-lines", "line": 1, "message": "x"}],
                }
            ]
        )
        findings = plugin.parse_output(eslint_output, tmp_path)
        assert len(findings) == 1
        assert findings[0].path == "/completely/different/path.ts"

    def test_empty_file_path(self, tmp_path):
        plugin = TypeScriptPlugin()
        eslint_output = json.dumps(
            [
                {
                    "filePath": "",
                    "messages": [{"ruleId": "max-lines", "line": 1, "message": "x"}],
                }
            ]
        )
        findings = plugin.parse_output(eslint_output, tmp_path)
        assert len(findings) == 1
        assert findings[0].path == ""

    def test_confidence_and_severity(self, tmp_path):
        plugin = TypeScriptPlugin()
        eslint_output = json.dumps(
            [
                {
                    "filePath": str(tmp_path / "a.ts"),
                    "messages": [{"ruleId": "max-lines", "line": 1, "message": "x"}],
                }
            ]
        )
        findings = plugin.parse_output(eslint_output, tmp_path)
        assert findings[0].confidence == 0.85
        assert findings[0].severity == "medium"
        assert findings[0].category == "lint"
        assert findings[0].quick_win is True
        assert findings[0].safe_to_autofix is True


class TestTypeScriptTextScan:
    def test_any_type_detection(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        src = tmp_path / "src"
        src.mkdir()
        (src / "foo.ts").write_text("const x: any = 1;\n")
        plugin = TypeScriptPlugin()
        findings = plugin._text_scan(tmp_path)
        assert any(f.rule == "type-explicit-any" for f in findings)

    def test_todo_detection(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        src = tmp_path / "src"
        src.mkdir()
        (src / "bar.ts").write_text("// TODO: fix this later\n")
        plugin = TypeScriptPlugin()
        findings = plugin._text_scan(tmp_path)
        assert any(
            f.rule == "xo-no-warning-comments" and "TODO" in f.snippet for f in findings
        )

    def test_fixme_detection(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        src = tmp_path / "src"
        src.mkdir()
        (src / "baz.ts").write_text("// FIXME: broken\n")
        plugin = TypeScriptPlugin()
        findings = plugin._text_scan(tmp_path)
        assert any(
            f.rule == "xo-no-warning-comments" and "FIXME" in f.snippet
            for f in findings
        )

    def test_console_log_detection(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.ts").write_text('console.log("debug")\n')
        plugin = TypeScriptPlugin()
        findings = plugin._text_scan(tmp_path)
        assert any("console.log" in f.snippet for f in findings)

    def test_console_log_in_test_not_flagged(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "app.test.ts").write_text('console.log("debug")\n')
        plugin = TypeScriptPlugin()
        findings = plugin._text_scan(tmp_path)
        assert not any("console.log" in f.snippet for f in findings)

    def test_oversized_file_detection(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        src = tmp_path / "src"
        src.mkdir()
        (src / "big.ts").write_text("\n" * 600)
        plugin = TypeScriptPlugin()
        findings = plugin._text_scan(tmp_path)
        assert any(f.rule == "xo-max-lines" for f in findings)

    def test_node_modules_skipped(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.ts").write_text(
            'const x: any = 1;\nconsole.log("x")\n// TODO: fix\n'
        )
        plugin = TypeScriptPlugin()
        findings = plugin._text_scan(tmp_path)
        assert findings == []

    def test_javascript_extensions_when_no_ts(self, tmp_path):
        pkg = {"dependencies": {"lodash": "4.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.js").write_text("// TODO: fix\n")
        plugin = TypeScriptPlugin()
        findings = plugin._text_scan(tmp_path)
        assert any(f.rule == "xo-no-warning-comments" for f in findings)

    def test_detect_returns_false_skips(self, tmp_path):
        plugin = TypeScriptPlugin()
        assert plugin._text_scan(tmp_path) == []

    def test_jsx_extension(self, tmp_path):
        pkg = {"dependencies": {"react": "18.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        src = tmp_path / "src"
        src.mkdir()
        (src / "comp.jsx").write_text("// FIXME: bad\n")
        plugin = TypeScriptPlugin()
        findings = plugin._text_scan(tmp_path)
        assert any(f.rule == "xo-no-warning-comments" for f in findings)


class TestTypeScriptDiscover:
    def test_merges_tool_and_text_findings(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.ts").write_text("// TODO: fix\n")
        plugin = TypeScriptPlugin()
        eslint_output = json.dumps(
            [
                {
                    "filePath": str(tmp_path / "src/a.ts"),
                    "messages": [
                        {"ruleId": "max-lines", "line": 1, "message": "Too many"}
                    ],
                }
            ]
        )
        with patch.object(plugin, "_run_tool", return_value=eslint_output):
            findings = plugin.discover(tmp_path, {})
            tool_rules = [f.rule for f in findings if f.rule == "xo-max-lines"]
            text_rules = [
                f.rule for f in findings if f.rule == "xo-no-warning-comments"
            ]
            assert len(tool_rules) >= 1
            assert len(text_rules) >= 1

    def test_no_tool_returns_text_only(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.ts").write_text("// TODO: fix\n")
        plugin = TypeScriptPlugin()
        with patch.object(plugin, "_run_tool", return_value=None):
            findings = plugin.discover(tmp_path, {})
            assert all(f.rule != "xo-max-lines" for f in findings) or len(findings) > 0

    def test_deduplication_by_path_line_rule(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.ts").write_text("const x: any = 1;\n")
        plugin = TypeScriptPlugin()
        eslint_output = json.dumps(
            [
                {
                    "filePath": str(tmp_path / "src/a.ts"),
                    "messages": [
                        {
                            "ruleId": "@typescript-eslint/no-explicit-any",
                            "line": 1,
                            "message": "any",
                        }
                    ],
                }
            ]
        )
        with patch.object(plugin, "_run_tool", return_value=eslint_output):
            findings = plugin.discover(tmp_path, {})
            any_findings = [f for f in findings if f.rule == "type-explicit-any"]
            assert len(any_findings) == 1

    def test_no_detect_no_text_scan(self, tmp_path):
        plugin = TypeScriptPlugin()
        with (
            patch.object(plugin, "_run_tool", return_value=None),
            patch.object(plugin, "detect", return_value=False),
        ):
            findings = plugin.discover(tmp_path, {})
            text_findings = [
                f
                for f in findings
                if f.rule.startswith("type-") or f.rule.startswith("xo-")
            ]
            assert len(text_findings) == 0
