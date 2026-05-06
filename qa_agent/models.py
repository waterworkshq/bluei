#!/usr/bin/env python3
"""Data models for QA Agent."""

from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import re
import hashlib
import uuid


class RepoStatus(str, Enum):
    IDLE = "idle"
    ONBOARDING = "onboarding"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


class SafetyMode(str, Enum):
    OBSERVE = "observe"
    ISSUE_ONLY = "issue-only"
    PR = "pr"
    MERGE = "merge"


class SafetyProfile(str, Enum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class ReviewMode(str, Enum):
    OBSERVATION = "observation"
    AUTONOMOUS_REVIEW = "autonomous-review"
    REMEDIATION = "remediation"


class CompressionMode(str, Enum):
    """
    Compression/chunking mode used for an autonomous review run.

    full_diff    - Entire diff/file set is processed in a single pass
                    (no chunking; all content fed to LLM together).
    compressed   - Content is compressed or summarized before being passed
                    to the LLM (e.g. via a separate summarization step).
    multi_pass   - Content is processed across multiple targeted passes,
                    each focusing on a specific subset or aspect.
    """
    FULL_DIFF = "full_diff"
    COMPRESSED = "compressed"
    MULTI_PASS = "multi_pass"


class LiveRolloutMode(str, Enum):
    """
    Rollout mode for autonomous-review live publication.

    local_only  - Default. No backend generation when live_actions=True.
                  No live GitHub publication. Safe local analysis only.
                  Backend IS used for local-only analysis when live_actions=False.

    shadow       - Backend generation + target resolution + summary build.
                  Do NOT actually post to GitHub. Record what would have
                  happened as a SHADOW publication entry. Useful to validate
                  the full pipeline (backend + targeting + summary) without
                  making any live GitHub API mutation.

    limited      - Full guarded path. Backend generation + live publication
                  only when guarded_live_review=True AND live_actions=True.
                  Requires a clear PR target. This is the standard guarded
                  live-review progression.
    """
    LOCAL_ONLY = "local_only"
    SHADOW = "shadow"
    LIMITED = "limited"


@dataclass
class MonitoredSafetyState:
    """
    Phase G7: Monitored-rollout safety state for circuit-breaker behavior.

    Tracks consecutive failures in guarded-live publication attempts to
    implement a circuit-breaker / open-cooldown pattern that prevents
    repeated live publication attempts after failures.

    Attributes:
        circuit_open: True when the circuit breaker has opened due to
                      repeated failures. Live publication is blocked while
                      circuit is open.
        failure_count: Number of consecutive guarded-live publish failures
                       since the last successful live publication.
        cooldown_until: ISO timestamp when the cooldown expires and the
                        circuit may close again. None if circuit is closed.
        last_failure_at: ISO timestamp of the most recent failure.
        last_failure_reason: Human-readable reason for the most recent failure.
        auto_rollback_active: True when monitored feedback has tripped a
                        fail-closed rollback for guarded live publication.
        auto_rollback_reason: Human-readable rollback trigger summary.
        auto_rollback_triggered_at: ISO timestamp when rollback was activated.
    """
    circuit_open: bool = False
    failure_count: int = 0
    cooldown_until: Optional[str] = None  # ISO timestamp
    last_failure_at: Optional[str] = None
    last_failure_reason: str = ""
    auto_rollback_active: bool = False
    auto_rollback_reason: str = ""
    auto_rollback_triggered_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MonitoredSafetyState":
        return cls(
            circuit_open=data.get("circuit_open", False),
            failure_count=data.get("failure_count", 0),
            cooldown_until=data.get("cooldown_until"),
            last_failure_at=data.get("last_failure_at"),
            last_failure_reason=data.get("last_failure_reason", ""),
            auto_rollback_active=data.get("auto_rollback_active", False),
            auto_rollback_reason=data.get("auto_rollback_reason", ""),
            auto_rollback_triggered_at=data.get("auto_rollback_triggered_at"),
        )

    def record_failure(self, reason: str, cooldown_seconds: int) -> None:
        """
        Record a publish failure and open the circuit if threshold exceeded.

        Args:
            reason: Human-readable failure reason.
            cooldown_seconds: How long the cooldown lasts in seconds.
        """
        from datetime import datetime, timezone, timedelta

        self.failure_count += 1
        self.last_failure_at = datetime.now(timezone.utc).isoformat()
        self.last_failure_reason = reason
        # Circuit opens when failure_count reaches the threshold (set by caller)
        # The caller checks threshold and sets circuit_open = True

    def record_success(self) -> None:
        """Reset failure tracking after a successful live publication."""
        self.circuit_open = False
        self.failure_count = 0
        self.cooldown_until = None
        self.last_failure_at = None
        self.last_failure_reason = ""

    def check_cooldown_ready(self) -> bool:
        """
        Check if cooldown period has elapsed and circuit can close.

        Returns:
            True if cooldown has expired (or was never set), False if still in cooldown.
        """
        if not self.circuit_open or not self.cooldown_until:
            return True
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        expires = datetime.fromisoformat(self.cooldown_until)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now >= expires


@dataclass
class LanguageInfo:
    name: str
    version: Optional[str] = None
    package_manager: Optional[str] = None
    build_tool: Optional[str] = None
    secondary_languages: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RepoConfig:
    """Repository configuration."""
    id: str
    name: str
    path: str
    language: str
    framework: Optional[str] = None
    enabled: bool = True
    plugin_id: str = ""
    
    # Discovery options
    discovery: Dict[str, Any] = field(default_factory=dict)
    
    # Rules
    rules_enabled: Optional[List[str]] = None
    rules_disabled: List[str] = field(default_factory=list)
    
    # Fix options
    fix_engine: str = "deterministic"
    fallback_engines: List[str] = field(default_factory=lambda: ['claude', 'opencode', 'deterministic'])
    claude_template: str = ""
    opencode_template: str = ""
    review_claude_template: str = ""
    review_opencode_template: str = ""
    
    # Validation
    baseline_checks: List[List[str]] = field(default_factory=list)
    
    # Limits
    limits: Dict[str, int] = field(default_factory=lambda: {
        'open_issues_cap': 20,
        'open_prs_cap': 5,
        'max_prs_per_run': 2,
        'max_issues_per_run': 10,
        'max_files_changed': 5,
        'max_loc_diff': 200,
        'max_fix_attempts': 3,
    })
    
    # Cooldowns
    cooldowns: Dict[str, int] = field(default_factory=lambda: {
        'finding_seconds': 14400,
        'merge_minutes': 30,
        'staleness_seconds': 7200,
    })
    
    # GitHub
    github: Dict[str, bool] = field(default_factory=lambda: {
        'live_actions': False,
        'auto_merge': False,
    })

    # Review care
    review_care: Dict[str, Any] = field(default_factory=lambda: {
        'enabled': True,
        'mode': ReviewMode.OBSERVATION.value,
        'provider_order': ['github'],
        'max_attempts': 3,
        'max_loops': 2,
        'max_prs_per_run': 1,
        'retry_delay_minutes': 15,
        'style_retry_threshold': 3,
        'allow_forks': False,
        'allow_unchanged_baseline_failures': True,
        'remediation_requires_validation': True,
        'conceptual_feedback_action': 'pause',
        'contradictory_feedback_action': 'pause',
        'cleanup_worktrees_after_push': True,
        # Phase G4: guarded live-review gate.
        # When True, enables backend generation and live GitHub publication.
        # Requires github.live_actions to also be True.
        # Default False (local-only mode) ensures safe testability on real repos.
        'guarded_live_review': False,
        # Phase G5: live rollout mode for autonomous-review progression.
        # local_only  - No backend when live_actions=True; safe local analysis only.
        # shadow       - Backend + targeting, but do NOT actually publish; record intent.
        # limited     - Full guarded live path (requires guarded_live_review + live_actions).
        'live_rollout_mode': LiveRolloutMode.LOCAL_ONLY.value,
        # Phase G7: monitored-rollout safety config.
        # Number of consecutive failures before circuit breaker opens.
        'monitored_failure_threshold': 3,
        # Cooldown duration in seconds after circuit opens.
        'monitored_cooldown_seconds': 300,
        # Optional fail-closed rollback based on recent feedback signals.
        'monitored_auto_rollback_enabled': False,
        'monitored_negative_feedback_threshold': 0.3,
        'monitored_feedback_min_events': 3,
        'monitored_feedback_window': 20,
    })

    # Safety
    safety: Dict[str, Any] = field(default_factory=lambda: {
        'mode': SafetyMode.OBSERVE.value,
        'profile': SafetyProfile.CONSERVATIVE.value,
        'require_clean_worktree': True,
        'protected_branches': ['main', 'master'],
        'allow_live_on_dirty_tree': False,
        'notes': [],
    })

    # Metadata / template provenance
    meta: Dict[str, Any] = field(default_factory=lambda: {
        'onboarding_version': 1,
        'template': None,
        'inferred_by': 'legacy',
    })
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RepoConfig':
        data = dict(data)
        if 'safety' not in data or not data.get('safety'):
            github = data.get('github', {}) or {}
            inferred_mode = SafetyMode.MERGE.value if github.get('live_actions') else SafetyMode.OBSERVE.value
            inferred_profile = SafetyProfile.BALANCED.value if github.get('live_actions') else SafetyProfile.CONSERVATIVE.value
            data['safety'] = {
                'mode': inferred_mode,
                'profile': inferred_profile,
                'require_clean_worktree': True,
                'protected_branches': ['main', 'master'],
                'allow_live_on_dirty_tree': False,
                'notes': ['safety policy inferred during config migration'],
            }
        if 'meta' not in data or not data.get('meta'):
            data['meta'] = {
                'onboarding_version': 1,
                'template': None,
                'inferred_by': 'migration',
            }
        if 'review_care' not in data or not data.get('review_care'):
            data['review_care'] = {
                'enabled': True,
                'mode': ReviewMode.OBSERVATION.value,
                'provider_order': ['github'],
                'max_attempts': 3,
                'max_loops': 2,
                'max_prs_per_run': 1,
                'retry_delay_minutes': 15,
                'style_retry_threshold': 3,
                'allow_forks': False,
                'allow_unchanged_baseline_failures': True,
                'remediation_requires_validation': True,
                'conceptual_feedback_action': 'pause',
                'contradictory_feedback_action': 'pause',
                'cleanup_worktrees_after_push': True,
                'guarded_live_review': False,
                'live_rollout_mode': LiveRolloutMode.LOCAL_ONLY.value,
                'monitored_auto_rollback_enabled': False,
                'monitored_negative_feedback_threshold': 0.3,
                'monitored_feedback_min_events': 3,
                'monitored_feedback_window': 20,
            }
        else:
            review_care = dict(data.get('review_care') or {})
            review_care.setdefault('mode', ReviewMode.OBSERVATION.value)
            review_care.setdefault('cleanup_worktrees_after_push', True)
            review_care.setdefault('guarded_live_review', False)
            review_care.setdefault('live_rollout_mode', LiveRolloutMode.LOCAL_ONLY.value)
            review_care.setdefault('monitored_auto_rollback_enabled', False)
            review_care.setdefault('monitored_negative_feedback_threshold', 0.3)
            review_care.setdefault('monitored_feedback_min_events', 3)
            review_care.setdefault('monitored_feedback_window', 20)
            data['review_care'] = review_care
        return cls(**data)
    
    def validate(self) -> List[str]:
        """Validate config fields. Returns list of error messages (empty = valid)."""
        errors: List[str] = []

        # Required string fields
        for field_name in ('name', 'id', 'language'):
            value = getattr(self, field_name, '')
            if not value or not isinstance(value, str):
                errors.append(f"{field_name}: must be a non-empty string (got {type(value).__name__}: {value!r})")

        # Path field
        if not self.path or not isinstance(self.path, str):
            errors.append(f"path: must be a non-empty string (got {type(self.path).__name__}: {self.path!r})")

        # Fix engine
        valid_engines = {'auto', 'deterministic', 'claude', 'opencode'}
        if self.fix_engine and self.fix_engine not in valid_engines:
            errors.append(f"fix_engine: must be one of {valid_engines} (got {self.fix_engine!r})")

        # Limits must be dict of ints
        if self.limits and isinstance(self.limits, dict):
            for k, v in self.limits.items():
                if not isinstance(v, int):
                    errors.append(f"limits.{k}: must be an int (got {type(v).__name__}: {v!r})")

        # Cooldowns must be dict of ints
        if self.cooldowns and isinstance(self.cooldowns, dict):
            for k, v in self.cooldowns.items():
                if not isinstance(v, int):
                    errors.append(f"cooldowns.{k}: must be an int (got {type(v).__name__}: {v!r})")

        # Safety mode must be valid
        if self.safety and isinstance(self.safety, dict):
            mode = self.safety.get('mode', '')
            if mode and mode not in {e.value for e in SafetyMode}:
                errors.append(f"safety.mode: invalid value {mode!r}")

        return errors

    @classmethod
    def from_yaml(cls, path: Path) -> 'RepoConfig':
        import yaml
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")
        with open(path) as f:
            data = yaml.safe_load(f)
        if data is None:
            raise ValueError(f"Empty or invalid YAML in {path}")
        return cls.from_dict(data)


@dataclass
class Finding:
    """A single finding from discovery."""
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
    discovered_at: Optional[str] = None
    severity: str = "medium"
    category: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Finding':
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        filtered.setdefault('fix_attempts', 0)
        filtered.setdefault('last_fix_error', None)
        filtered.setdefault('last_fix_at', None)
        filtered.setdefault('fix_success', False)
        filtered.setdefault('discovered_at', None)
        filtered.setdefault('severity', 'medium')
        filtered.setdefault('category', '')
        return cls(**filtered)


@dataclass
class HealthScore:
    """Repository health score."""
    score: float
    components: Dict[str, float]
    calculated_at: str
    
    @property
    def band(self) -> str:
        if self.score >= 90:
            return "excellent"
        elif self.score >= 70:
            return "good"
        elif self.score >= 50:
            return "needs_work"
        elif self.score >= 30:
            return "poor"
        else:
            return "critical"
    
    @property
    def color(self) -> str:
        colors = {
            "excellent": "green",
            "good": "blue",
            "needs_work": "yellow",
            "poor": "orange",
            "critical": "red"
        }
        return colors.get(self.band, "gray")
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'score': self.score,
            'components': self.components,
            'calculated_at': self.calculated_at,
            'band': self.band,
            'color': self.color,
        }


@dataclass
class Baseline:
    """Snapshot of repo health at onboarding."""
    id: str
    repo_id: str
    captured_at: str
    findings_total: int
    findings_by_category: Dict[str, int]
    findings_by_severity: Dict[str, int]
    health_score: float
    health_components: Dict[str, float]
    findings_file: str
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Repo:
    """Repository with runtime state."""
    config: RepoConfig
    status: RepoStatus = RepoStatus.IDLE
    onboarded_at: Optional[str] = None
    last_run_at: Optional[str] = None
    baseline: Optional[Baseline] = None
    current_findings_count: int = 0
    current_health_score: float = 0.0
    total_fixes: int = 0
    total_prs: int = 0
    total_merges: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'config': self.config.to_dict(),
            'status': self.status.value,
            'onboarded_at': self.onboarded_at,
            'last_run_at': self.last_run_at,
            'baseline': self.baseline.to_dict() if self.baseline else None,
            'current_findings_count': self.current_findings_count,
            'current_health_score': self.current_health_score,
            'total_fixes': self.total_fixes,
            'total_prs': self.total_prs,
            'total_merges': self.total_merges,
        }


@dataclass
class Run:
    """A single agent run."""
    id: str
    repo_id: str
    phase: str
    started_at: str
    ended_at: Optional[str] = None
    duration_seconds: int = 0
    dry_run: bool = True
    fix_engine: str = "deterministic"
    findings_detected: int = 0
    issues_created: int = 0
    fix_attempts: int = 0
    fixes_verified: int = 0
    fixes_failed: int = 0
    prs_created: int = 0
    merges_completed: int = 0
    health_before: float = 0.0
    health_after: float = 0.0
    health_delta: float = 0.0
    status: str = "running"
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def generate_id(prefix: str = "") -> str:
    """Generate a unique ID."""
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    unique = uuid.uuid4().hex[:8]
    return f"{prefix}-{ts}-{unique}" if prefix else f"{ts}-{unique}"


def now_iso() -> str:
    """Return current timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Autonomous-review models (Phase C1/C2)
# ---------------------------------------------------------------------------

class FindingSource(str, Enum):
    """Where a review finding originated."""
    LINTER = "linter"
    LLM = "llm"
    BASELINE = "baseline"
    REVIEW_FEEDBACK = "review-feedback"
    MANUAL = "manual"


class FindingActionability(str, Enum):
    """How directly actionable a finding is."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFORMATIONAL = "informational"


class FindingSeverity(str, Enum):
    """Normalized severity for review findings."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class FeedbackSentiment(str, Enum):
    """Sentiment classification for feedback events."""
    POSITIVE = "positive"
    NEGATIVE = "negative"
    CONCEPTUAL = "conceptual"
    CONTRADICTORY = "contradictory"
    MIXED = "mixed"


class FeedbackSource(str, Enum):
    """Source of feedback in the review loop."""
    HUMAN_REVIEWER = "human-reviewer"
    LLM_REVIEWER = "llm-reviewer"
    CI_CHECK = "ci-check"
    SELF_REVIEW = "self-review"


class ReviewRunStatus(str, Enum):
    """Status of a review run."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    EXHAUSTED = "exhausted"


class PublishStatus(str, Enum):
    """
    Publication status for a review finding or run.

    absent     - Finding was present in prior state but is absent from the
                 current candidate set.  For published findings this means
                 the issue appears resolved.
    pending    - New finding not yet published.
    published  - Successfully published to GitHub.
    failed     - Publishing was attempted but failed; error is captured.
    skipped    - Intentionally skipped (e.g. below confidence threshold).
    superseded - A finding with the same fingerprint was already published
                 in an earlier run; the current occurrence is a re-run.
    """
    ABSENT = "absent"
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"
    SKIPPED = "skipped"
    SUPERSEDED = "superseded"


class LearnedRuleStatus(str, Enum):
    """
    Lifecycle status for a learned rule.

    tentative   - Newly proposed; not yet active.  Must pass safety gates
                  and accumulate sufficient evidence before activating.
    active      - Activated and applying to findings.  Can be suppressed
                  by operator-authored rules or conflict resolution.
    rejected    - Rejected at proposal time due to safety gates or conflicts.
                  Never activates.
    superseded  - Was active but was later overridden by an operator-authored
                  rule or a newer learned rule with higher precedence.
    """
    TENTATIVE = "tentative"
    ACTIVE = "active"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


# ---------------------------------------------------------------------------
# Deterministic finding identity helpers (owned by QA-agent, not LLM input)
# ---------------------------------------------------------------------------

def normalize_finding_path(path: str) -> str:
    """
    Normalize a file path for consistent fingerprinting.

    - Strips leading/trailing whitespace
    - Converts backslashes to forward slashes
    - Collapses multiple slashes to single slash
    - Removes trailing slash

    Does NOT strip a repo prefix — that would require knowing the repo
    name and could accidentally strip legitimate path segments (e.g.
    ``src/`` from ``src/main.ts``). Callers should pre-strip the repo
    prefix before calling this function if needed.
    """
    if not path:
        return ""
    path = path.strip().replace("\\", "/")
    # Collapse multiple slashes
    while "//" in path:
        path = path.replace("//", "/")
    # Remove trailing slash
    path = path.rstrip("/")
    return path


def normalize_finding_header(header: str) -> str:
    """
    Normalize a header/rule identifier for fingerprinting.

    - Lowercases
    - Strips whitespace
    - Collapses internal whitespace to single space
    - Strips leading/trailing punctuation
    """
    if not header:
        return ""
    normalized = header.strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.strip(".-_/: ")
    return normalized


def make_finding_fingerprint(
    repo: str,
    path: str,
    line: int,
    header: str,
    snippet: str,
) -> str:
    """
    Generate a deterministic SHA-256 fingerprint for a finding.

    This fingerprint identifies the *logical* finding regardless of
    generated finding_id. It is stable across re-runs for the same
    code location + rule combination.

    Args:
        repo: repository identifier
        path: file path (will be normalized)
        line: line number
        header: rule/header identifier (will be normalized)
        snippet: code snippet (will be trimmed before hashing)

    Returns:
        64-character hex SHA-256 fingerprint
    """
    norm_path = normalize_finding_path(path)
    norm_header = normalize_finding_header(header)
    # Trim snippet to first 200 chars for stability
    snippet = (snippet or "")[:200].strip()
    payload = "|".join(str(x) for x in [repo, norm_path, line, norm_header, snippet])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_review_finding_id(fingerprint: str, attempt: int = 0) -> str:
    """
    Generate a QA-agent-owned finding_id from a fingerprint + attempt.

    The finding_id is deterministic for a given fingerprint + attempt
    combination, making it stable across review runs for the same finding.

    Format: ``rf-{fingerprint[:12]}-{attempt:03d}``

    Args:
        fingerprint: 64-char SHA-256 hex from make_finding_fingerprint
        attempt: non-negative integer (0 = first occurrence)

    Returns:
        Stable finding_id string
    """
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    short_fp = fingerprint[:12]
    return f"rf-{short_fp}-{attempt:03d}"


# ---------------------------------------------------------------------------
# Autonomous review data models
# ---------------------------------------------------------------------------

@dataclass
class ReviewFinding:
    """
    A finding produced during an autonomous review run.

    This is distinct from the older ``Finding`` dataclass which is
    linter-output oriented.  ``ReviewFinding`` captures a finding that
    may come from an LLM review, a linter, a baseline diff, or
    human review feedback.

    Identity is owned by the QA-agent: the ``finding_id`` is
    deterministically generated from a content fingerprint and is
    stable across re-runs for the same logical finding.
    """
    # Identity (QA-owned, deterministic)
    finding_id: str
    finding_fingerprint: str  # SHA-256 hex from make_finding_fingerprint

    # Core location
    repo: str
    path: str
    line: int
    header: str  # e.g. rule name, LLM-generated label

    # Classification
    source: FindingSource
    actionability: FindingActionability
    severity: FindingSeverity

    # Loop-ready lineage (inert until loop logic is wired)
    run_id: Optional[str] = None
    parent_finding_id: Optional[str] = None

    # Defaults
    snippet: str = ""
    confidence: float = 0.5
    discovered_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["source"] = self.source.value
        out["actionability"] = self.actionability.value
        out["severity"] = self.severity.value
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewFinding":
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        if isinstance(filtered.get("source"), str):
            filtered["source"] = FindingSource(filtered["source"])
        if isinstance(filtered.get("actionability"), str):
            filtered["actionability"] = FindingActionability(filtered["actionability"])
        if isinstance(filtered.get("severity"), str):
            filtered["severity"] = FindingSeverity(filtered["severity"])
        filtered.setdefault("run_id", None)
        filtered.setdefault("parent_finding_id", None)
        filtered.setdefault("snippet", "")
        filtered.setdefault("confidence", 0.5)
        filtered.setdefault("discovered_at", None)
        return cls(**filtered)


@dataclass
class ReviewSummary:
    """
    A summary of a completed (or aborted) review run.

    Provides a stable artifact for downstream consumers that want
    aggregate data without loading every individual ReviewFinding.
    """
    id: str  # QA-owned stable id
    run_id: str
    repo: str

    # Counts
    finding_count: int = 0
    actionable_count: int = 0
    critical_count: int = 0

    # Delta from baseline (inert until baseline tracking is wired)
    baseline_summary_id: Optional[str] = None
    delta_findings: int = 0
    delta_actionable: int = 0

    # Defaults
    generated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewSummary":
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        filtered.setdefault("finding_count", 0)
        filtered.setdefault("actionable_count", 0)
        filtered.setdefault("critical_count", 0)
        filtered.setdefault("baseline_summary_id", None)
        filtered.setdefault("delta_findings", 0)
        filtered.setdefault("delta_actionable", 0)
        filtered.setdefault("generated_at", None)
        return cls(**filtered)


@dataclass
class ReviewRun:
    """
    A single autonomous review run.

    Bounded by time or a specific PR cycle.  Carries lineage fields
    that are inert until the loop orchestration is wired.
    """
    id: str  # QA-owned stable id (use generate_id)

    # Identity
    repo: str
    pr_number: Optional[int] = None

    # Loop state
    status: ReviewRunStatus = ReviewRunStatus.PENDING
    loop_count: int = 0
    attempts_used: int = 0

    # Lineage (inert until loop logic is wired)
    parent_run_id: Optional[str] = None
    root_run_id: Optional[str] = None

    # Findings tracking
    finding_ids: List[str] = field(default_factory=list)
    summary_id: Optional[str] = None

    # Defaults
    mode: str = "autonomous-review"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["status"] = self.status.value
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewRun":
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        if isinstance(filtered.get("status"), str):
            filtered["status"] = ReviewRunStatus(filtered["status"])
        filtered.setdefault("pr_number", None)
        filtered.setdefault("loop_count", 0)
        filtered.setdefault("attempts_used", 0)
        filtered.setdefault("parent_run_id", None)
        filtered.setdefault("root_run_id", None)
        filtered.setdefault("finding_ids", [])
        filtered.setdefault("summary_id", None)
        filtered.setdefault("mode", "autonomous-review")
        filtered.setdefault("started_at", None)
        filtered.setdefault("ended_at", None)
        filtered.setdefault("error", None)
        return cls(**filtered)


@dataclass
class FeedbackEvent:
    """
    A single feedback event recorded during a review loop.

    Captures feedback from human reviewers, LLM reviewers, CI checks,
    or self-review to inform retry/exit decisions.
    """
    id: str  # QA-owned stable id (use generate_id)
    finding_id: str

    # Classification
    sentiment: FeedbackSentiment
    source: FeedbackSource

    # Context
    comment: str = ""
    loop_count: int = 0

    # Normalization helpers (informational — not used for routing yet)
    is_contradictory: bool = False
    is_conceptual: bool = False

    # Defaults
    recorded_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["sentiment"] = self.sentiment.value
        out["source"] = self.source.value
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeedbackEvent":
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        if isinstance(filtered.get("sentiment"), str):
            filtered["sentiment"] = FeedbackSentiment(filtered["sentiment"])
        if isinstance(filtered.get("source"), str):
            filtered["source"] = FeedbackSource(filtered["source"])
        filtered.setdefault("comment", "")
        filtered.setdefault("loop_count", 0)
        filtered.setdefault("is_contradictory", False)
        filtered.setdefault("is_conceptual", False)
        filtered.setdefault("recorded_at", None)
        return cls(**filtered)


@dataclass
class LearnedRule:
    """
    A learned rule derived from repeated feedback/findings during autonomous review.

    Learned rules are a conservative mechanism: they can suppress low-risk
    style/format/import-order patterns when the same finding occurs
    repeatedly across runs, but they NEVER override operator-authored rules
    and NEVER activate based on reaction-only signals.

    Lifecycle:
      tentative → active  (when evidence threshold met AND safety gates pass)
      tentative → rejected (when safety gates or conflicts fail)
      active → superseded (when operator-authored rule takes precedence)

    Attributes:
        rule_id:         QA-owned stable identifier (e.g. ``lr-{fp[:12]}-{n:03d}``).
        header:          Normalized rule/finding header this rule suppresses.
        pattern:         Normalized path pattern or glob this rule applies to.
        status:          Current LearnedRuleStatus.
        risk_level:      ``low`` for style/format/import-order only.
        precedence:      Lower number = higher priority.  Operator rules are
                         always precedence 0; learned rules start at 10.
        evidence_count:  Number of times this pattern was observed.
        proposal_run_id: Run ID that proposed this rule.
        activated_at:    ISO timestamp when status changed to ACTIVE (if ever).
        superseded_by:   rule_id of the rule that superseded this one (if any).
        created_at:      ISO timestamp of rule creation.
        updated_at:      ISO timestamp of last update.
        source_finding_ids: List of finding_ids that contributed to this rule.
        notes:           Human-readable context (how rule was derived).
    """
    rule_id: str
    header: str
    pattern: str
    status: LearnedRuleStatus

    # Classification
    risk_level: str = "low"          # "low" | "high" — high-risk never auto-activates
    precedence: int = 10              # 0 = operator-authored; 10+ = learned

    # Evidence
    evidence_count: int = 0
    source_finding_ids: List[str] = field(default_factory=list)

    # Lineage
    proposal_run_id: Optional[str] = None
    activated_at: Optional[str] = None
    superseded_by: Optional[str] = None

    # Timestamps
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    # Notes
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["status"] = self.status.value
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LearnedRule":
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        if isinstance(filtered.get("status"), str):
            filtered["status"] = LearnedRuleStatus(filtered["status"])
        filtered.setdefault("risk_level", "low")
        filtered.setdefault("precedence", 10)
        filtered.setdefault("evidence_count", 0)
        filtered.setdefault("source_finding_ids", [])
        filtered.setdefault("proposal_run_id", None)
        filtered.setdefault("activated_at", None)
        filtered.setdefault("superseded_by", None)
        filtered.setdefault("created_at", None)
        filtered.setdefault("updated_at", None)
        filtered.setdefault("notes", "")
        return cls(**filtered)
