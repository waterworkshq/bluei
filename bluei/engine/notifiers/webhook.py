"""Generic HTTP POST webhook notifier."""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict

from bluei.engine.notifiers import (
    BaseNotifier,
    DeliveryResult,
    NotificationPayload,
    register_notifier,
    find_missing_env_vars,
    resolve_env_vars,
)

_VERSION = "2.6.0"


@register_notifier
class WebhookNotifier(BaseNotifier):
    channel_type = "webhook"

    def format_payload(self, payload: NotificationPayload) -> bytes:
        envelope = {
            "source": "bluei",
            "version": _VERSION,
            "timestamp": payload.timestamp,
            "repo": payload.repo_name,
            "severity": payload.severity,
            "escalation_type": payload.escalation_type,
            "title": payload.title,
            "body": payload.body,
            "findings": payload.raw_findings,
        }
        return json.dumps(envelope, default=str).encode("utf-8")

    def send(self, payload: NotificationPayload) -> DeliveryResult:
        raw_url = self.config.get("url", "")
        missing = find_missing_env_vars(raw_url)
        if missing:
            return DeliveryResult(
                channel_type=self.channel_type,
                success=False,
                error=f"missing env var(s): {', '.join(missing)}",
            )
        url = resolve_env_vars(raw_url)
        if not url:
            return DeliveryResult(channel_type=self.channel_type, success=False, error="no url configured")

        if not url.startswith(("http://", "https://")):
            return DeliveryResult(
                channel_type=self.channel_type,
                success=False,
                error=f"invalid url: {url[:100]}",
            )

        http_warn = ""
        if url.startswith("http://"):
            http_warn = "http:// URL (not HTTPS)"

        data = self.format_payload(payload)
        headers = {"Content-Type": "application/json", "User-Agent": f"bluei/{_VERSION}"}
        for k, v in self.config.get("headers", {}).items():
            headers[k] = resolve_env_vars(str(v))

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        for attempt in range(3):
            start = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    latency = (time.monotonic() - start) * 1000
                    return DeliveryResult(
                        channel_type=self.channel_type,
                        success=True,
                        status_code=resp.status,
                        latency_ms=latency,
                        error=http_warn,
                    )
            except urllib.error.HTTPError as e:
                latency = (time.monotonic() - start) * 1000
                if 400 <= e.code < 500:
                    return DeliveryResult(
                        channel_type=self.channel_type,
                        success=False,
                        status_code=e.code,
                        error=e.read().decode("utf-8", errors="replace")[:200],
                        latency_ms=latency,
                    )
                if attempt < 2:
                    time.sleep(1)
                    continue
                return DeliveryResult(
                    channel_type=self.channel_type,
                    success=False,
                    status_code=e.code,
                    error=e.read().decode("utf-8", errors="replace")[:200],
                    latency_ms=latency,
                )
            except (urllib.error.URLError, OSError) as e:
                if attempt < 2:
                    time.sleep(1)
                    continue
                latency = (time.monotonic() - start) * 1000
                return DeliveryResult(
                    channel_type=self.channel_type,
                    success=False,
                    error=str(e)[:200],
                    latency_ms=latency,
                )
