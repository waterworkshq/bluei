"""Notification channel base classes and registry."""

import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass
class NotificationPayload:
    title: str
    body: str
    severity: str
    escalation_type: str
    repo_name: str
    timestamp: str
    raw_findings: List[Dict[str, Any]]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DeliveryResult:
    channel_type: str
    success: bool
    status_code: Optional[int] = None
    error: Optional[str] = None
    latency_ms: float = 0.0


class BaseNotifier(ABC):
    channel_type: str = "base"

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.logger = logging.getLogger(f"bluei.notify.{self.channel_type}")

    @abstractmethod
    def send(self, payload: NotificationPayload) -> DeliveryResult: ...

    def should_send(
        self, payload: NotificationPayload, severity_filter: List[str]
    ) -> bool:
        if not severity_filter:
            return True
        return payload.severity in severity_filter


CHANNEL_REGISTRY: Dict[str, type] = {}


def register_notifier(cls):
    CHANNEL_REGISTRY[cls.channel_type] = cls
    return cls


def resolve_env_vars(value: str) -> str:
    """Replace ${VAR} patterns with os.environ values."""

    def _replace(match):
        return os.environ.get(match.group(1), match.group(0))

    return _ENV_VAR_RE.sub(_replace, value)


def find_missing_env_vars(value: str) -> List[str]:
    """Return names of env vars referenced in value that are not set."""
    missing = []
    for match in _ENV_VAR_RE.finditer(value):
        var_name = match.group(1)
        if var_name not in os.environ:
            missing.append(var_name)
    return missing
