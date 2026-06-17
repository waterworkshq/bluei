"""Always-on JSONL log notifier."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from bluei.engine.jsonl import append_jsonl
from bluei.engine.notifiers import (
    BaseNotifier,
    DeliveryResult,
    NotificationPayload,
    register_notifier,
)


@register_notifier
class LogNotifier(BaseNotifier):
    channel_type = "log"

    def send(self, payload: NotificationPayload) -> DeliveryResult:
        log_path = Path(self.config.get("log_path", "state/notification_log.jsonl"))
        deliveries = payload.metadata.get("deliveries", [])
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "repo": payload.repo_name,
            "severity": payload.severity,
            "escalation_type": payload.escalation_type,
            "detail_hash": payload.metadata.get("detail_hash", ""),
            "deliveries": deliveries,
        }
        try:
            append_jsonl(log_path, record, default=str)
            return DeliveryResult(channel_type=self.channel_type, success=True)
        except OSError as e:
            return DeliveryResult(
                channel_type=self.channel_type, success=False, error=str(e)[:200]
            )
