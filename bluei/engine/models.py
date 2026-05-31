"""bluei.engine.models — Finding dataclass and date/time helpers."""

from __future__ import annotations

import enum
import hashlib
import uuid
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Finding:
    finding_id: str
    repo: str
    path: str
    line: int
    rule: str
    snippet: str
    confidence: float
    quick_win: bool
    safe_to_autofix: bool
    fix_attempts: int = 0
    last_fix_error: Optional[str] = None
    last_fix_at: Optional[str] = None
    fix_success: bool = False
    refactor_class: Optional[str] = None
    refactor_phase: Optional[str] = None
    discovered_at: Optional[str] = None
    severity: str = "medium"
    category: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def as_dict(self) -> Dict[str, Any]:
        """Alias for to_dict(). Prefer to_dict() in new code."""
        return self.to_dict()

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Finding:
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in allowed})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_seconds(
    value: Optional[str], reference: Optional[datetime] = None
) -> Optional[int]:
    ts = parse_iso(value)
    if ts is None:
        return None
    ref = reference or datetime.now(timezone.utc)
    return max(0, int((ref - ts).total_seconds()))


def stable_finding_id(repo: str, path: str, line: int, rule: str, snippet: str) -> str:
    material = f"{repo}|{path}|{line}|{rule}|{snippet.strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────
# Batch PR Engine — data models (Phase 1)
# ────────────────────────────────────────────────────────────────


class FixEngine(str, enum.Enum):
    """Available fix engine backends."""

    AUTO = "auto"
    DETERMINISTIC = "deterministic"
    CLAUDE = "claude"
    OPENCODE = "opencode"


class FixStatus(str, enum.Enum):
    """Status of a single fix attempt."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class FixMethod(str, enum.Enum):
    """Method used to apply a fix."""

    AUTOFIX = "autofix"
    CONTEXTUAL = "contextual"
    CLAUDE = "claude"


class BatchSeverity(str, enum.Enum):
    """Severity level for batch rule configuration."""

    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class BatchGroupBy(str, enum.Enum):
    """Grouping strategy for batching findings."""

    RULE = "rule"
    FILE = "file"
    DIRECTORY = "directory"
    CROSS_RULE = "cross-rule"
    SOLO = "solo"


class BatchStatus(str, enum.Enum):
    """Lifecycle states for a batch group."""

    OPEN = "open"
    FIXING = "fixing"
    FIXING_PARTIAL = "fixing_partial"
    PR_CREATED = "pr_created"
    PR_MERGED = "pr_merged"
    FAILED = "failed"
    SPLIT = "split"
    ABORTED = "aborted"
    DRY_RUN = "dry_run"
    SKIPPED = "skipped"


@dataclass
class FixResult:
    """Result of fixing a single finding within a batch."""

    finding_id: str
    status: str = FixStatus.FAILED.value
    diff_lines: int = 0
    error: Optional[str] = None
    fix_method: str = FixMethod.AUTOFIX.value


@dataclass
class BatchRule:
    """Configuration for batching a specific rule or rule pattern."""

    rule_pattern: str
    enabled: bool = True
    group_by: str = BatchGroupBy.RULE.value
    max_batch_size: int = 20
    max_files_per_batch: int = 15
    max_loc_per_batch: int = 500
    isolation: dict = field(default_factory=dict)
    priority: int = 99
    severity: str = BatchSeverity.NORMAL.value


@dataclass
class BatchGroup:
    """A group of findings to be fixed in a single PR."""

    batch_id: str
    rule_pattern: str
    group_by: str = BatchGroupBy.RULE.value
    findings: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    max_files: int = 15
    max_loc: int = 500
    status: str = BatchStatus.OPEN.value
    worktree_path: Optional[Path] = None
    branch: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    fix_results: Dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0
    split_history: list = field(default_factory=list)

    @property
    def is_solo(self) -> bool:
        return len(self.findings) == 1

    @property
    def file_count(self) -> int:
        return len({f.path for f in self.findings})

    def pr_title(self) -> str:
        if self.is_solo:
            f = self.findings[0]
            return f"fix: resolve {f.rule} in {f.path}"
        # Cross-rule: mixed rules in one batch
        unique_rules = {f.rule for f in self.findings}
        if len(unique_rules) > 1:
            return f"fix: resolve {len(self.findings)} linter specks ({len(unique_rules)} rules)"
        return f"fix: resolve {len(self.findings)} {self.rule_pattern} specks"

    def pr_body(self) -> str:
        if self.is_solo:
            return self._solo_body()
        # Cross-rule: use grouped-by-rule body
        unique_rules = {f.rule for f in self.findings}
        if len(unique_rules) > 1:
            return self._cross_rule_body()
        return self._batch_body()

    def _solo_body(self) -> str:
        f = self.findings[0]
        return (
            f"## Fix: {f.rule}\n\n"
            f"- **File:** `{f.path}`\n"
            f"- **Line:** {f.line}\n"
            f"- **Rule:** `{f.rule}`\n"
            f"- **Snippet:** {f.snippet}\n\n"
            f"---\n*bluei sweep*\n"
        )

    def _cross_rule_body(self) -> str:
        """Body for cross-rule batches — findings grouped by rule."""
        unique_rules = sorted({f.rule for f in self.findings})
        files = {f.path for f in self.findings}
        header = (
            f"## Batch Fix: {len(self.findings)} linter specks across "
            f"{len(unique_rules)} rules\n\n"
        )
        sections = ["### Specks by Rule\n"]
        for rule in unique_rules:
            rule_findings = [f for f in self.findings if f.rule == rule]
            sections.append(f"#### {rule} ({len(rule_findings)} specks)\n")
            sections.append("| # | File | Line |\n")
            sections.append("|---|------|------|\n")
            for i, f in enumerate(rule_findings, 1):
                sections.append(f"| {i} | `{f.path}` | {f.line} |\n")
            sections.append("\n")
        body = header + "".join(sections)
        body += (
            f"### Scope\n"
            f"- Files changed: {len(files)}\n"
            f"- Fix method: autofix\n\n"
            f"### Verification\n"
            f"- [ ] All target detectors no longer fire\n"
            f"- [ ] No baseline regressions\n\n"
            f"---\n*bluei sweep*\n"
        )
        return body

    def _batch_body(self) -> str:
        files = {f.path for f in self.findings}
        rows = []
        for i, f in enumerate(self.findings, 1):
            issue_num = None
            for issue in self.issues:
                if issue.get("finding_id") == f.finding_id:
                    issue_num = issue.get("github", {}).get("issue_number")
                    break
            issue_link = f"[#{issue_num}](...)" if issue_num else "unlinked"
            rows.append(f"| {i} | `{f.path}` | {f.line} | {issue_link} |")
        table_rows = "\n".join(rows)
        return (
            f"## Batch Fix: {len(self.findings)} {self.rule_pattern} specks\n\n"
            f"This PR resolves {len(self.findings)} specks of type "
            f"`{self.rule_pattern}` across {len(files)} files.\n\n"
            f"### Specks\n\n"
            f"| # | File | Line | Issue |\n"
            f"|---|------|------|-------|\n"
            f"{table_rows}\n\n"
            f"### Scope\n"
            f"- Files changed: {len(files)}\n"
            f"- Fix method: autofix\n\n"
            f"### Verification\n"
            f"- [ ] All target detectors no longer fire\n"
            f"- [ ] No baseline regressions\n\n"
            f"---\n*bluei sweep*\n"
        )

    @classmethod
    def from_solo(cls, issue: dict, finding: "Finding") -> "BatchGroup":
        """Create a solo batch (single finding)."""
        return cls(
            batch_id=f"solo-{finding.finding_id[:8]}",
            rule_pattern=finding.rule,
            group_by="solo",
            findings=[finding],
            issues=[issue],
        )

    @classmethod
    def from_findings(
        cls,
        findings: list,
        issues_map: Dict[str, dict],
        rule_config: "BatchRule",
    ) -> "BatchGroup":
        """Create a multi-finding batch."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d")
        rule_short = findings[0].rule.replace("ruff-", "")[:8]
        batch_id = f"batch-{ts}-{rule_short}-{uuid.uuid4().hex[:4]}"
        return cls(
            batch_id=batch_id,
            rule_pattern=findings[0].rule,
            group_by=rule_config.group_by,
            findings=findings,
            issues=[issues_map[f.finding_id] for f in findings],
            max_files=rule_config.max_files_per_batch,
            max_loc=rule_config.max_loc_per_batch,
        )

    def to_record(self) -> Dict[str, Any]:
        """Serialize batch for JSONL persistence."""
        return {
            "batch_id": self.batch_id,
            "rule_pattern": self.rule_pattern,
            "group_by": self.group_by,
            "status": self.status,
            "created_at": now_iso(),
            "findings": [
                {
                    "finding_id": f.finding_id,
                    "path": f.path,
                    "line": f.line,
                    "rule": f.rule,
                    "issue_id": (i.get("issue_id") or i.get("id")),
                    "fix_status": self.fix_results.get(f.finding_id, {}).get(
                        "status", "pending"
                    )
                    if isinstance(self.fix_results.get(f.finding_id), dict)
                    else "pending",
                }
                for f, i in zip(self.findings, self.issues)
            ],
            "worktree_path": str(self.worktree_path) if self.worktree_path else None,
            "branch": self.branch,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "fix_results": {
                fid: (
                    r
                    if isinstance(r, dict)
                    else {
                        "finding_id": r.finding_id,
                        "status": r.status,
                        "diff_lines": r.diff_lines,
                        "error": r.error,
                        "fix_method": r.fix_method,
                    }
                )
                for fid, r in self.fix_results.items()
            },
            "retry_count": self.retry_count,
            "split_history": self.split_history,
        }
