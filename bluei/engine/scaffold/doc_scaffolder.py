"""scaffold.doc_scaffolder — Insert generated sections into markdown docs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .framework_detect import detect_install_commands, detect_test_commands
from .templates import render_quickstart_section, render_rollback_section


def insert_section(
    doc_path: Path,
    heading: str,
    content: str,
    after_heading: Optional[str] = None,
    before_heading: Optional[str] = None,
) -> bool:
    if not doc_path.exists():
        return False

    text = doc_path.read_text(encoding="utf-8")

    normalized = heading.strip()
    if not normalized.startswith("#"):
        normalized = f"## {normalized}"

    if normalized in text:
        return False

    if before_heading:
        idx = text.find(before_heading)
        if idx >= 0:
            text = text[:idx] + content + "\n" + text[idx:]
            doc_path.write_text(text, encoding="utf-8")
            return True

    if after_heading:
        idx = text.find(after_heading)
        if idx >= 0:
            end_of_section = _find_next_heading(text, idx + len(after_heading))
            text = text[:end_of_section] + content + "\n" + text[end_of_section:]
            doc_path.write_text(text, encoding="utf-8")
            return True

    text = text.rstrip() + "\n\n" + content + "\n"
    doc_path.write_text(text, encoding="utf-8")
    return True


def generate_rollback_content(worktree: Path) -> str:
    verify = detect_test_commands(worktree)
    return render_rollback_section(verify_commands=verify)


def generate_quickstart_content(worktree: Path) -> str:
    install = detect_install_commands(worktree)
    tests = detect_test_commands(worktree)
    return render_quickstart_section(install_commands=install, test_commands=tests)


def _find_next_heading(text: str, start: int) -> int:
    pattern = re.compile(r"\n(?=#{1,6}\s)", re.MULTILINE)
    match = pattern.search(text, start)
    if match:
        return match.start()
    return len(text)
