"""Enum and dataclass definitions shared across the campaign orchestration subsystem.

Campaigns progress through statuses (planning → active → completed/aborted/paused)
and are split into ordered phases executed either sequentially or in parallel.
"""

import enum


class CampaignStatus(str, enum.Enum):
    """Lifecycle states for a campaign."""
    planning = "planning"
    pending = "pending"
    active = "active"
    running = "running"
    completed = "completed"
    aborted = "aborted"
    paused = "paused"


class CampaignStrategy(str, enum.Enum):
    """Strategy used to group findings into phases."""
    rule_based = "rule_based"
    depth_first = "depth_first"
    dependency_ordered = "dependency_ordered"


class PhaseStatus(str, enum.Enum):
    """Lifecycle states for a single campaign phase."""
    pending = "pending"
    active = "active"
    completed = "completed"
    paused = "paused"


class PhaseExecutionMode(str, enum.Enum):
    """Whether a phase's findings are applied sequentially or in parallel."""
    sequential = "sequential"
    parallel = "parallel"
