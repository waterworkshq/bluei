"""scaffold.path_resolver — Map between source and test file paths."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


class TestPathResolver:
    def source_to_test(self, source_path: str, worktree: Path) -> Path:
        source = Path(source_path)

        if source.parts[0] == "src" and len(source.parts) > 2:
            pkg_parts = source.parts[1:-1]
            filename = source.parts[-1]
            test_name = f"test_{filename}" if not filename.startswith("test_") else filename
            return worktree / "tests" / test_name

        if source.parts[0] == "src" and len(source.parts) == 2:
            filename = source.parts[1]
            test_name = f"test_{filename}" if not filename.startswith("test_") else filename
            return worktree / "tests" / test_name

        filename = source.parts[-1]
        test_name = f"test_{filename}" if not filename.startswith("test_") else filename
        return worktree / "tests" / test_name

    def test_to_source(self, test_path: str, worktree: Path) -> Optional[Path]:
        test = Path(test_path)
        filename = test.parts[-1] if test.parts else ""

        if filename.startswith("test_") and filename.endswith(".py"):
            source_name = filename[5:]

            candidates = [
                worktree / "src" / source_name,
                worktree / source_name,
            ]

            src_dirs = list((worktree / "src").iterdir()) if (worktree / "src").is_dir() else []
            for d in src_dirs:
                if d.is_dir() and not d.name.startswith("_"):
                    candidates.append(d / source_name)

            for candidate in candidates:
                if candidate.exists():
                    return candidate

        return None

    def infer_module_name(self, source_path: str, worktree: Path) -> str:
        source = Path(source_path)

        if source.parts[0] == "src" and len(source.parts) > 2:
            pkg_name = source.parts[1]
            mod_name = source.parts[-1].replace(".py", "")
            return f"{pkg_name}.{mod_name}"

        if source.parts[0] == "src" and len(source.parts) == 2:
            return source.parts[1].replace(".py", "")

        return source.parts[-1].replace(".py", "") if source.parts else "module"
