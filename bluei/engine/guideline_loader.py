"""Load authoritative guidelines for LLM prompt injection (ADR-0017).

Guidelines are YAML files under bluei/engine/authoritative_guidelines/.
Each file declares the rule_families and language it applies to, plus an
ordered list of directive strings. The loader matches by rule family +
language and returns a formatted string for Directive Seeding.

Guidelines are prompt-context (ADR-0017), NOT governed assets — they do
not flow through the Operator Control Plane or ``bluei learn`` namespace.
"""

import logging
from pathlib import Path
from typing import Optional

import yaml

_logger = logging.getLogger(__name__)

_PRODUCT_GUIDELINES_DIR = Path(__file__).parent / "authoritative_guidelines"


def load_authoritative_guidelines(
    rule: str,
    language: str,
    guidelines_dir: Optional[Path] = None,
) -> str:
    """Load matching authoritative guidelines as a formatted prompt string.

    Matches guideline files whose rule_families include the derived family
    of ``rule`` (via ``derive_rule_family``) OR a catch-all
    ``"general"``/``"<language>"``/``"*"`` entry, and whose language
    matches (or is ``"*"``).

    Returns empty string if no guidelines match (or the directory is
    absent/malformed). The returned string is injected into the LLM fix
    prompt via ``render_claude_fix_prompt``.
    """
    from bluei.engine.rule_family import derive_rule_family

    directory = guidelines_dir or _PRODUCT_GUIDELINES_DIR
    if not directory.is_dir():
        return ""

    target_family = derive_rule_family(rule)
    matching_lines = []

    for yaml_path in sorted(directory.glob("*.yaml")):
        try:
            with yaml_path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as exc:
            _logger.debug("Skipping malformed guideline %s: %s", yaml_path, exc)
            continue
        if not isinstance(data, dict):
            continue

        file_lang = data.get("language", "*")
        if file_lang not in ("*", language):
            continue

        families = data.get("rule_families", []) or []
        # Match if the file's families include our derived family, or a
        # catch-all ("general", "<language>", "*") applies to all rules of
        # that language.
        applies = (
            target_family in families
            or "general" in families
            or language in families
            or "*" in families
        )
        if not applies:
            continue

        topic = data.get("topic", yaml_path.stem)
        directives = data.get("directives", []) or []
        if directives:
            matching_lines.append(f"### {topic}")
            for d in directives:
                matching_lines.append(f"- {d}")
            matching_lines.append("")

    return "\n".join(matching_lines).strip()
