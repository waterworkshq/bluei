"""Create-plugin command handler.

Extracted from bin/bluei.py to reduce its surface area.

Scaffolds a new discovery plugin under ``plugins/<plugin-id>/`` plus a
matching pytest skeleton under ``tests/``. The generated files are
intentionally minimal — they compile, load via ``PluginLoader``, and
declare a placeholder rule list that the author fleshes out.
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path
from typing import List, Optional, Tuple

from bin.cmd_utils import parse_option


# Default location of the plugins directory. Can be overridden with
# --plugins-dir (handy for tests and for users who vendor plugins
# outside the source tree).
DEFAULT_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _classify_id(value: str) -> str:
    """Normalise a plugin id so it is filesystem-safe.

    Lower-cases, replaces any run of non-alphanumeric characters with a
    single dash, and trims leading/trailing dashes.
    """
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _derive_plugin_id(tool: str) -> str:
    """Derive a plugin id from a tool name. ``ruff`` -> ``plugin-ruff``."""
    return f"plugin-{_classify_id(tool)}"


def _plugin_id_to_class(plugin_id: str) -> str:
    """Convert ``plugin-my-tool`` -> ``MyToolPlugin``."""
    suffix = plugin_id.split("plugin-", 1)[-1] if "plugin-" in plugin_id else plugin_id
    parts = [p for p in re.split(r"[^a-zA-Z0-9]+", suffix) if p]
    if not parts:
        return "Plugin"
    return "".join(p[:1].upper() + p[1:] for p in parts) + "Plugin"


def _plugin_id_to_display(plugin_id: str) -> str:
    """Convert ``plugin-my-tool`` -> ``My Tool Plugin``."""
    suffix = plugin_id.split("plugin-", 1)[-1] if "plugin-" in plugin_id else plugin_id
    parts = [p for p in re.split(r"[^a-zA-Z0-9]+", suffix) if p]
    if not parts:
        return "Plugin"
    return " ".join(p[:1].upper() + p[1:] for p in parts) + " Plugin"


def render_plugin_yaml(
    *,
    plugin_id: str,
    display_name: str,
    language: str,
    tool: str,
    author: str,
) -> str:
    """Render the ``plugin.yaml`` manifest body."""
    return textwrap.dedent(
        f"""\
        id: {plugin_id}
        name: {display_name}
        version: 0.1.0
        author: {author}

        languages:
          - {language}

        requires_container: false

        discovery:
          tool: {tool}
          tool_args: []  # TODO: fill in (e.g. ["check", "--output-format=json"])
          output_format: json
          rules: []  # TODO: add rules

        detection:
          files: []  # TODO: list files that mark a repo as using {language}

        health_component: lint_quality
        """
    )


def render_health_mapping_yaml(*, plugin_id: str) -> str:
    """Render the ``health_mapping.yaml`` body."""
    return textwrap.dedent(
        f"""\
        rule_to_component: {{}}
        # Example:
        #   my-rule-id: lint_quality

        category_inference:
          {language_token_for_mapping(plugin_id)}: lint_quality
        """
    )


def language_token_for_mapping(plugin_id: str) -> str:
    """Pick a stable category-inference token from the plugin id."""
    suffix = plugin_id.split("plugin-", 1)[-1] if "plugin-" in plugin_id else plugin_id
    return suffix or "plugin"


def render_plugin_py(
    *,
    plugin_id: str,
    display_name: str,
    language: str,
    tool: str,
    class_name: str,
) -> str:
    """Render the ``plugin.py`` skeleton body."""
    return textwrap.dedent(
        f'''\
        #!/usr/bin/env python3
        """{display_name} — wraps ``{tool}`` to discover specks in {language} code."""

        import logging
        from pathlib import Path
        from typing import Any, Dict, List

        import yaml

        from bluei.app.plugins import ToolWrapperPlugin
        from bluei.engine.models import Finding

        _logger = logging.getLogger(__name__)


        class Plugin(ToolWrapperPlugin):
            """{display_name}.

            Subclasses ``ToolWrapperPlugin`` so {tool} gets invoked, its
            output captured, and parsed by :meth:`parse_output`. If {tool}
            is not installed, ``discover()`` returns an empty list and the
            caller falls back to other plugins.
            """

            @property
            def id(self) -> str:
                return "{plugin_id}"

            @property
            def name(self) -> str:
                return "{display_name}"

            @property
            def languages(self) -> List[str]:
                return ["{language}"]

            @property
            def rules(self) -> List[str]:
                # TODO: list the rule IDs you will discover
                return []

            def __init__(self):
                manifest_path = Path(__file__).parent / "plugin.yaml"
                with open(manifest_path) as f:
                    manifest = yaml.safe_load(f)
                super().__init__(manifest)

            def parse_output(self, output: str, repo_path: Path) -> List[Finding]:
                """Parse {tool} output into :class:`Finding` objects.

                TODO: replace this stub with a real parser for {tool}'s
                output format. The default assumes JSON; if {tool}
                produces plain text, override ``discover()`` instead.
                """
                return []

            def detect(self, repo_path: Path) -> bool:
                # TODO: return True if ``repo_path`` uses {language}
                return False
        '''
    )


def render_test_py(*, plugin_id: str, class_name: str) -> str:
    """Render the pytest skeleton for the new plugin."""
    return textwrap.dedent(
        f'''\
        #!/usr/bin/env python3
        """Pytest skeleton for {plugin_id}.

        Replace the placeholders below with real assertions for your
        plugin's behaviour. The plugin module is imported lazily so the
        test suite stays green even if the plugin's binary dependency is
        not installed in CI.
        """

        from pathlib import Path

        import pytest


        def _load_plugin():
            from bluei.app.plugins import PluginLoader

            loader = PluginLoader(Path(__file__).resolve().parents[1] / "plugins")
            loader.discover()
            plugin = loader.load("{plugin_id}")
            if plugin is None:
                pytest.skip("{plugin_id} not available in plugins/ directory")
            return plugin


        def test_id_matches_manifest():
            plugin = _load_plugin()
            assert plugin.id == "{plugin_id}"


        def test_languages_is_nonempty_list():
            plugin = _load_plugin()
            assert isinstance(plugin.languages, list)
            assert plugin.languages, "languages should declare at least one entry"


        def test_rules_is_list():
            plugin = _load_plugin()
            assert isinstance(plugin.rules, list)


        def test_discover_returns_list(tmp_path):
            plugin = _load_plugin()
            findings = plugin.discover(tmp_path, {{}})
            assert isinstance(findings, list)
        '''
    )


def _check_overwrite(plugin_dir: Path) -> Optional[str]:
    """Return an error message if ``plugin_dir`` already exists, else None."""
    if plugin_dir.exists():
        return (
            f"bluei: plugin directory already exists at {plugin_dir}. "
            "Refusing to overwrite. Remove it first or pick a different --plugin-id."
        )
    return None


def _write_files(
    *,
    plugin_dir: Path,
    plugin_id: str,
    display_name: str,
    language: str,
    tool: str,
    author: str,
    class_name: str,
    test_file: Path,
    force: bool = False,
) -> List[Path]:
    """Materialise the four scaffold files. Returns the list written."""
    plugin_dir.mkdir(parents=True, exist_ok=force)
    files = [
        plugin_dir / "plugin.yaml",
        plugin_dir / "plugin.py",
        plugin_dir / "health_mapping.yaml",
    ]
    files[0].write_text(
        render_plugin_yaml(
            plugin_id=plugin_id,
            display_name=display_name,
            language=language,
            tool=tool,
            author=author,
        )
    )
    files[1].write_text(
        render_plugin_py(
            plugin_id=plugin_id,
            display_name=display_name,
            language=language,
            tool=tool,
            class_name=class_name,
        )
    )
    files[2].write_text(render_health_mapping_yaml(plugin_id=plugin_id))

    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(render_test_py(plugin_id=plugin_id, class_name=class_name))
    return [*files, test_file]


def _resolve_paths(
    *,
    plugins_dir: Path,
    plugin_id: str,
) -> Tuple[Path, Path]:
    """Return ``(plugin_dir, test_file)`` for the given plugin id."""
    plugin_dir = plugins_dir / plugin_id
    tests_dir = plugins_dir.parent / "tests"
    test_module = plugin_id.replace("-", "_")
    return plugin_dir, tests_dir / f"test_{test_module}.py"


def _cmd_create_plugin(rest: list[str]) -> int:
    from bin.help_text import HELP_TEXT  # local import keeps cold-start fast

    if not rest or rest[0] in ("-h", "--help"):
        print(HELP_TEXT["create-plugin"])
        return 0

    language = parse_option(rest, "--language")
    tool = parse_option(rest, "--tool")
    plugin_id_override = parse_option(rest, "--plugin-id")
    description = parse_option(rest, "--description")
    author = parse_option(rest, "--author") or "bluei"
    plugins_dir_str = parse_option(rest, "--plugins-dir")
    force = "--force" in rest

    if not language:
        print(
            "bluei: create-plugin requires --language <lang>. "
            "Try 'bluei help create-plugin'.",
            file=sys.stderr,
        )
        return 1
    if not tool:
        print(
            "bluei: create-plugin requires --tool <tool>. "
            "Try 'bluei help create-plugin'.",
            file=sys.stderr,
        )
        return 1

    plugin_id = (
        _classify_id(plugin_id_override)
        if plugin_id_override
        else _derive_plugin_id(tool)
    )
    if not plugin_id.startswith("plugin-"):
        plugin_id = f"plugin-{plugin_id}"

    display_name = _plugin_id_to_display(plugin_id)
    class_name = _plugin_id_to_class(plugin_id)

    plugins_dir = (
        Path(plugins_dir_str).expanduser() if plugins_dir_str else DEFAULT_PLUGINS_DIR
    )
    plugin_dir, test_file = _resolve_paths(plugins_dir=plugins_dir, plugin_id=plugin_id)

    if not force:
        err = _check_overwrite(plugin_dir)
        if err:
            print(err, file=sys.stderr)
            return 1
        if test_file.exists():
            print(
                f"bluei: test file already exists at {test_file}. "
                "Refusing to overwrite. Pass --force to overwrite.",
                file=sys.stderr,
            )
            return 1

    try:
        written = _write_files(
            plugin_dir=plugin_dir,
            plugin_id=plugin_id,
            display_name=display_name,
            language=language,
            tool=tool,
            author=author,
            class_name=class_name,
            test_file=test_file,
            force=force,
        )
    except FileExistsError as exc:
        print(f"bluei: {exc}", file=sys.stderr)
        return 1

    print(f"Created plugin '{plugin_id}' for {language} (tool: {tool})")
    if description:
        print(f"  description: {description}")
    print(f"  location:    {plugin_dir}")
    print()
    print("Files written:")
    for path in written:
        marker = "  (overwritten)" if force and path.exists() else ""
        print(f"  {path}{marker}")
    print()
    print("Next steps:")
    print(f"  1. Edit {plugin_dir / 'plugin.yaml'} to add rule definitions.")
    print(f"  2. Implement parse_output() in {plugin_dir / 'plugin.py'}.")
    print(f"  3. Add real tests to {test_file}.")
    print("  4. Run `bluei languages` to confirm discovery picks it up.")
    return 0
