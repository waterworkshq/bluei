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


ONBOARDING_VERSION = 2


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

    @classmethod
    def from_brand(cls, value: str) -> "SafetyMode":
        brand_map = {
            "watch-only": cls.OBSERVE,
            "watch only": cls.OBSERVE,
            "note-only": cls.ISSUE_ONLY,
            "note only": cls.ISSUE_ONLY,
            "offer-fixes": cls.PR,
            "offer fixes": cls.PR,
            "full-care": cls.MERGE,
            "full care": cls.MERGE,
        }
        result = brand_map.get(value)
        return result if result is not None else cls(value)

    @property
    def brand_label(self) -> str:
        return {
            self.OBSERVE: "watch only",
            self.ISSUE_ONLY: "note only",
            self.PR: "offer fixes",
            self.MERGE: "full care",
        }[self]


class SafetyProfile(str, Enum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class ReviewMode(str, Enum):
    OBSERVATION = "observation"
    AUTONOMOUS_REVIEW = "autonomous-review"
    REMEDIATION = "remediation"


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
    rule_pack: Optional[str] = None
    enabled: bool = True
    plugin_id: str = ""

    # Discovery options
    discovery: Dict[str, Any] = field(default_factory=dict)

    # Rules
    rules_enabled: Optional[List[str]] = None
    rules_disabled: List[str] = field(default_factory=list)

    # Fix options
    fix_engine: str = "deterministic"
    fallback_engines: List[str] = field(
        default_factory=lambda: ["claude", "opencode", "deterministic"]
    )
    claude_template: str = ""
    opencode_template: str = ""
    review_claude_template: str = ""
    review_opencode_template: str = ""

    # Validation
    baseline_checks: List[List[str]] = field(default_factory=list)

    # Limits
    limits: Dict[str, int] = field(
        default_factory=lambda: {
            "open_issues_cap": 20,
            "open_prs_cap": 5,
            "max_prs_per_run": 2,
            "max_issues_per_run": 10,
            "max_files_changed": 5,
            "max_loc_diff": 200,
            "max_fix_attempts": 3,
        }
    )

    # Cooldowns
    cooldowns: Dict[str, int] = field(
        default_factory=lambda: {
            "finding_seconds": 14400,
            "merge_minutes": 30,
            "staleness_seconds": 7200,
        }
    )

    # GitHub
    github: Dict[str, bool] = field(
        default_factory=lambda: {
            "live_actions": False,
            "auto_merge": False,
        }
    )

    # Review care
    review_care: Dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": True,
            "mode": ReviewMode.OBSERVATION.value,
            "provider_order": ["github"],
            "max_attempts": 3,
            "max_loops": 2,
            "max_prs_per_run": 1,
            "retry_delay_minutes": 15,
            "style_retry_threshold": 3,
            "allow_forks": False,
            "allow_unchanged_baseline_failures": True,
            "remediation_requires_validation": True,
            "conceptual_feedback_action": "pause",
            "contradictory_feedback_action": "pause",
            "cleanup_worktrees_after_push": True,
            # Phase G4: guarded live-review gate.
            # When True, enables backend generation and live GitHub publication.
            # Requires github.live_actions to also be True.
            # Default False (local-only mode) ensures safe testability on real repos.
            "guarded_live_review": False,
            # Phase G5: live rollout mode for autonomous-review progression.
            # local_only  - No backend when live_actions=True; safe local analysis only.
            # shadow       - Backend + targeting, but do NOT actually publish; record intent.
            # limited     - Full guarded live path (requires guarded_live_review + live_actions).
            "live_rollout_mode": LiveRolloutMode.LOCAL_ONLY.value,
            # Phase G7: monitored-rollout safety config.
            # Number of consecutive failures before circuit breaker opens.
            "monitored_failure_threshold": 3,
            # Cooldown duration in seconds after circuit opens.
            "monitored_cooldown_seconds": 300,
            # Optional fail-closed rollback based on recent feedback signals.
            "monitored_auto_rollback_enabled": False,
            "monitored_negative_feedback_threshold": 0.3,
            "monitored_feedback_min_events": 3,
            "monitored_feedback_window": 20,
        }
    )

    # Safety
    safety: Dict[str, Any] = field(
        default_factory=lambda: {
            "mode": SafetyMode.OBSERVE.value,
            "profile": SafetyProfile.CONSERVATIVE.value,
            "require_clean_worktree": True,
            "protected_branches": ["main", "master"],
            "allow_live_on_dirty_tree": False,
            "notes": [],
        }
    )

    # Metadata / template provenance
    meta: Dict[str, Any] = field(
        default_factory=lambda: {
            "onboarding_version": 1,
            "template": None,
            "inferred_by": "legacy",
        }
    )

    # Notifications
    notifications: Dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False,
            "channels": [],
            "digest": {"enabled": False, "schedule": "never", "channels": []},
            "rate_limit": {"cooldown_seconds": 300, "max_per_hour": 20},
        }
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RepoConfig":
        data = dict(data)
        if "safety" not in data or not data.get("safety"):
            github = data.get("github", {}) or {}
            inferred_mode = (
                SafetyMode.MERGE.value
                if github.get("live_actions")
                else SafetyMode.OBSERVE.value
            )
            inferred_profile = (
                SafetyProfile.BALANCED.value
                if github.get("live_actions")
                else SafetyProfile.CONSERVATIVE.value
            )
            data["safety"] = {
                "mode": inferred_mode,
                "profile": inferred_profile,
                "require_clean_worktree": True,
                "protected_branches": ["main", "master"],
                "allow_live_on_dirty_tree": False,
                "notes": ["safety policy inferred during config migration"],
            }
        if "meta" not in data or not data.get("meta"):
            data["meta"] = {
                "onboarding_version": 1,
                "template": None,
                "inferred_by": "migration",
            }
        if "review_care" not in data or not data.get("review_care"):
            data["review_care"] = {
                "enabled": True,
                "mode": ReviewMode.OBSERVATION.value,
                "provider_order": ["github"],
                "max_attempts": 3,
                "max_loops": 2,
                "max_prs_per_run": 1,
                "retry_delay_minutes": 15,
                "style_retry_threshold": 3,
                "allow_forks": False,
                "allow_unchanged_baseline_failures": True,
                "remediation_requires_validation": True,
                "conceptual_feedback_action": "pause",
                "contradictory_feedback_action": "pause",
                "cleanup_worktrees_after_push": True,
                "guarded_live_review": False,
                "live_rollout_mode": LiveRolloutMode.LOCAL_ONLY.value,
                "monitored_auto_rollback_enabled": False,
                "monitored_negative_feedback_threshold": 0.3,
                "monitored_feedback_min_events": 3,
                "monitored_feedback_window": 20,
            }
        else:
            review_care = dict(data.get("review_care") or {})
            review_care.setdefault("mode", ReviewMode.OBSERVATION.value)
            review_care.setdefault("cleanup_worktrees_after_push", True)
            review_care.setdefault("guarded_live_review", False)
            review_care.setdefault(
                "live_rollout_mode", LiveRolloutMode.LOCAL_ONLY.value
            )
            review_care.setdefault("monitored_auto_rollback_enabled", False)
            review_care.setdefault("monitored_negative_feedback_threshold", 0.3)
            review_care.setdefault("monitored_feedback_min_events", 3)
            review_care.setdefault("monitored_feedback_window", 20)
            data["review_care"] = review_care
        if "notifications" not in data or not data.get("notifications"):
            data["notifications"] = {
                "enabled": False,
                "channels": [],
                "digest": {"enabled": False, "schedule": "never", "channels": []},
                "rate_limit": {"cooldown_seconds": 300, "max_per_hour": 20},
            }
        return cls(**data)

    def validate(self) -> List[str]:
        """Validate config fields. Returns list of error messages (empty = valid)."""
        errors: List[str] = []

        # Required string fields
        for field_name in ("name", "id", "language"):
            value = getattr(self, field_name, "")
            if not value or not isinstance(value, str):
                errors.append(
                    f"{field_name}: must be a non-empty string (got {type(value).__name__}: {value!r})"
                )

        # Path field
        if not self.path or not isinstance(self.path, str):
            errors.append(
                f"path: must be a non-empty string (got {type(self.path).__name__}: {self.path!r})"
            )

        # Fix engine
        valid_engines = {"auto", "deterministic", "claude", "opencode"}
        if self.fix_engine and self.fix_engine not in valid_engines:
            errors.append(
                f"fix_engine: must be one of {valid_engines} (got {self.fix_engine!r})"
            )

        # Limits must be dict of ints
        if self.limits and isinstance(self.limits, dict):
            for k, v in self.limits.items():
                if not isinstance(v, int):
                    errors.append(
                        f"limits.{k}: must be an int (got {type(v).__name__}: {v!r})"
                    )

        # Cooldowns must be dict of ints
        if self.cooldowns and isinstance(self.cooldowns, dict):
            for k, v in self.cooldowns.items():
                if not isinstance(v, int):
                    errors.append(
                        f"cooldowns.{k}: must be an int (got {type(v).__name__}: {v!r})"
                    )

        # Safety mode must be valid (accept old and brand values)
        if self.safety and isinstance(self.safety, dict):
            mode = self.safety.get("mode", "")
            if (
                mode
                and mode not in {e.value for e in SafetyMode}
                and mode
                not in {
                    "watch-only",
                    "note-only",
                    "offer-fixes",
                    "full-care",
                }
            ):
                errors.append(f"safety.mode: invalid value {mode!r}")

        if self.notifications and isinstance(self.notifications, dict):
            for i, ch in enumerate(self.notifications.get("channels", [])):
                if not isinstance(ch, dict):
                    errors.append(f"notifications.channels[{i}]: must be a dict")
                    continue
                if "type" not in ch:
                    errors.append(f"notifications.channels[{i}]: missing 'type'")
                elif ch["type"] not in {"webhook", "slack", "email"}:
                    errors.append(
                        f"notifications.channels[{i}]: invalid type '{ch['type']}'"
                    )
                elif ch["type"] in ("webhook", "slack") and "url" not in ch:
                    errors.append(
                        f"notifications.channels[{i}]: {ch['type']} requires 'url'"
                    )
                elif ch["type"] == "email" and "to" not in ch:
                    errors.append(f"notifications.channels[{i}]: email requires 'to'")

        return errors

    @classmethod
    def from_yaml(cls, path: Path) -> "RepoConfig":
        import yaml

        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")
        with open(path) as f:
            data = yaml.safe_load(f)
        if data is None:
            raise ValueError(f"Empty or invalid YAML in {path}")
        return cls.from_dict(data)


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
            "critical": "red",
        }
        return colors.get(self.band, "gray")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "components": self.components,
            "calculated_at": self.calculated_at,
            "band": self.band,
            "color": self.color,
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
            "config": self.config.to_dict(),
            "status": self.status.value,
            "onboarded_at": self.onboarded_at,
            "last_run_at": self.last_run_at,
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "current_findings_count": self.current_findings_count,
            "current_health_score": self.current_health_score,
            "total_fixes": self.total_fixes,
            "total_prs": self.total_prs,
            "total_merges": self.total_merges,
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
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    unique = uuid.uuid4().hex[:16]
    return f"{prefix}-{ts}-{unique}" if prefix else f"{ts}-{unique}"


from bluei.engine.models import now_iso  # noqa: E402


# ---------------------------------------------------------------------------
# Autonomous-review models (Phase C1/C2)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Deterministic finding identity helpers (owned by QA-agent, not LLM input)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Autonomous review data models
# ---------------------------------------------------------------------------


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
