"""SMTP email notifier with sendmail fallback."""

import smtplib
import ssl
import subprocess
import time
from email.message import EmailMessage
from typing import Any, Dict

from bluei.engine.notifiers import (
    BaseNotifier,
    DeliveryResult,
    NotificationPayload,
    register_notifier,
    resolve_env_vars,
)


@register_notifier
class EmailNotifier(BaseNotifier):
    channel_type = "email"

    def send(self, payload: NotificationPayload) -> DeliveryResult:
        global_config = self.config.get("_global", {})
        email_config = global_config.get("email", {})
        smtp_host = resolve_env_vars(str(email_config.get("smtp_host", "")))
        smtp_port = int(email_config.get("smtp_port", 587))
        smtp_user = resolve_env_vars(str(email_config.get("smtp_user", "")))
        smtp_password = resolve_env_vars(str(email_config.get("smtp_password", "")))
        from_address = resolve_env_vars(str(email_config.get("from_address", "bluei@localhost")))
        to_addresses = self.config.get("to", [])
        subject_prefix = self.config.get("subject_prefix", "[bluei]")

        if not to_addresses:
            return DeliveryResult(
                channel_type=self.channel_type,
                success=False,
                error="no recipients configured",
            )

        msg = EmailMessage()
        msg["Subject"] = f"{subject_prefix} {payload.escalation_type}: {payload.title}"
        msg["From"] = from_address
        msg["To"] = ", ".join(to_addresses)

        body_lines = [
            f"bluei Escalation: {payload.title}",
            f"Repo: {payload.repo_name}",
            f"Severity: {payload.severity}",
            f"Type: {payload.escalation_type}",
            f"Time: {payload.timestamp}",
            "",
            payload.body,
        ]
        if payload.raw_findings:
            body_lines.append("")
            body_lines.append("Findings:")
            for f in payload.raw_findings[:10]:
                body_lines.append(f"  - {f.get('detail', 'unknown')[:200]}")
        msg.set_content("\n".join(body_lines))

        if not smtp_host:
            return self._try_sendmail(msg)

        return self._try_smtp(msg, smtp_host, smtp_port, smtp_user, smtp_password)

    def _try_smtp(self, msg, host, port, user, password):
        start = time.monotonic()
        try:
            context = ssl.create_default_context()
            if port == 465:
                with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                    if user:
                        server.login(user, password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=30) as server:
                    server.ehlo()
                    if port != 25:
                        server.starttls(context=context)
                        server.ehlo()
                    if user:
                        server.login(user, password)
                    server.send_message(msg)
            latency = (time.monotonic() - start) * 1000
            return DeliveryResult(channel_type=self.channel_type, success=True, latency_ms=latency)
        except smtplib.SMTPAuthenticationError as e:
            latency = (time.monotonic() - start) * 1000
            return DeliveryResult(
                channel_type=self.channel_type,
                success=False,
                error=f"auth failed: {e}",
                latency_ms=latency,
            )
        except smtplib.SMTPRecipientsRefused as e:
            latency = (time.monotonic() - start) * 1000
            return DeliveryResult(
                channel_type=self.channel_type,
                success=False,
                error=str(e)[:200],
                latency_ms=latency,
            )
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, OSError) as e:
            latency = (time.monotonic() - start) * 1000
            return DeliveryResult(
                channel_type=self.channel_type,
                success=False,
                error=str(e)[:200],
                latency_ms=latency,
            )

    def _try_sendmail(self, msg):
        start = time.monotonic()
        try:
            result = subprocess.run(
                ["/usr/sbin/sendmail", "-t"],
                input=msg.as_bytes(),
                timeout=30,
                capture_output=True,
            )
            latency = (time.monotonic() - start) * 1000
            if result.returncode == 0:
                return DeliveryResult(channel_type=self.channel_type, success=True, latency_ms=latency)
            return DeliveryResult(
                channel_type=self.channel_type,
                success=False,
                error=result.stderr.decode("utf-8", errors="replace")[:200],
                latency_ms=latency,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            latency = (time.monotonic() - start) * 1000
            return DeliveryResult(
                channel_type=self.channel_type,
                success=False,
                error=str(e)[:200],
                latency_ms=latency,
            )
