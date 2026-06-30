"""Enum and dataclass definitions shared across the campaign orchestration subsystem.

Campaigns progress through statuses (planning → active → completed/aborted/paused)
and are split into ordered phases executed either sequentially or in parallel.
"""

import enum
from dataclasses import dataclass, field
from typing import Any, Dict


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


class ObjectiveTarget(str, enum.Enum):
    """What a learning-objective campaign is gathering evidence for.

    The ``target_ref`` on a :class:`LearningObjective` is interpreted according
    to this value:

    - ``emergent_rule``: ``target_ref`` is an emergent rule_id (``er-...``) to
      advance via shadow validation.
    - ``pattern_family``: ``target_ref`` is a rule family (e.g. ``ruff-b``) to
      gather Dry Replay evidence for.
    - ``recipe``: ``target_ref`` is a recipe rule id whose matched assets are
      recorded in the learning report.
    """

    emergent_rule = "emergent_rule"
    pattern_family = "pattern_family"
    recipe = "recipe"


@dataclass
class LearningObjective:
    """A declared learning target for a campaign.

    When set on a :class:`~bluei.campaigns.planner.Campaign`, the executor
    routes per-finding outcomes to the matching native evidence layer
    (Dry Replay for pattern families; shadow-rule advancement + new-rule
    proposal for emergent rules). See DESIGN §2.1.
    """

    target_type: ObjectiveTarget
    target_ref: str
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict (``target_type`` as ``.value``)."""
        return {
            "target_type": self.target_type.value,
            "target_ref": self.target_ref,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LearningObjective":
        """Reconstruct a LearningObjective from a persisted dict.

        ``notes`` is backfilled for forward-compatibility with old records.
        """
        return cls(
            target_type=ObjectiveTarget(data["target_type"]),
            target_ref=data["target_ref"],
            notes=data.get("notes", "") or "",
        )
