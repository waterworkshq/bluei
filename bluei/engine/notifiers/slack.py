"""Slack incoming webhook notifier."""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

from bluei.engine.notifiers import (
    BaseNotifier,
    DeliveryResult,
    NotificationPayload,
    register_notifier,
    find_missing_env_vars,
    resolve_env_vars,
)

_MAX_TEXT_LEN = 2900
_MAX_BLOCKS = 50
_MAX_PAYLOAD_BYTES = 16000

SEVERITY_EMOJI = {"error": "\U0001f6a8", "warning": "\u26a0\ufe0f", "info": "\u2139\ufe0f"}


@register_notifier
class SlackNotifier(BaseNotifier):
    channel_type = "slack"

    def format_payload(self, payload: NotificationPayload) -> bytes:
        emoji = SEVERITY_EMOJI.get(payload.severity, "\u2139\ufe0f")
        blocks = self._build_blocks(payload, emoji)
        fallback_text = f"{emoji} bluei: {payload.title}"
        envelope = {
            "text": fallback_text[:3000],
            "blocks": blocks[:_MAX_BLOCKS],
        }
        raw = json.dumps(envelope, default=str).encode("utf-8")
        if len(raw) > _MAX_PAYLOAD_BYTES:
            envelope["blocks"] = envelope["blocks"][:3]
            raw = json.dumps(envelope, default=str).encode("utf-8")
        return raw

    def _build_blocks(self, payload: NotificationPayload, emoji: str) -> List[Dict]:
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} bluei: {payload.title}"[:150]},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": self._truncate(
                        f"*Repo:* {payload.repo_name}\n*Type:* {payload.escalation_type}\n{payload.body}"
                    ),
                },
            },
        ]
        if payload.raw_findings:
            details = []
            for f in payload.raw_findings[:5]:
                detail = f.get("detail", "")
                if detail:
                    details.append(f"\u2022 {detail[:200]}")
            if details:
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": self._truncate("\n".join(details))},
                })
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{payload.timestamp} | bluei"}],
        })
        return blocks

    def _truncate(self, text: str) -> str:
        if len(text) <= _MAX_TEXT_LEN:
            return text
        return text[:_MAX_TEXT_LEN - 3] + "..."

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
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        for attempt in range(3):
            start = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    latency = (time.monotonic() - start) * 1000
                    body = resp.read().decode("utf-8", errors="replace").strip()
                    if body == "ok" and resp.status == 200:
                        return DeliveryResult(
                            channel_type=self.channel_type,
                            success=True,
                            status_code=200,
                            latency_ms=latency,
                            error=http_warn,
                        )
                    return DeliveryResult(
                        channel_type=self.channel_type,
                        success=False,
                        status_code=resp.status,
                        error=body[:200],
                        latency_ms=latency,
                    )
            except urllib.error.HTTPError as e:
                latency = (time.monotonic() - start) * 1000
                body_text = e.read().decode("utf-8", errors="replace").strip()
                if e.code in (400, 403, 404, 410):
                    return DeliveryResult(
                        channel_type=self.channel_type,
                        success=False,
                        status_code=e.code,
                        error=body_text[:200],
                        latency_ms=latency,
                    )
                if attempt < 2:
                    time.sleep(1)
                    continue
                return DeliveryResult(
                    channel_type=self.channel_type,
                    success=False,
                    status_code=e.code,
                    error=body_text[:200],
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
