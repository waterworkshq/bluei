"""Load repo taste entries for LLM prompt injection (ADR-0017 principle).

Taste entries are YAML files under bluei/engine/repo_taste/. Each file
declares the ``frameworks`` and ``language`` it applies to, plus an ordered
list of directive strings. The loader matches by framework + language
(with optional ``rule_family`` exception scoping) and returns a formatted
string for prompt injection.

Taste is prompt-context, NOT a governed asset — it does not flow through
the Operator Control Plane or ``bluei learn`` namespace. Mirrors the
authoritative-guidelines loader (guideline_loader.py).
"""

import logging
from pathlib import Path
from typing import Optional

import yaml

_logger = logging.getLogger(__name__)

_PRODUCT_TASTE_DIR = Path(__file__).parent / "repo_taste"


def load_repo_taste(
    framework: Optional[str],
    language: str,
    rule_family: Optional[str] = None,
    taste_dir: Optional[Path] = None,
) -> str:
    """Load matching repo taste entries as a formatted prompt string.

    Matches taste YAML files whose ``frameworks`` includes ``framework`` OR
    a catch-all (``"*"``, ``"<language>"``), AND whose ``language`` matches
    (or is ``"*"``). When ``rule_family`` is given, an entry is ALSO
    eligible if its ``rule_families`` includes ``rule_family`` (exception
    entries). Returns the concatenated ``### {topic}\\n- {directive}``
    blocks, empty string if no match or the directory is absent/malformed.
    """
    directory = taste_dir or _PRODUCT_TASTE_DIR
    if not directory.is_dir():
        return ""

    matching_lines = []

    for yaml_path in sorted(directory.glob("*.yaml")):
        try:
            with yaml_path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as exc:
            _logger.debug("Skipping malformed taste %s: %s", yaml_path, exc)
            continue
        if not isinstance(data, dict):
            continue

        file_lang = data.get("language", "*")
        if file_lang not in ("*", language):
            continue

        fw_list = data.get("frameworks", []) or []
        rf_list = data.get("rule_families", []) or []
        applies = (framework in fw_list) or ("*" in fw_list) or (language in fw_list)
        if rule_family is not None:
            applies = applies or (rule_family in rf_list)
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
