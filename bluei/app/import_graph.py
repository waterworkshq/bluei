"""Regex-based import graph for dependency-ordered campaign strategy.

Parses Python and TypeScript import statements via regex (no AST dependency),
resolves them to project-relative file paths, and exposes a topological sort
so campaigns can target leaf modules first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ImportEdge:
    source: str
    target: str


@dataclass
class ImportGraph:
    """Directed graph of file-to-file import relationships within a repository."""
    files: List[str] = field(default_factory=list)
    edges: List[ImportEdge] = field(default_factory=list)
    adjacency: Dict[str, List[str]] = field(default_factory=dict)

    def add_edge(self, source: str, target: str) -> None:
        self.edges.append(ImportEdge(source=source, target=target))
        self.adjacency.setdefault(source, []).append(target)

    def topological_sort(self) -> List[str]:
        """Return files in dependency order (leaves first, roots last).

        Files with zero in-degree are processed first.  Cycles are tolerated:
        unreachable nodes are appended at the end.  The result is reversed so
        that dependent files come before their dependencies.

        Returns:
            List of project-relative file paths in topological order.
        """
        in_degree: Dict[str, int] = {f: 0 for f in self.files}
        for edge in self.edges:
            if edge.target in in_degree:
                in_degree[edge.target] = in_degree.get(edge.target, 0) + 1
        queue = sorted([f for f, d in in_degree.items() if d == 0])
        result = []
        while queue:
            node = queue.pop(0)
            result.append(node)
            for neighbor in sorted(self.adjacency.get(node, [])):
                if neighbor in in_degree:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)
        for f in self.files:
            if f not in result:
                result.append(f)
        result.reverse()
        return result


_PYTHON_IMPORT_RE = re.compile(
    r'^\s*(?:from\s+(\S+)\s+import|import\s+([^\n]+))',
    re.MULTILINE,
)

_TS_IMPORT_RE = re.compile(
    r'''(?:import\s+[^;]*?\s+from\s+['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"]\s*\))''',
    re.MULTILINE,
)


def _resolve_python_module(repo_root: Path, module_name: str) -> Optional[str]:
    """Resolve a dotted Python module name to a project-relative file path.

    Args:
        repo_root: Absolute path to the repository root.
            module_name: Dotted import name (e.g. ``bluei.app.models``).

    Returns:
        Project-relative path string, or None if the module is external.
    """
    parts = module_name.split('.')
    for i in range(len(parts), 0, -1):
        candidate = repo_root / Path(*parts[:i])
        py_file = candidate.with_suffix('.py')
        if py_file.exists():
            return str(py_file.relative_to(repo_root))
        init_file = candidate / '__init__.py'
        if init_file.exists():
            return str(init_file.relative_to(repo_root))
    return None


def _resolve_ts_module(repo_root: Path, module_path: str, source_file: str) -> Optional[str]:
    """Resolve a relative TypeScript/JavaScript import to a project-relative path.

    Args:
        repo_root: Absolute path to the repository root.
        module_path: Import specifier (only relative paths starting with ``.``).
        source_file: Project-relative path of the importing file.

    Returns:
        Project-relative path string, or None if unresolvable or external.
    """
    if module_path.startswith('.'):
        source_dir = repo_root / Path(source_file).parent
        resolved = (source_dir / module_path).resolve()
        for ext in ('.ts', '.tsx', '.js', '.jsx', '/index.ts', '/index.tsx'):
            candidate = Path(str(resolved) + ext)
            if candidate.exists():
                try:
                    return str(candidate.relative_to(repo_root))
                except ValueError:
                    return None
    return None


def build_import_graph(repo_path: Path, language: str = 'python') -> ImportGraph:
    """Scan a repository and build an import dependency graph.

    Walks the repository tree, reads source files, extracts import statements
    via regex, and resolves them to project-relative paths.

    Args:
        repo_path: Root directory of the repository to scan.
        language: One of ``python``, ``typescript``, or ``javascript``.

    Returns:
        Populated ImportGraph with files and edges.
    """
    repo_path = Path(repo_path).resolve()
    graph = ImportGraph()
    extensions = {
        'python': ['.py'],
        'typescript': ['.ts', '.tsx'],
        'javascript': ['.js', '.jsx'],
    }
    exts = extensions.get(language, ['.py'])
    skip_dirs = {'node_modules', '__pycache__', '.git', '.venv', 'venv', 'site-packages'}

    file_contents: Dict[str, str] = {}
    for filepath in sorted(repo_path.rglob('*')):
        if not filepath.is_file() or filepath.suffix not in exts:
            continue
        if any(part in skip_dirs for part in filepath.parts):
            continue
        rel = str(filepath.relative_to(repo_path))
        graph.files.append(rel)
        try:
            file_contents[rel] = filepath.read_text(errors='ignore')
        except OSError:
            file_contents[rel] = ''

    all_files = set(graph.files)
    for rel, content in file_contents.items():
        if language == 'python':
            for match in _PYTHON_IMPORT_RE.finditer(content):
                mod = match.group(1) or match.group(2)
                if not mod:
                    continue
                mod = mod.split(',')[0].split(' as ')[0].strip()
                if mod.startswith('.') or mod.startswith('_'):
                    continue
                resolved = _resolve_python_module(repo_path, mod)
                if resolved and resolved in all_files:
                    graph.add_edge(rel, resolved)

        elif language in ('typescript', 'javascript'):
            for match in _TS_IMPORT_RE.finditer(content):
                mod = match.group(1) or match.group(2)
                if not mod:
                    continue
                resolved = _resolve_ts_module(repo_path, mod, rel)
                if resolved and resolved in all_files:
                    graph.add_edge(rel, resolved)

    return graph
