"""bluei.engine.corpus_manifest — load the synthetic Seed Library corpus.

Builds a unified ``List[CorpusEntry]`` by scanning the three committed seed
sources (Slice 2 of alpha.6 "Economics"):

  * **Golden Bundles**  — ``bluei/engine/golden_bundles/*.yaml`` (``rule_family`` stored)
  * **Seeded Patterns** — ``bluei/engine/seeded_patterns/*.jsonl`` (``rule_family`` stored)
  * **Recipes**         — ``bluei/engine/recipes/**/*.yaml``     (family derived via ``derive_rule_family``)

The manifest is computed at benchmark time — the seed assets ARE the
committed source of truth; the manifest is a unified, deterministic view.
Mirrors ``guideline_loader.py`` / ``repo_taste_loader.py`` layering (reads
committed seed assets only; no runtime state).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

import yaml

from bluei.engine.jsonl import read_jsonl
from bluei.engine.models import Finding
from bluei.engine.rule_family import derive_rule_family

_logger = logging.getLogger(__name__)

_ENGINE_DIR = Path(__file__).parent
_BUNDLES_DIR = _ENGINE_DIR / "golden_bundles"
_SEEDED_PATTERNS_DIR = _ENGINE_DIR / "seeded_patterns"
_RECIPES_DIR = _ENGINE_DIR / "recipes"

# Synthetic repo label for reconstructed Findings (never a real on-disk repo).
_CORPUS_REPO = "benchmark-corpus"


@dataclass
class CorpusEntry:
    """One synthetic Finding from the Seed Library + its resolution context.

    Attributes:
        finding: reconstructed from the asset's ``before`` / violation text.
        rule_family: precomputed (from the bundle's stored field,
            ``FixPattern.rule_family``, or ``derive_rule_family(recipe.rule)``).
        asset_class: ``"pattern"`` | ``"recipe"`` | ``"bundle"`` — the source
            dir the entry came from, NOT the bundle YAML's internal
            ``asset_class`` field.
        asset_id: e.g. ``"gb-ruff-b904"``, ``"seed-ruff-b006"``, recipe id.
        source_path: committed path to the seed asset (relative to repo root).
    """

    finding: Finding
    rule_family: str
    asset_class: str
    asset_id: str
    source_path: str

    @property
    def rule(self) -> str:
        """The Finding's linter rule.

        Lets ``CorpusEntry`` satisfy the recipe protocol (objects with
        ``.rule``) so ``compute_coverage`` can count it without adapter
        objects. A derived property — not a serialized field.
        """
        return self.finding.rule


def _repo_root() -> Path:
    """Repo root derived from this file's location (``bluei/engine/`` → root)."""
    return _ENGINE_DIR.parent.parent


def _rel_source(path: Path) -> str:
    """Path relative to repo root (readable in reports); absolute fallback."""
    try:
        return str(path.relative_to(_repo_root()))
    except ValueError:
        return str(path)


def _stable_finding_id(asset_class: str, asset_id: str) -> str:
    material = f"{_CORPUS_REPO}|{asset_class}|{asset_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _make_finding(rule: str, snippet: str, asset_class: str, asset_id: str) -> Finding:
    """Reconstruct a synthetic Finding for a corpus entry.

    ``finding_id`` is deterministic (sha256 of corpus|class|id) so repeated
    manifest loads produce identical ids (AC-P2-1, AC-P2-5).
    """
    return Finding(
        finding_id=_stable_finding_id(asset_class, asset_id),
        repo=_CORPUS_REPO,
        path=f"<synthetic>/{asset_class}/{asset_id}",
        line=1,
        rule=rule,
        snippet=snippet,
        confidence=1.0,
        quick_win=False,
        safe_to_autofix=False,
    )


def _load_bundles() -> List[CorpusEntry]:
    entries: List[CorpusEntry] = []
    if not _BUNDLES_DIR.is_dir():
        return entries
    for path in sorted(_BUNDLES_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            _logger.debug("corpus: skipped malformed bundle %s: %r", path, exc)
            continue
        if not isinstance(data, dict):
            continue
        rule = str(data.get("rule", "")).strip()
        if not rule:
            continue
        rule_family = str(data.get("rule_family", "") or derive_rule_family(rule))
        asset_id = str(data.get("id") or path.stem)
        before = str(data.get("before", "") or "")
        entries.append(
            CorpusEntry(
                finding=_make_finding(rule, before, "bundle", asset_id),
                rule_family=rule_family,
                asset_class="bundle",
                asset_id=asset_id,
                source_path=_rel_source(path),
            )
        )
    return entries


def _load_seeded_patterns() -> List[CorpusEntry]:
    # Deferred import: pattern_store imports are engine-internal and heavy.
    from bluei.engine.pattern_store import (
        DEACTIVATION_THRESHOLD,
        FixPattern,
    )

    entries: List[CorpusEntry] = []
    if not _SEEDED_PATTERNS_DIR.is_dir():
        return entries
    for path in sorted(_SEEDED_PATTERNS_DIR.glob("*.jsonl")):
        for record in read_jsonl(path):
            if not isinstance(record, dict):
                continue
            try:
                pattern = FixPattern.from_dict(record)
            except Exception as exc:
                _logger.debug("corpus: skipped unparseable pattern %s: %r", path, exc)
                continue
            if pattern.confidence < DEACTIVATION_THRESHOLD:
                continue
            if not pattern.rule:
                continue
            rule_family = pattern.rule_family or derive_rule_family(pattern.rule)
            entries.append(
                CorpusEntry(
                    finding=_make_finding(
                        pattern.rule,
                        pattern.before_snippet,
                        "pattern",
                        pattern.pattern_id,
                    ),
                    rule_family=rule_family,
                    asset_class="pattern",
                    asset_id=pattern.pattern_id,
                    source_path=_rel_source(path),
                )
            )
    return entries


def _load_recipes() -> List[CorpusEntry]:
    from bluei.engine.recipe_schema import load_recipe

    entries: List[CorpusEntry] = []
    if not _RECIPES_DIR.is_dir():
        return entries
    for path in sorted(_RECIPES_DIR.glob("**/*.yaml")):
        try:
            recipe = load_recipe(path)
        except Exception as exc:
            _logger.debug("corpus: skipped unparseable recipe %s: %r", path, exc)
            continue
        if not recipe.rule:
            continue
        rule_family = derive_rule_family(recipe.rule)
        entries.append(
            CorpusEntry(
                finding=_make_finding(recipe.rule, "", "recipe", recipe.id),
                rule_family=rule_family,
                asset_class="recipe",
                asset_id=recipe.id,
                source_path=_rel_source(path),
            )
        )
    return entries


def load_corpus_manifest() -> List[CorpusEntry]:
    """Build the corpus manifest from the three committed seed sources.

    Returns:
        ``List[CorpusEntry]`` sorted by ``asset_id`` for determinism.
        Covers Bundles + seeded Patterns + Recipes. Non-empty when any seed
        source is populated.
    """
    entries: List[CorpusEntry] = (
        _load_bundles() + _load_seeded_patterns() + _load_recipes()
    )
    entries.sort(key=lambda e: e.asset_id)
    return entries
