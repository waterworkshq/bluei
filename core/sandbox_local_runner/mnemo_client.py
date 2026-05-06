from __future__ import annotations

"""
mnemo_client — Python-native engram access + backward-compatible mnemo API.

Backward-compatible constants (used by test_mnemo_client.py):
  MNEMO_MAX_CHARS = int(os.environ.get("MNEMO_MAX_CHARS", "2000"))
  MNEMO_RECALL_LIMIT = int(os.environ.get("MNEMO_RECALL_LIMIT", "5"))
  MNEMO_TIMEOUT_SECONDS = int(os.environ.get("MNEMO_TIMEOUT_SECONDS", "30"))

Two access paths:
1. Python-native (fast, <50ms): direct SQLite to {repo}/.mnemo/db/memory.db
   → use `get_context_for_finding(finding)` for fast code context

2. TypeScript CLI (slow, ~10s+): RecallTool via bun subprocess
   → `recall(finding)` / `seed(...)` for session memory

For QA agent fix context, prefer `get_context_for_finding()`.
"""

import logging
import os
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .models import Finding

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

ENGRAM_MAX_PATTERNS = int(os.environ.get("ENGRAM_MAX_PATTERNS", "20"))
# Backward-compatible constants (for test compatibility)
MNEMO_MAX_CHARS = int(os.environ.get("MNEMO_MAX_CHARS", "2000"))
MNEMO_RECALL_LIMIT = int(os.environ.get("MNEMO_RECALL_LIMIT", "5"))
MNEMO_TIMEOUT_SECONDS = int(os.environ.get("MNEMO_TIMEOUT_SECONDS", "30"))

ENGRAM_MAX_FILES = int(os.environ.get("ENGRAM_MAX_FILES", "5"))
ENGRAM_MAX_CALLERS = int(os.environ.get("ENGRAM_MAX_CALLERS", "10"))
ENGRAM_MAX_CALLEES = int(os.environ.get("ENGRAM_MAX_CALLLEES", "10"))
ENGRAM_MAX_DEPS = int(os.environ.get("ENGRAM_MAX_DEPS", "10"))

# ─────────────────────────────────────────────────────────────────────────────
# Module-level caches
# ─────────────────────────────────────────────────────────────────────────────

_mnemo_available_cache: dict[str, bool] = {}
_conn_cache: dict[str, sqlite3.Connection] = {}


def _mnemo_enabled() -> bool:
    return os.environ.get("MNEMO_ENABLED", "1") != "0"


def is_mnemo_available(repo_path: Path) -> bool:
    """Check if the engram database is accessible for the given repo."""
    if not _mnemo_enabled():
        return False

    repo_key = str(Path(repo_path).resolve())
    cached = _mnemo_available_cache.get(repo_key)
    if cached is not None:
        return cached

    db_path = Path(repo_path) / ".mnemo" / "db" / "memory.db"
    available = db_path.exists()
    _mnemo_available_cache[repo_key] = available
    return available


def _get_conn(repo_path: Path) -> Optional[sqlite3.Connection]:
    """Get or create a cached SQLite connection for the repo's engram DB."""
    cache_key = str(Path(repo_path).resolve())
    if cache_key in _conn_cache:
        return _conn_cache[cache_key]

    db_path = Path(repo_path) / ".mnemo" / "db" / "memory.db"
    if not db_path.exists():
        return None

    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA query_only = ON")
        _conn_cache[cache_key] = conn
        return conn
    except Exception:
        return None


def _project_for(repo_path: Path) -> str:
    repo_path = Path(repo_path).resolve()
    config_path = repo_path / ".mnemo" / "config.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            project_name = data.get("project", {}).get("name")
            if isinstance(project_name, str) and project_name.strip():
                return project_name.strip()
        except Exception:
            pass
    return repo_path.name


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PatternMatch:
    pattern_type: str
    content: str
    file_path: str
    line_number: int
    frequency: int

    def format_brief(self) -> str:
        return f"[{self.pattern_type}] {self.file_path}:{self.line_number}"


@dataclass
class FileRelevance:
    file_path: str
    match_count: int
    pattern_types: List[str]

    def format(self) -> str:
        types = ", ".join(sorted(self.pattern_types)[:5])
        return f"**{self.file_path}** — {self.match_count} matches ({types})"


@dataclass
class CallGraphEntry:
    caller_file: str
    caller_symbol: str
    callee_file: Optional[str]
    callee_symbol: Optional[str]
    line_number: int
    is_async: bool = False

    def format_callers(self) -> str:
        return f"← **{self.caller_symbol}** ({self.caller_file}:{self.line_number})"

    def format_callees(self) -> str:
        if self.callee_file:
            return f"→ **{self.callee_symbol}** ({self.callee_file})"
        return f"→ **{self.callee_symbol}**"


@dataclass
class Dependency:
    source_file: str
    imported_module: str
    imported_name: Optional[str]

    def format(self) -> str:
        if self.imported_name:
            return f"import {self.imported_module}.{self.imported_name}"
        return f"import {self.imported_module}"


# ─────────────────────────────────────────────────────────────────────────────
# MnemoClient
# ─────────────────────────────────────────────────────────────────────────────

class MnemoClient:
    """
    Unified mnemo client with Python-native engram access.

    Methods:
    - is_available() → bool
    - get_context_for_finding(finding) → str | None  (fast, <50ms)
    - recall(finding) → str | None                   (slow CLI fallback)
    - seed(finding, outcome, error, changes) → bool

    All methods degrade gracefully — they never raise, only return None/False.
    """

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = Path(repo_path)
        self._project = _project_for(self.repo_path)
        self._conn: Optional[sqlite3.Connection] = None
        self.logger = logging.getLogger(__name__)

    def is_available(self) -> bool:
        if not _mnemo_enabled():
            return False
        if self._conn is None:
            self._conn = _get_conn(self.repo_path)
        return self._conn is not None

    # ── fast native queries ────────────────────────────────────────────────

    def find_relevant_files(self, query: str, limit: int = ENGRAM_MAX_FILES) -> List[FileRelevance]:
        """Find files with patterns matching the query."""
        if not self.is_available():
            return []
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT file_path, COUNT(*) as cnt,
                   GROUP_CONCAT(DISTINCT pattern_type) as types
            FROM engram_patterns
            WHERE project = ? AND (content LIKE ? OR file_path LIKE ?)
            GROUP BY file_path
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (self._project, f"%{query}%", f"%{query}%", limit),
        )
        return [
            FileRelevance(file_path=row[0], match_count=row[1], pattern_types=row[2].split(",") if row[2] else [])
            for row in cur.fetchall()
        ]

    def find_relevant_files_for_finding(self, finding: Finding, limit: int = ENGRAM_MAX_FILES) -> List[FileRelevance]:
        """Find likely-relevant files for a finding using rule, snippet, and path hints."""
        if not self.is_available():
            return []

        target_path = Path(finding.path)
        target_dir = "" if str(target_path.parent) == "." else str(target_path.parent)
        target_name = target_path.name
        target_stem = target_path.stem
        terms = self._extract_search_terms(finding)
        queries: List[str] = []
        seen_queries: set[str] = set()
        for item in [finding.rule, target_name, target_stem, *terms]:
            value = (item or "").strip()
            if not value:
                continue
            key = value.lower()
            if key in seen_queries:
                continue
            seen_queries.add(key)
            queries.append(value)

        scored: dict[str, FileRelevance] = {}
        bonus: dict[str, int] = {}

        for query in queries[:6]:
            for row in self.find_relevant_files(query, limit=max(limit * 3, 10)):
                existing = scored.get(row.file_path)
                if existing is None:
                    scored[row.file_path] = FileRelevance(
                        file_path=row.file_path,
                        match_count=row.match_count,
                        pattern_types=list(row.pattern_types),
                    )
                else:
                    existing.match_count += row.match_count
                    existing.pattern_types = list(sorted(set(existing.pattern_types + row.pattern_types)))

                file_bonus = 0
                if row.file_path == finding.path:
                    file_bonus += 50
                if target_dir and row.file_path.startswith(target_dir):
                    file_bonus += 15
                if target_name and row.file_path.endswith(target_name):
                    file_bonus += 20
                if target_stem and target_stem.lower() in row.file_path.lower():
                    file_bonus += 10
                bonus[row.file_path] = bonus.get(row.file_path, 0) + file_bonus

        ranked = sorted(
            scored.values(),
            key=lambda fr: (fr.match_count + bonus.get(fr.file_path, 0), fr.file_path == finding.path, -len(fr.pattern_types)),
            reverse=True,
        )
        return ranked[:limit]

    def search_patterns(
        self, query: str, pattern_types: Optional[List[str]] = None, limit: int = ENGRAM_MAX_PATTERNS
    ) -> List[PatternMatch]:
        """Search for code patterns matching the query."""
        if not self.is_available():
            return []
        cur = self._conn.cursor()
        if pattern_types:
            placeholders = ", ".join("?" * len(pattern_types))
            sql = f"""
                SELECT pattern_type, content, file_path, line_number, frequency
                FROM engram_patterns
                WHERE project = ? AND content LIKE ?
                  AND pattern_type IN ({placeholders})
                ORDER BY frequency DESC
                LIMIT ?
            """
            params = [self._project, f"%{query}%"] + pattern_types + [limit]
        else:
            sql = """
                SELECT pattern_type, content, file_path, line_number, frequency
                FROM engram_patterns
                WHERE project = ? AND content LIKE ?
                ORDER BY frequency DESC
                LIMIT ?
            """
            params = [self._project, f"%{query}%", limit]
        cur.execute(sql, params)
        return [
            PatternMatch(pattern_type=row[0], content=row[1], file_path=row[2], line_number=row[3], frequency=row[4])
            for row in cur.fetchall()
        ]

    def get_callers(self, symbol: str, limit: int = ENGRAM_MAX_CALLERS) -> List[CallGraphEntry]:
        """Get functions/methods that call the given symbol."""
        if not self.is_available():
            return []
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT caller_file, caller_symbol, callee_file, callee_symbol, line_number, is_async
            FROM engram_calls
            WHERE project = ? AND callee_symbol = ?
            ORDER BY confidence DESC
            LIMIT ?
            """,
            (self._project, symbol, limit),
        )
        return [
            CallGraphEntry(
                caller_file=row[0], caller_symbol=row[1], callee_file=row[2],
                callee_symbol=row[3], line_number=row[4], is_async=bool(row[5]),
            )
            for row in cur.fetchall()
        ]

    def get_callees(self, symbol: str, limit: int = ENGRAM_MAX_CALLEES) -> List[CallGraphEntry]:
        """Get functions/methods called by the given symbol."""
        if not self.is_available():
            return []
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT caller_file, caller_symbol, callee_file, callee_symbol, line_number, is_async
            FROM engram_calls
            WHERE project = ? AND caller_symbol = ?
            ORDER BY confidence DESC
            LIMIT ?
            """,
            (self._project, symbol, limit),
        )
        return [
            CallGraphEntry(
                caller_file=row[0], caller_symbol=row[1], callee_file=row[2],
                callee_symbol=row[3], line_number=row[4], is_async=bool(row[5]),
            )
            for row in cur.fetchall()
        ]

    def get_dependencies(self, file_path: str, limit: int = ENGRAM_MAX_DEPS) -> List[Dependency]:
        """Get import statements in a file."""
        if not self.is_available():
            return []
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT source_file, imported_module, imported_name
            FROM engram_dependencies
            WHERE project = ? AND source_file = ?
            ORDER BY imported_module, imported_name
            LIMIT ?
            """,
            (self._project, file_path, limit),
        )
        return [
            Dependency(source_file=row[0], imported_module=row[1], imported_name=row[2])
            for row in cur.fetchall()
        ]

    def get_symbols_for_file(self, file_path: str, limit: int = 20) -> List[PatternMatch]:
        """Get all patterns (functions, classes, etc.) defined in a file."""
        if not self.is_available():
            return []
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT pattern_type, content, file_path, line_number, frequency
            FROM engram_patterns
            WHERE project = ? AND file_path = ?
              AND pattern_type IN ('function','class','method','decorator')
            ORDER BY line_number
            LIMIT ?
            """,
            (self._project, file_path, limit),
        )
        return [
            PatternMatch(pattern_type=row[0], content=row[1], file_path=row[2], line_number=row[3], frequency=row[4])
            for row in cur.fetchall()
        ]

    # ── context building ──────────────────────────────────────────────────

    def get_context_for_finding(self, finding: Finding) -> Optional[str]:
        """
        Build a rich context string for a QA agent finding using fast native queries.

        Returns None if the engram DB is unavailable.
        """
        if not self.is_available():
            return None

        lines: List[str] = []
        file_path = finding.path

        # 1. Relevant files
        relevant_files = self.find_relevant_files_for_finding(finding, limit=5)
        if relevant_files:
            lines.append("## Relevant files (from engram index)")
            for fr in relevant_files:
                lines.append(f"  {fr.format()}")
            lines.append("")

        # 2. Search patterns — key terms from finding
        for term in self._extract_search_terms(finding)[:3]:
            patterns = self.search_patterns(term, limit=5)
            if patterns:
                lines.append(f"## Patterns matching: `{term}`")
                for p in patterns:
                    lines.append(f"  {p.format_brief()} — `{p.content[:70]}`")
                lines.append("")

        # 3. Call graph for file symbols
        for sym in self.get_symbols_for_file(file_path, limit=3)[:2]:
            func_name = sym.content.split("(")[0].strip()
            callers = self.get_callers(func_name, limit=3)
            callees = self.get_callees(func_name, limit=3)
            if callers or callees:
                lines.append(f"## Call graph: `{sym.content[:60]}`")
                for c in callers:
                    lines.append(f"  {c.format_callers()}")
                for c in callees:
                    lines.append(f"  {c.format_callees()}")
                lines.append("")

        # 4. Dependencies
        deps = self.get_dependencies(file_path, limit=10)
        if deps:
            lines.append(f"## Dependencies: {file_path}")
            for d in deps[:8]:
                lines.append(f"  {d.format()}")
            lines.append("")

        # 5. Similar patterns in same file
        same_file = [p for p in self.search_patterns(finding.rule, limit=5) if p.file_path == file_path]
        if same_file:
            lines.append(f"## Other {finding.rule} in {file_path}")
            for p in same_file:
                lines.append(f"  line {p.line_number}: `{p.content[:80]}`")

        return "\n".join(lines) if lines else None

    def _extract_search_terms(self, finding: Finding) -> List[str]:
        """Extract meaningful search terms from a finding."""
        import re

        terms: List[str] = [finding.rule]
        snippet = finding.snippet

        # Class/function names
        for ident in re.findall(r"\b([A-Z][a-zA-Z0-9_]+)\b", snippet):
            if len(ident) > 3 and ident not in ("True", "False", "None", "self"):
                terms.append(ident)

        # Function calls
        for fc in re.findall(r"\b([a-z_][a-z0-9_]*)\s*\(", snippet.lower()):
            if len(fc) > 2:
                terms.append(fc)

        # Rule-specific keywords
        rule_keywords = {
            "ruff-b904": ["raise from err", "exception cause"],
            "ruff-s311": ["random generator", "secrets module"],
            "ruff-c408": ["dict()", "dict literal"],
            "ruff-b007": ["unused loop variable"],
            "ruff-e501": ["line too long"],
            "ruff-f401": ["unused import"],
            "ruff-f841": ["unused variable"],
        }
        if finding.rule in rule_keywords:
            terms.extend(rule_keywords[finding.rule])

        # Deduplicate
        seen: set[str] = set()
        unique: List[str] = []
        for t in terms:
            if t.lower() not in seen:
                seen.add(t.lower())
                unique.append(t)

        return unique[:5]

    # ── backward-compatible CLI methods ─────────────────────────────────

    def recall(self, finding: Finding) -> Optional[str]:
        """
        Get context for a finding.

        Fast path: uses Python-native engram DB (<50ms).
        Falls back to CLI if native returns nothing.
        """
        fast = self.get_context_for_finding(finding)
        if fast:
            return fast
        return self._recall_via_cli(finding)

    def seed(self, finding: Finding, outcome: str, error: Optional[str], changes: Optional[str]) -> bool:
        """
        Seed mnemo with structured finding + outcome data.

        Writes to mnemo via CLI capture events.
        Returns True if all events succeeded.
        """
        if not is_mnemo_available(self.repo_path):
            return False

        session_id = f"qa-{finding.finding_id}"
        cwd = str(self.repo_path)

        def capture(
            event: str,
            role: Optional[str] = None,
            content: Optional[str] = None,
            cwd: Optional[str] = None,
        ) -> bool:
            args = ["mnemo", "capture", "--event", event, "--session-id", session_id]
            effective_cwd = cwd or str(self.repo_path)
            if effective_cwd:
                args.extend(["--cwd", effective_cwd])
            if role:
                args.extend(["--role", role])
            if content:
                args.extend(["--content", content])
            try:
                r = subprocess.run(args, capture_output=True, text=True, timeout=10)
                return r.returncode == 0
            except Exception:
                return False

        ok = True
        ok &= capture("session-start", cwd=cwd)
        ok &= capture(
            "user",
            content=f"Finding: {finding.rule} in {finding.path}:{finding.line}\n"
            f"Snippet: {finding.snippet[:200]}\nConfidence: {finding.confidence}\n"
            f"Quick fix: {finding.quick_win}\nSafe to autofix: {finding.safe_to_autofix}",
        )
        outcome_content = (
            f"Fix attempt for {finding.rule} in {finding.path}:{finding.line}\nOutcome: {outcome}"
        )
        if error:
            outcome_content += f"\nError: {error}"
        if changes:
            outcome_content += f"\nChanges: {changes}"
        ok &= capture("assistant", content=outcome_content)
        ok &= capture("session-end", cwd=cwd)
        return ok

    def _recall_via_cli(self, finding: Finding) -> Optional[str]:
        """
        Fallback: call engram CLI for file context.

        NOTE: This is slow (~10s+) due to CLI EngramStore init overhead.
        Only used when the native path returns nothing.
        """
        CLI = "/media/vikas/Work/mem-db/packages/cli/dist/src/cli.js"
        query = f"{finding.rule} {finding.path}:{finding.line}"

        try:
            result = subprocess.run(
                ["bun", CLI, "engram", "files", query, "--limit", "5"],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=str(self.repo_path),
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass

        return None
