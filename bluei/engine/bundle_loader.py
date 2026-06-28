"""bluei.engine.bundle_loader — Golden Validation Bundle substrate.

Loads product fixtures (shipped YAML under ``bluei/engine/golden_bundles/``)
and repo-state bundles (per-repo YAML under ``state/golden_bundles/``).

Storage model (ADR-0009): hybrid. Product fixtures are immutable and shipped
with the agent; repo-state bundles are mutable and per-repo. On ``id`` conflict,
product fixtures take precedence (repo-state bundle is skipped).

Schema (ADR-0016): minimal and forward-compatible. Unknown keys are ignored;
missing optional keys default to ``None`` / empty string.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_logger = logging.getLogger(__name__)

# Directory holding shipped (product) fixtures — sibling of recipes/built-in/.
_PRODUCT_BUNDLES_DIR = Path(__file__).parent / "golden_bundles"

# Sub-directory name within a repo-state root holding per-repo bundles.
_REPO_STATE_SUBDIR = "golden_bundles"


@dataclass
class GoldenBundle:
    """A single Golden Validation Bundle entry.

    Forward-compatible: loaders use :meth:`from_dict` which applies defaults
    for any missing optional field so older bundles remain loadable as the
    schema grows.
    """

    id: str
    asset_class: str
    asset_ref: Optional[str]
    rule: str
    rule_family: str
    language: str
    before: str
    after: str
    detector_before: str
    detector_after: str
    validation_command: str
    source_finding_id: Optional[str]
    extracted_at: str
    imports_touched: List[str] = field(default_factory=list)
    negative_examples: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GoldenBundle":
        """Build a :class:`GoldenBundle` from a raw dict (e.g. ``yaml.safe_load``).

        Uses ``.get()`` with defaults for forward-compatibility — unknown keys
        are ignored and missing optional keys fall back to ``None`` / empty
        strings. Mirrors the pattern used by :meth:`FixPattern.from_dict`.
        """
        return cls(
            id=str(data.get("id", "")),
            asset_class=str(data.get("asset_class", "pattern")),
            asset_ref=data.get("asset_ref"),
            rule=str(data.get("rule", "")),
            rule_family=str(data.get("rule_family", "") or ""),
            language=str(data.get("language", "") or ""),
            before=str(data.get("before", "") or ""),
            after=str(data.get("after", "") or ""),
            detector_before=str(data.get("detector_before", "") or ""),
            detector_after=str(data.get("detector_after", "") or ""),
            validation_command=str(data.get("validation_command", "") or ""),
            source_finding_id=data.get("source_finding_id"),
            extracted_at=str(data.get("extracted_at", "") or ""),
            imports_touched=list(data.get("imports_touched") or []),
            negative_examples=list(data.get("negative_examples") or []),
        )


def _load_dir(directory: Path) -> List[GoldenBundle]:
    """Load every ``*.yaml`` file in *directory* (sorted) as a bundle.

    Returns ``[]`` if the directory does not exist. Malformed individual files
    are skipped with a log warning so a single bad bundle can't poison the lot.
    """
    if not directory.is_dir():
        return []

    bundles: List[GoldenBundle] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            _logger.warning("Skipping malformed bundle %s: %s", path, exc)
            continue
        if not isinstance(data, dict):
            _logger.warning("Skipping non-mapping bundle %s", path)
            continue
        bundles.append(GoldenBundle.from_dict(data))
    return bundles


def load_bundles(repo_state_dir: Optional[Path] = None) -> List[GoldenBundle]:
    """Load product fixtures first, then repo-state bundles.

    Product fixtures live under ``bluei/engine/golden_bundles/`` and are always
    loaded. Repo-state bundles live under
    ``<repo_state_dir>/golden_bundles/*.yaml`` and are loaded only when
    *repo_state_dir* is provided and exists.

    On ``id`` conflict between a product fixture and a repo-state bundle, the
    product fixture wins — the repo-state duplicate is skipped.

    Returns ``[]`` if neither source yields anything.
    """
    product = _load_dir(_PRODUCT_BUNDLES_DIR)

    if repo_state_dir is None:
        return product

    repo_dir = Path(repo_state_dir) / _REPO_STATE_SUBDIR
    repo_bundles = _load_dir(repo_dir)
    if not repo_bundles:
        return product

    seen_ids = {b.id for b in product}
    for bundle in repo_bundles:
        if bundle.id in seen_ids:
            _logger.debug(
                "Skipping repo-state bundle %s — shadowed by product fixture",
                bundle.id,
            )
            continue
        product.append(bundle)
        seen_ids.add(bundle.id)
    return product


def has_bundle_reference(asset_ref: str, bundles: List[GoldenBundle]) -> bool:
    """Return True if any bundle's ``asset_ref`` matches *asset_ref*.

    Used by the promotion gate (Phase 4) to decide whether a finding's pattern
    is anchored by a Golden Bundle.
    """
    return any(b.asset_ref == asset_ref for b in bundles)
