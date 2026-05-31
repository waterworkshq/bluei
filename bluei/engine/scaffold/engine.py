"""scaffold.engine — ScaffoldEngine ties inspector + resolver + template together."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .framework_detect import (
    detect_install_commands,
    detect_test_commands,
    detect_test_framework,
)
from .inspectors.python import inspect_python_module
from .models import ScaffoldResult
from .path_resolver import TestPathResolver
from .templates import render_quickstart_section, render_rollback_section, render_test_file


class ScaffoldEngine:
    def __init__(self) -> None:
        self._resolver = TestPathResolver()

    def generate_test_file(
        self,
        test_path: Path,
        worktree: Path,
        language: str = "python",
    ) -> ScaffoldResult:
        source_path = self._resolver.test_to_source(str(test_path), worktree)
        if source_path is None:
            source_name = test_path.name
            if source_name.startswith("test_") and source_name.endswith(".py"):
                source_name = source_name[5:]
            source_path = worktree / "src" / source_name

        if not source_path.exists():
            return ScaffoldResult(
                success=False,
                error=f"source module not found: {source_path}",
            )

        if language == "python":
            return self._generate_python_test(test_path, source_path, worktree)

        return ScaffoldResult(
            success=False,
            error=f"unsupported language for test scaffolding: {language}",
        )

    def _generate_python_test(
        self,
        test_path: Path,
        source_path: Path,
        worktree: Path,
    ) -> ScaffoldResult:
        try:
            exports = inspect_python_module(source_path, worktree)
        except SyntaxError as exc:
            return ScaffoldResult(success=False, error=f"syntax error in source: {exc}")
        except Exception as exc:
            return ScaffoldResult(success=False, error=f"inspection failed: {exc}")

        if not exports.functions and not exports.classes:
            return ScaffoldResult(
                success=False,
                error=f"no public exports in {source_path}",
            )

        framework = detect_test_framework(worktree, "python")
        content = render_test_file(exports, framework)

        try:
            compile(content, str(test_path), "exec")
        except SyntaxError as exc:
            return ScaffoldResult(success=False, error=f"generated code has syntax error: {exc}")

        if test_path.exists():
            return ScaffoldResult(
                success=False,
                error=f"test file already exists: {test_path}",
            )

        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(content, encoding="utf-8")

        return ScaffoldResult(
            success=True,
            output_path=test_path,
            content=content,
        )

    def generate_doc_section(
        self,
        doc_path: Path,
        section_type: str,
        worktree: Optional[Path] = None,
        insert_before: Optional[str] = None,
        insert_after: Optional[str] = None,
    ) -> ScaffoldResult:
        if not doc_path.exists():
            return ScaffoldResult(success=False, error=f"doc file not found: {doc_path}")

        existing = doc_path.read_text(encoding="utf-8")
        heading = f"## {section_type.replace('_', ' ').title()}"

        if heading in existing:
            return ScaffoldResult(success=False, error=f"section already exists: {heading}")

        wt = worktree or doc_path.parent

        if section_type == "rollback":
            verify = detect_test_commands(wt) if wt else "pytest -q"
            content = render_rollback_section(verify_commands=verify)
        elif section_type == "quickstart":
            install = detect_install_commands(wt) if wt else "pip install pytest"
            tests = detect_test_commands(wt) if wt else "pytest -q"
            content = render_quickstart_section(install_commands=install, test_commands=tests)
        else:
            return ScaffoldResult(success=False, error=f"unknown section type: {section_type}")

        updated = _insert_section(existing, content, insert_before=insert_before, insert_after=insert_after)

        doc_path.write_text(updated, encoding="utf-8")
        return ScaffoldResult(success=True, output_path=doc_path, content=content)


def _insert_section(
    doc: str,
    section: str,
    insert_before: Optional[str] = None,
    insert_after: Optional[str] = None,
) -> str:
    if insert_before:
        idx = doc.find(insert_before)
        if idx >= 0:
            return doc[:idx] + section + "\n" + doc[idx:]

    if insert_after:
        idx = doc.find(insert_after)
        if idx >= 0:
            end = idx + len(insert_after)
            after = doc[end:]
            if after and not after.startswith("\n"):
                after = "\n" + after
            return doc[:end] + after + "\n" + section + "\n"

    return doc.rstrip() + "\n\n" + section + "\n"
