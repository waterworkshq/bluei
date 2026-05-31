"""Tests for the SMTP email notifier."""

from unittest.mock import MagicMock, call, patch

import pytest

from bluei.engine.notifiers import NotificationPayload
from bluei.engine.notifiers.email import EmailNotifier


def _make_payload(**overrides):
    defaults = {
        "title": "merge failures detected",
        "body": "3 consecutive failures",
        "severity": "error",
        "escalation_type": "consecutive_merge_failures",
        "repo_name": "my-project",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "raw_findings": [
            {"type": "consecutive_merge_failures", "detail": "3 failures in a row"}
        ],
    }
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def _global_config(smtp_host="smtp.example.com", smtp_port=587, **extra):
    return {
        "to": ["oncall@example.com"],
        "_global": {
            "email": {
                "smtp_host": smtp_host,
                "smtp_port": smtp_port,
                "smtp_user": "user@example.com",
                "smtp_password": "pass123",
                "from_address": "bluei@example.com",
                **extra,
            },
        },
    }


def test_email_constructs_email_message(mock_smtp_server):
    notifier = EmailNotifier(_global_config())

    with patch(
        "bluei.engine.notifiers.email.smtplib.SMTP", return_value=mock_smtp_server
    ):
        result = notifier.send(_make_payload())

    assert result.success
    sent_msg = mock_smtp_server.send_message.call_args[0][0]
    assert "[bluei]" in sent_msg["Subject"]
    assert "oncall@example.com" in sent_msg["To"]
    assert "bluei@example.com" in sent_msg["From"]


def test_email_port_587_starttls(mock_smtp_server):
    notifier = EmailNotifier(_global_config(smtp_port=587))

    with patch(
        "bluei.engine.notifiers.email.smtplib.SMTP", return_value=mock_smtp_server
    ):
        notifier.send(_make_payload())

    mock_smtp_server.starttls.assert_called_once()
    mock_smtp_server.ehlo.assert_called()


def test_email_port_465_smtp_ssl(mock_smtp_server):
    notifier = EmailNotifier(_global_config(smtp_port=465))

    with patch(
        "bluei.engine.notifiers.email.smtplib.SMTP_SSL", return_value=mock_smtp_server
    ):
        result = notifier.send(_make_payload())

    assert result.success
    mock_smtp_server.starttls.assert_not_called()


def test_email_port_25_no_tls(mock_smtp_server):
    notifier = EmailNotifier(_global_config(smtp_port=25))

    with patch(
        "bluei.engine.notifiers.email.smtplib.SMTP", return_value=mock_smtp_server
    ):
        result = notifier.send(_make_payload())

    assert result.success
    mock_smtp_server.starttls.assert_not_called()


def test_email_no_smtp_falls_back_sendmail():
    notifier = EmailNotifier(
        {
            "to": ["oncall@example.com"],
            "_global": {"email": {"smtp_host": ""}},
        }
    )
    with patch("bluei.engine.notifiers.email.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = notifier.send(_make_payload())

    assert result.success
    mock_run.assert_called_once()
    args = mock_run.call_args
    assert args[0][0] == ["/usr/sbin/sendmail", "-t"]


def test_email_smtp_auth_failure(mock_smtp_server):
    import smtplib

    notifier = EmailNotifier(_global_config())
    mock_smtp_server.login.side_effect = smtplib.SMTPAuthenticationError(
        535, b"Auth failed"
    )

    with patch(
        "bluei.engine.notifiers.email.smtplib.SMTP", return_value=mock_smtp_server
    ):
        result = notifier.send(_make_payload())

    assert not result.success
    assert "auth failed" in result.error.lower()


def test_email_smtp_connect_error(mock_smtp_server):
    import smtplib

    notifier = EmailNotifier(_global_config())
    mock_smtp_server.ehlo.side_effect = smtplib.SMTPConnectError(
        421, b"Connection refused"
    )

    with patch(
        "bluei.engine.notifiers.email.smtplib.SMTP", return_value=mock_smtp_server
    ):
        result = notifier.send(_make_payload())

    assert not result.success


def test_email_body_includes_findings(mock_smtp_server):
    notifier = EmailNotifier(_global_config())

    with patch(
        "bluei.engine.notifiers.email.smtplib.SMTP", return_value=mock_smtp_server
    ):
        notifier.send(_make_payload())

    sent_msg = mock_smtp_server.send_message.call_args[0][0]
    body = sent_msg.get_content()
    assert "3 failures in a row" in body


def test_email_no_recipients():
    notifier = EmailNotifier({"to": [], "_global": {"email": {"smtp_host": "x"}}})
    result = notifier.send(_make_payload())
    assert not result.success
    assert "no recipients" in result.error


def test_email_subject_prefix(mock_smtp_server):
    notifier = EmailNotifier({**_global_config(), "subject_prefix": "[custom]"})

    with patch(
        "bluei.engine.notifiers.email.smtplib.SMTP", return_value=mock_smtp_server
    ):
        notifier.send(_make_payload())

    sent_msg = mock_smtp_server.send_message.call_args[0][0]
    assert "[custom]" in sent_msg["Subject"]


def test_email_recipients_refused(mock_smtp_server):
    import smtplib

    notifier = EmailNotifier(_global_config())
    mock_smtp_server.send_message.side_effect = smtplib.SMTPRecipientsRefused(
        {"bad@x": (550, b"bad address")}
    )

    with patch(
        "bluei.engine.notifiers.email.smtplib.SMTP", return_value=mock_smtp_server
    ):
        result = notifier.send(_make_payload())

    assert not result.success
    assert result.error is not None


def test_email_sendmail_nonzero_returncode():
    notifier = EmailNotifier(
        {
            "to": ["oncall@example.com"],
            "_global": {"email": {"smtp_host": ""}},
        }
    )
    with patch("bluei.engine.notifiers.email.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr=b"delivery failed")
        result = notifier.send(_make_payload())

    assert not result.success
    assert "delivery failed" in result.error


def test_email_sendmail_not_found():
    import subprocess

    notifier = EmailNotifier(
        {
            "to": ["oncall@example.com"],
            "_global": {"email": {"smtp_host": ""}},
        }
    )
    with patch(
        "bluei.engine.notifiers.email.subprocess.run",
        side_effect=FileNotFoundError("no sendmail"),
    ) as mock_run:
        result = notifier.send(_make_payload())

    assert not result.success
    assert "no sendmail" in result.error


def test_email_sendmail_timeout():
    import subprocess

    notifier = EmailNotifier(
        {
            "to": ["oncall@example.com"],
            "_global": {"email": {"smtp_host": ""}},
        }
    )
    with patch(
        "bluei.engine.notifiers.email.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="sendmail", timeout=30),
    ) as mock_run:
        result = notifier.send(_make_payload())

    assert not result.success
