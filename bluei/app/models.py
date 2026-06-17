#!/usr/bin/env python3
"""Data models for QA Agent — app layer.

Shared types/enums/dataclasses live in bluei/common/models.py and are
re-exported here for backward compatibility. New code should import
directly from bluei.common.models.

App-specific symbols (AgentStatus, HealthScore, Baseline, Run) remain
defined here because they are only used by app/ (runner, health,
dashboard).
"""

from dataclasses import dataclass, asdict
from enum import Enum
from typing import Any, Dict, Optional

# Backward-compat re-export — canonical home is bluei/common/models.py.
# migrated 2026-06-18 during H5 review→app decoupling.
from bluei.common.models import (  # noqa: F401
    ONBOARDING_VERSION,
    RepoStatus,
    SafetyMode,
    SafetyProfile,
    ReviewMode,
    LiveRolloutMode,
    FeedbackSentiment,
    FeedbackSource,
    ReviewRunStatus,
    LanguageInfo,
    RepoConfig,
    Repo,
    ReviewRun,
    FeedbackEvent,
    generate_id,
    now_iso,
)


class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


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
