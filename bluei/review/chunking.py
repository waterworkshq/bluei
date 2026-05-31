#!/usr/bin/env python3
"""Chunking utilities — extracted from review.cycle for single-responsibility."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Language priority order for chunking (higher priority = processed first)
# Languages that are more likely to have review-relevant findings come first.
_LANGUAGE_PRIORITY = {
    "typescript": 10,
    "javascript": 9,
    "python": 8,
    "go": 7,
    "rust": 6,
    "java": 5,
    "cpp": 4,
    "c": 3,
    "ruby": 2,
    "php": 1,
}


def _get_language_from_path(path: str) -> str:
    """Infer language from file path extension (simple heuristic)."""
    ext = Path(path).suffix.lower()
    mapping = {
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".py": "python",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".cxx": "cpp",
        ".c": "c",
        ".h": "c",
        ".hpp": "cpp",
        ".rb": "ruby",
        ".php": "php",
    }
    return mapping.get(ext, "")


def _estimate_file_size_token_proxy(path: str) -> int:
    """Estimate file weight for chunking via line count (placeholder for real tokenization)."""
    try:
        p = Path(path)
        if p.exists() and p.is_file():
            return len(p.read_text(encoding="utf-8", errors="replace").splitlines())
    except (OSError, UnicodeDecodeError):
        pass
    return 0


def order_files_for_chunking(
    files: List[str],
    language: Optional[str] = None,
) -> List[str]:
    """
    Return a deterministically ordered list of file paths for future chunking.

    Ordering rules (applied in priority order):
    1. Language priority descending — files in the repo's primary language first
    2. File size/token proxy descending — larger files first (more content to review)
    3. Path ascending (lexicographic) — stable tiebreaker for identical priority/size

    This ordering is stable across calls for the same input, making it safe
    to use as a pre-pass ordering step before chunking.

    Args:
        files: List of file paths to order.
        language: Primary language of the repo (optional, used for priority scoring).
                  If not provided, inferred from file extensions.

    Returns:
        A new list of files sorted by the rules above (does not mutate input).
    """
    if not files:
        return []

    # Build (priority, size_proxy, path) tuples for stable sort
    # If a repo primary language is provided, files matching it get a strong boost.
    repo_language = (language or "").lower().strip()
    scored: List[Tuple[int, int, str]] = []
    for f in files:
        file_language = _get_language_from_path(f).lower()
        priority = _LANGUAGE_PRIORITY.get(file_language, 0)
        if repo_language and file_language == repo_language:
            priority += 100
        elif not repo_language:
            inferred = file_language
            priority = _LANGUAGE_PRIORITY.get(inferred, 0)
        size_proxy = _estimate_file_size_token_proxy(f)
        scored.append((priority, size_proxy, f))

    # Sort: priority desc, size desc, path asc (reversed for descending)
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [f for _, _, f in scored]


@dataclass
class ChunkManifest:
    """
    Manifest describing how a set of files is divided into chunks/passes
    for multi-pass LLM review.

    Attributes:
        mode: CompressionMode used for this run.
        token_budget: Maximum tokens available per pass (placeholder).
        total_files: Number of files in the manifest.
        total_chunks: Number of chunks the files are divided into.
        chunks: List of chunks, each containing a list of file paths.
        ordering: The deterministic file ordering used to produce the chunks.
    """

    mode: str = "full_diff"
    token_budget: int = 0
    total_files: int = 0
    total_chunks: int = 1
    chunks: List[List[str]] = field(default_factory=list)
    ordering: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChunkManifest":
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        filtered.setdefault("mode", "full_diff")
        filtered.setdefault("token_budget", 0)
        filtered.setdefault("total_files", 0)
        filtered.setdefault("total_chunks", 1)
        filtered.setdefault("chunks", [])
        filtered.setdefault("ordering", [])
        return cls(**filtered)


def build_chunk_manifest(
    files: List[str],
    mode: str = "full_diff",
    token_budget: int = 0,
    language: Optional[str] = None,
) -> ChunkManifest:
    """
    Build a chunk manifest for a set of files.

    For ``full_diff`` mode (default), all files go into a single chunk.
    For ``compressed`` or ``multi_pass`` modes, the files are first ordered
    deterministically and then divided into chunks.  The actual chunking
    logic (token-based splitting, pass scheduling) is a future extension;
    this helper provides the manifest structure and stable ordering.

    Args:
        files: List of file paths to include in the manifest.
        mode: CompressionMode value (``full_diff``, ``compressed``, ``multi_pass``).
        token_budget: Token budget per pass (placeholder; default 0 = no limit).
        language: Primary language of the repo (optional).

    Returns:
        A ``ChunkManifest`` describing the file set and its chunk layout.
    """
    ordered = order_files_for_chunking(files, language=language)

    if mode == "full_diff" or len(ordered) == 0:
        # Single pass: all files in one chunk
        chunks: List[List[str]] = [ordered] if ordered else []
        total_chunks = 1 if ordered else 0
    else:
        # Placeholder: for compressed/multi_pass, still use single-chunk
        # until real token-based splitting is implemented.
        # The manifest structure is ready; chunking logic is the future step.
        chunks = [ordered]
        total_chunks = 1

    return ChunkManifest(
        mode=mode,
        token_budget=token_budget,
        total_files=len(ordered),
        total_chunks=total_chunks,
        chunks=chunks,
        ordering=ordered,
    )
