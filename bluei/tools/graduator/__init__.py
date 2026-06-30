"""Build-time plugin-skeleton generator. NOT imported by runtime code.

Mirrors ``bluei/tools/foundry/`` and ``bluei/tools/seed/`` (ADR-0018 precedent,
ADR-0021 layering): a build-time-only package whose output is committed product
fixtures in ``plugins/graduated-*/``. Runtime loads the committed packs via the
UNCHANGED ``PluginLoader``; it never imports this module.

``generate_plugin_pack(rule, output_dir)`` converts a graduated ACTIVE emergent
rule into a complete self-contained ``DiscoveryPlugin`` pack (``plugin.py`` +
``plugin.yaml`` + test scaffold). The ACTIVE-only gate is deliberately stricter
than ``promote_rule`` (which allows active|tentative) — a graduated pack is a
shipped product detector, so only FP-gate-passed (ACTIVE) rules qualify.

Out of scope here: ``promote_rule`` (the runtime YAML-append path), the
``PluginLoader`` / ``DiscoveryPlugin`` ABC, and the 8 existing plugin packs —
all read-only.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from bluei.app.emergent_rules import (
    DetectionType,
    EmergentRule,
    EmergentRuleStatus,
)
from bluei.tools.graduator import templates

__all__ = ["generate_plugin_pack"]

_logger = logging.getLogger(__name__)


def generate_plugin_pack(
    rule: EmergentRule,
    output_dir: Path,
    *,
    pack_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Emit a self-contained plugin pack for a graduated ACTIVE rule.

    Writes ``<output_dir>/<pack_id>/{plugin.py, plugin.yaml, test_<pack_id>.py}``
    and returns a summary dict. Refuses (returns ``{"generated": False, ...}``)
    unless:

    - ``rule.status == ACTIVE`` (stricter than ``promote_rule`` — only FP-gate
      graduates ship as product detectors), AND
    - ``rule.detection_pattern.detection_type`` is not COMPOSITE (deferred), AND
    - for STRUCTURAL, ``rule.language == "python"`` (Phase 1 C2 constraint).

    Args:
        rule: Graduated emergent rule to graduate into a pack.
        output_dir: Parent directory (typically the ``plugins/`` tree); a
            ``<pack_id>/`` subdir is created within it.
        pack_id: Override the default ``graduated-<rule.rule_id>`` pack id.

    Returns:
        ``{"generated": True, "pack_id", "pack_dir", "written": [...]}`` on
        success, or ``{"generated": False, "reason"}`` on a refusal.
    """
    resolved_pack_id = pack_id or f"graduated-{rule.rule_id}"

    # ACTIVE-only gate (stricter than promote_rule's active|tentative).
    if rule.status != EmergentRuleStatus.ACTIVE:
        reason = (
            f"rule {rule.rule_id} status is {rule.status.value}; "
            f"only ACTIVE rules graduate to a product pack"
        )
        _logger.info("graduator: refusing — %s", reason)
        return {"generated": False, "reason": reason, "pack_id": resolved_pack_id}

    detection_type = rule.detection_pattern.detection_type

    # COMPOSITE is deferred — the generator cannot embed a multi-signal
    # dispatch into a single self-contained discover() yet.
    if detection_type == DetectionType.COMPOSITE:
        reason = f"rule {rule.rule_id} is COMPOSITE; composite packs are deferred"
        _logger.info("graduator: refusing — %s", reason)
        return {"generated": False, "reason": reason, "pack_id": resolved_pack_id}

    # STRUCTURAL is Python-only in Phase 1 (DESIGN 4.6 / C2) — the generated
    # discover() hard-codes ``ast.parse`` (a Python parser).
    if detection_type == DetectionType.STRUCTURAL and rule.language != "python":
        reason = (
            f"rule {rule.rule_id} is STRUCTURAL but language="
            f"{rule.language!r}; structural packs are python-only (Phase 1)"
        )
        _logger.info("graduator: refusing — %s", reason)
        return {"generated": False, "reason": reason, "pack_id": resolved_pack_id}

    pack_dir = Path(output_dir) / resolved_pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)

    rule_header = rule.header or f"Graduated Rule {rule.rule_id}"
    detection_type_value = detection_type.value
    ast_blob = rule.detection_pattern.ast_pattern

    plugin_py = templates.render_plugin_py(
        pack_id=resolved_pack_id,
        rule_id=rule.rule_id,
        rule_header=rule_header,
        language=rule.language,
        category=rule.category or "lint",
        confidence=rule.confidence,
        detection_type=detection_type_value,
        search_pattern=rule.detection_pattern.search_pattern,
        file_glob=rule.detection_pattern.file_glob,
        ast_blob=ast_blob,
        evidence_count=rule.observation_count,
    )
    plugin_yaml = templates.render_plugin_yaml(
        pack_id=resolved_pack_id,
        rule_header=rule_header,
        language=rule.language,
        rule_id=rule.rule_id,
        category=rule.category or "lint",
        confidence=rule.confidence,
        evidence_count=rule.observation_count,
        file_glob=rule.detection_pattern.file_glob,
    )
    test_filename = f"test_{resolved_pack_id.replace('-', '_')}.py"
    test_py = templates.render_test_scaffold(
        pack_id=resolved_pack_id,
        file_glob=rule.detection_pattern.file_glob,
        language=rule.language,
    )

    (pack_dir / "plugin.py").write_text(plugin_py, encoding="utf-8")
    (pack_dir / "plugin.yaml").write_text(plugin_yaml, encoding="utf-8")
    (pack_dir / test_filename).write_text(test_py, encoding="utf-8")

    return {
        "generated": True,
        "pack_id": resolved_pack_id,
        "pack_dir": str(pack_dir),
        "written": ["plugin.py", "plugin.yaml", test_filename],
    }
