"""sandbox_local_runner.git_utils — Git helpers and docs index management."""

from __future__ import annotations

import ast
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .models import now_iso, parse_iso
from .utils import run_capture


def _git_last_commit_for_path(repo_path: Path, relative_path: str) -> str:
    rc, out = run_capture(['git', 'log', '-n', '1', '--format=%H', '--', relative_path], cwd=repo_path)
    if rc != 0:
        return ''
    return out.strip()


def _code_paths_for_docs_index(repo_path: Path) -> List[Path]:
    src_dir = repo_path / 'src' / 'qa_sandbox'
    paths: List[Path] = []
    if src_dir.exists():
        paths.extend(sorted(x for x in src_dir.glob('*.py') if x.is_file()))
    legacy_price = repo_path / 'price.py'
    if legacy_price.exists():
        paths.append(legacy_price)
    return sorted(paths)


def _has_inline_doc(relative_path: Path) -> bool:
    try:
        source = relative_path.read_text(encoding='utf-8')
        tree = ast.parse(source)
    except Exception:
        return False

    if ast.get_docstring(tree):
        return True

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and ast.get_docstring(node):
            return True
    return False


def _external_doc_text(repo_path: Path) -> str:
    docs_files: List[Path] = [repo_path / 'README.md']
    docs_dir = repo_path / 'docs'
    if docs_dir.exists():
        docs_files.extend(sorted(x for x in docs_dir.glob('*.md') if x.is_file()))

    chunks: List[str] = []
    for path in docs_files:
        try:
            chunks.append(path.read_text(encoding='utf-8').lower())
        except Exception:
            continue
    return '\n'.join(chunks)


def refresh_docs_index(repo_path: Path, docs_index_file: Path, log_file: Path) -> List[Dict[str, Any]]:
    from .state import _append_text

    docs_text = _external_doc_text(repo_path)
    generated_at = now_iso()
    entries: List[Dict[str, Any]] = []

    for abs_path in _code_paths_for_docs_index(repo_path):
        rel = abs_path.relative_to(repo_path).as_posix()
        has_inline_doc = _has_inline_doc(abs_path)
        tokens = {
            rel.lower(),
            abs_path.name.lower(),
            abs_path.stem.lower(),
            f'qa_sandbox.{abs_path.stem}'.lower(),
            f'qa_sandbox/{abs_path.stem}'.lower(),
        }
        has_external_doc_ref = any(token in docs_text for token in tokens)
        if has_inline_doc and has_external_doc_ref:
            coverage_status = 'covered'
        elif has_inline_doc:
            coverage_status = 'inline-only'
        elif has_external_doc_ref:
            coverage_status = 'external-only'
        else:
            coverage_status = 'uncovered'

        entries.append({
            'code_path': rel,
            'has_inline_doc': has_inline_doc,
            'has_external_doc_ref': has_external_doc_ref,
            'coverage_status': coverage_status,
            'last_seen_sha': _git_last_commit_for_path(repo_path, rel),
            'last_updated': generated_at,
        })

    payload = {
        'generated_at': generated_at,
        'repo_path': str(repo_path),
        'entries': entries,
    }
    docs_index_file.parent.mkdir(parents=True, exist_ok=True)
    docs_index_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    _append_text(log_file, f'docs-index: refreshed entries={len(entries)} file={docs_index_file}')
    return entries


def load_docs_index(docs_index_file: Path) -> List[Dict[str, Any]]:
    if not docs_index_file.exists():
        return []
    try:
        payload = json.loads(docs_index_file.read_text(encoding='utf-8'))
    except Exception:
        return []

    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = payload.get('entries', [])
    else:
        entries = []

    if not isinstance(entries, list):
        return []
    return [x for x in entries if isinstance(x, dict)]


def get_branch(repo_path: Path) -> str:
    """Get current git branch. Resolves state-dir paths to actual repo via config.yaml."""
    config_file = repo_path / 'config.yaml'
    if config_file.exists():
        import yaml
        try:
            cfg = yaml.safe_load(config_file.read_text())
            resolved = cfg.get('path')
            if resolved and Path(resolved).exists():
                repo_path = Path(resolved)
        except Exception:
            pass
    out = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=str(repo_path), text=True)
    return out.strip()
