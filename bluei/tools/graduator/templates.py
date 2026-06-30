"""String templates for the build-time plugin-skeleton generator.

These render a self-contained ``class Plugin(DiscoveryPlugin)`` pack whose
``discover()`` embeds a graduated emergent rule's ``DetectionPattern``. The
generated plugin mirrors the alpha.5 scan logic (``scan_shadow_rules`` /
``_scan_structural`` in :mod:`bluei.app.emergent_rules`) but is **self
contained** — it never imports those helpers; it re-implements the dispatch
inline so the committed pack is loadable by the unchanged ``PluginLoader``
without a runtime dependency on the emergent-rules module.

The generated ``plugin.py`` is a runtime artifact in the plugins tree — its
``from bluei.engine.*`` / ``from bluei.app.plugins`` imports are legal per the
repo layering policy (plugins→engine/app, like the existing packs). It reaches
``bluei/`` via the ``sys.path`` hack copied from ``plugins/test/plugin.py``.
"""

from __future__ import annotations

import json
from typing import Any, Dict


def _python_repr(value: Any) -> str:
    """Return a Python-source literal for a simple value.

    Used to embed rule fields (ids, patterns, the structural blob) directly
    into the generated module — the pack must be self-contained, so these
    values are baked in as literals (not read from the manifest at runtime).
    """
    return repr(value)


def render_plugin_py(
    *,
    pack_id: str,
    rule_id: str,
    rule_header: str,
    language: str,
    category: str,
    confidence: float,
    detection_type: str,
    search_pattern: str,
    file_glob: str,
    ast_blob: str | None,
    evidence_count: int,
) -> str:
    """Render the self-contained ``plugin.py`` module for a graduated rule.

    The rendered module exposes ``class Plugin(DiscoveryPlugin)`` (zero-arg
    constructible, named exactly ``Plugin``) and an embedding ``discover()``
    that dispatches on ``detection_type``:

    - ``text_pattern``  → substring match per line (mirrors scan_shadow_rules)
    - ``regex_pattern`` → compiled ``re.search`` per line (mirrors same)
    - ``structural``    → self-contained AST walk: exact hash + fuzzy fallback
      against ``canonical_snippet`` (mirrors ``_scan_structural`` +
      ``_iter_structural_subtrees`` with the same 200-subtree statement-boundary
      walk), Python only.

    COMPOSITE is never rendered (the generator refuses it upstream).
    """
    return PLUGIN_TEMPLATE.format(
        pack_id_repr=_python_repr(pack_id),
        rule_id_repr=_python_repr(rule_id),
        rule_header_repr=_python_repr(rule_header),
        language_repr=_python_repr(language),
        category_repr=_python_repr(category),
        confidence_repr=_python_repr(float(confidence)),
        detection_type_repr=_python_repr(detection_type),
        search_pattern_repr=_python_repr(search_pattern),
        file_glob_repr=_python_repr(file_glob),
        ast_blob_repr=_python_repr(ast_blob),
        evidence_count_repr=_python_repr(int(evidence_count)),
    )


def render_plugin_yaml(
    *,
    pack_id: str,
    rule_header: str,
    language: str,
    rule_id: str,
    category: str,
    confidence: float,
    evidence_count: int,
    file_glob: str,
) -> str:
    """Render the 7-top-level-key plugin.yaml manifest (no tool/tool_args).

    Mirrors ``plugins/python/plugin.yaml`` but omits ``discovery.tool``/
    ``tool_args`` — the generated pack is self-contained (NOT a
    ``ToolWrapperPlugin``). ``discovery.rules`` carries exactly one entry
    derived from the graduated rule. String values are YAML-double-quoted so
    headers containing colons (e.g. ``Graduated Rule: foo``) stay scalars.
    """
    return (
        f"id: {_yaml_str(pack_id)}\n"
        f"name: {_yaml_str(rule_header)}\n"
        f"version: 0.1.0\n"
        f"author: bluei-graduator\n"
        f"\n"
        f"languages:\n"
        f"  - {_yaml_str(language)}\n"
        f"\n"
        f"requires_container: false\n"
        f"\n"
        f"discovery:\n"
        f"  rules:\n"
        f"    - id: {_yaml_str(rule_id)}\n"
        f"      category: {_yaml_str(category)}\n"
        f"      confidence: {float(confidence)}\n"
        f"      autofix: false\n"
        f"      promoted_from: emergent\n"
        f"      evidence_count: {int(evidence_count)}\n"
        f"\n"
        f"detection:\n"
        f"  files:\n"
        f"    - {_yaml_str(file_glob)}\n"
    )


def _yaml_str(value: str) -> str:
    """Quote a string for YAML so colons / special chars stay scalar."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_test_scaffold(*, pack_id: str, file_glob: str, language: str) -> str:
    """Render a lightweight test that loads the pack via PluginLoader.

    Proves the generated pack loads + detects a planted violation. Uses the
    project ``plugins/`` tree (parent of this file) so the committed test runs
    in the real discovery layout.
    """
    # Derive a sample violating sample line based on detection type is not
    # possible from this template alone (it doesn't know the pattern); the
    # scaffold just asserts the pack loads and lists its rule. Operators add a
    # real fixture line during curation.
    return TEST_TEMPLATE.format(
        pack_id_repr=_python_repr(pack_id),
        file_glob_repr=_python_repr(file_glob),
        language_repr=_python_repr(language),
    )


# ---------------------------------------------------------------------------
# plugin.py template — the self-contained DiscoveryPlugin pack
# ---------------------------------------------------------------------------

PLUGIN_TEMPLATE = '''#!/usr/bin/env python3
"""Auto-generated by ``bluei.tools.graduator`` (build-time only).

Self-contained ``DiscoveryPlugin`` pack graduated from an ACTIVE emergent rule.
The ``discover()`` method embeds the rule's DetectionPattern and emits
``Finding`` instances directly — no tool subprocess, no manifest re-read.

Do not hand-edit; re-generate from the source emergent rule. Curated output
is committed to the plugins tree (alpha.5 Pillar 3).
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

# Reach the ``bluei/`` package from the plugins tree (mirrors
# plugins/test/plugin.py:10-12). The committed pack lives at
# ``plugins/<pack_id>/plugin.py`` — same depth as ``plugins/test/plugin.py`` —
# so ``parent.parent.parent`` is the repo root containing ``bluei/``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from bluei.engine.models import Finding
from bluei.engine.structural_hash import (
    compute_structural_hash,
    fuzzy_structural_match,
    get_fuzzy_threshold,
)
from bluei.app.plugins import DiscoveryPlugin


_PACK_ID = {pack_id_repr}
_RULE_ID = {rule_id_repr}
_RULE_HEADER = {rule_header_repr}
_LANGUAGE = {language_repr}
_CATEGORY = {category_repr}
_CONFIDENCE = {confidence_repr}
_DETECTION_TYPE = {detection_type_repr}
_SEARCH_PATTERN = {search_pattern_repr}
_FILE_GLOB = {file_glob_repr}
_AST_BLOB = {ast_blob_repr}
_EVIDENCE_COUNT = {evidence_count_repr}

_STRUCTURAL_SUBTREE_CAP = 200
_CONTAINER_KINDS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _iter_shadow_files(root: Path, file_glob: str) -> List[Path]:
    globs = [file_glob or "**/*"]
    if file_glob.endswith("/**"):
        globs.append(f"{{file_glob}}/*")
    files: Dict[str, Path] = {{}}
    for glob_pattern in globs:
        for path in root.glob(glob_pattern):
            if path.is_file():
                files[path.as_posix()] = path
    return [files[key] for key in sorted(files)]


def _iter_structural_subtrees(tree: ast.AST) -> List[ast.AST]:
    subtrees: List[ast.AST] = []

    def _walk_body(body: list) -> None:
        for node in body:
            if len(subtrees) >= _STRUCTURAL_SUBTREE_CAP:
                return
            if isinstance(node, _CONTAINER_KINDS):
                _walk_body(list(getattr(node, "body", [])))
                continue
            subtrees.append(node)

    _walk_body(list(getattr(tree, "body", [])))
    return subtrees


class Plugin(DiscoveryPlugin):
    """Self-contained detector graduated from emergent rule ``{{_RULE_ID}}``."""

    @property
    def id(self) -> str:
        return _PACK_ID

    @property
    def name(self) -> str:
        return _RULE_HEADER

    @property
    def languages(self) -> List[str]:
        return [_LANGUAGE]

    @property
    def rules(self) -> List[str]:
        return [_RULE_ID]

    def detect(self, repo_path: Path) -> bool:
        """Applicability: does the detection file glob hit anything?"""
        return any(_iter_shadow_files(Path(repo_path), _FILE_GLOB))

    def discover(self, repo_path: Path, config: Dict[str, Any]) -> List[Finding]:
        """Run the embedded DetectionPattern over the worktree."""
        root = Path(repo_path)
        findings: List[Finding] = []

        if _DETECTION_TYPE == "structural":
            return self._discover_structural(root)

        needle = _SEARCH_PATTERN.strip()
        compiled = None
        if _DETECTION_TYPE == "regex_pattern":
            try:
                compiled = re.compile(needle)
            except re.error:
                return []
        elif _DETECTION_TYPE != "text_pattern":
            return []
        elif not needle:
            return []

        for path in _iter_shadow_files(root, _FILE_GLOB):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            relative_path = path.relative_to(root).as_posix()
            for index, line in enumerate(text.splitlines(), start=1):
                if compiled is not None:
                    if not compiled.search(line):
                        continue
                elif needle not in line:
                    continue
                findings.append(
                    self._make_finding(
                        relative_path, index, line.strip(), root
                    )
                )
        return findings

    def _discover_structural(self, root: Path) -> List[Finding]:
        blob_raw = _AST_BLOB
        if not blob_raw:
            return []
        try:
            blob = json.loads(blob_raw)
        except (ValueError, TypeError):
            return []
        if not isinstance(blob, dict):
            return []
        target_hash = blob.get("hash")
        canonical = blob.get("canonical_snippet") or ""
        if not target_hash:
            return []
        threshold = get_fuzzy_threshold(_RULE_ID)
        findings: List[Finding] = []
        for path in _iter_shadow_files(root, _FILE_GLOB):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            relative_path = path.relative_to(root).as_posix()
            for subtree in _iter_structural_subtrees(tree):
                try:
                    snippet_src = ast.get_source_segment(text, subtree) or ""
                except Exception:
                    snippet_src = ""
                if not snippet_src.strip():
                    continue
                sub_hash = compute_structural_hash(snippet_src, "python")
                is_match = sub_hash == target_hash
                if not is_match and canonical:
                    try:
                        score = fuzzy_structural_match(
                            snippet_src, canonical, "python"
                        )
                    except Exception:
                        score = 0.0
                    is_match = score >= threshold
                if not is_match:
                    continue
                lineno = getattr(subtree, "lineno", 1) or 1
                findings.append(
                    self._make_finding(
                        relative_path, lineno, snippet_src.strip(), root
                    )
                )
        return findings

    @staticmethod
    def _make_finding(
        rel_path: str, line: int, snippet: str, root: Path
    ) -> Finding:
        return Finding(
            finding_id=f"{{_RULE_ID}}-{{rel_path}}:{{line}}",
            repo=str(root),
            path=rel_path,
            line=line,
            rule=_RULE_ID,
            snippet=snippet,
            confidence=_CONFIDENCE,
            quick_win=False,
            safe_to_autofix=False,
            category=_CATEGORY,
        )
'''


TEST_TEMPLATE = '''#!/usr/bin/env python3
"""Test scaffold for the auto-generated pack ``{{pack_id}}``.

Proves the pack loads via ``PluginLoader`` and exposes its graduated rule.
Extend the planted-violation assertions during curation (the generator cannot
guess a real fixture line for every rule shape).
"""

from pathlib import Path

from bluei.app.plugins import PluginLoader


PLUGINS_DIR = Path(__file__).resolve().parents[1]


def test_pack_loads():
    loader = PluginLoader(PLUGINS_DIR)
    plugin = loader.load({pack_id_repr})
    assert plugin is not None
    assert plugin.id == {pack_id_repr}
    assert {language_repr} in plugin.languages

    catalog = loader.detector_catalog()
    rule_ids = [entry["rule"] for entry in catalog]
    assert plugin.rules[0] in rule_ids
'''
