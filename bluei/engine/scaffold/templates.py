"""scaffold.templates — String template rendering for test files and doc sections."""

from __future__ import annotations

from typing import List

from .models import ClassExport, FunctionExport, ModuleExports


def render_test_file(exports: ModuleExports, framework: str = "pytest") -> str:
    public_names = []
    for func in exports.functions:
        public_names.append(func.name)
    for cls in exports.classes:
        public_names.append(cls.name)

    if not public_names:
        return '"""Auto-generated test file — no public exports found."""\n'

    import_names = ", ".join(public_names)
    import_line = f"from {exports.module_name} import {import_names}"

    parts = [
        f'"""Tests for {exports.module_name}."""',
        "import pytest",
        import_line,
        "",
    ]

    for func in exports.functions:
        parts.append(_render_function_test(func))

    for cls in exports.classes:
        parts.append(_render_class_test(cls))

    return "\n".join(parts)


def _render_function_test(func: FunctionExport) -> str:
    lines = [
        "",
        f"def test_{func.name}_basic():",
        f"    # TODO: implement test for {func.name}",
        "    pass",
        "",
    ]
    return "\n".join(lines)


def _render_class_test(cls: ClassExport) -> str:
    methods = [m for m in cls.methods if m.name != "__init__"]
    if not methods:
        return f"""

class Test{cls.name}:
    pass
"""

    lines = ["", f"class Test{cls.name}:"]
    for method in methods:
        lines.append(f"    def test_{method.name}_basic(self):")
        lines.append(f"        # TODO: implement test for {cls.name}.{method.name}")
        lines.append("        pass")
        lines.append("")
    return "\n".join(lines)


def render_rollback_section(verify_commands: str = "pytest -q") -> str:
    return f"""## Rollback

To revert a problematic deployment:

1. Identify the bad commit:
   `git log --oneline -10`
2. Revert:
   `git revert <sha>`
3. Verify checks pass:
   `{verify_commands}`
4. If checks recover, push revert and annotate the incident with root cause.
"""


def render_quickstart_section(install_commands: str = "pip install pytest", test_commands: str = "pytest -q") -> str:
    return f"""## Quick start

```bash
{install_commands}
{test_commands}
```
"""
