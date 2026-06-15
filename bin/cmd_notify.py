"""Notify command handler.

Extracted from bin/bluei.py to reduce its surface area."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

from bin.help_text import HELP_TEXT
from bin.cmd_utils import (
    has_flag,
    parse_option,
    parse_positive_int,
    parse_repo_arg,
)

# Backward-compat aliases
_parse_repo_arg = parse_repo_arg
_parse_option = parse_option
_has_flag = has_flag
_parse_positive_int = parse_positive_int


def _cmd_notify(rest: list[str]) -> int:
    if not rest or rest[0] in ("-h", "--help"):
        print(HELP_TEXT["notify"])
        return 0

    subcmd = rest[0]
    sub_rest = rest[1:]
    if subcmd not in {"test", "config", "digest", "log"}:
        print(
            f"bluei: unknown notify subcommand '{subcmd}'. Try 'bluei help notify'.",
            file=sys.stderr,
        )
        return 1

    if subcmd == "test":
        return _notify_test(sub_rest)
    if subcmd == "config":
        return _notify_config(sub_rest)
    if subcmd == "digest":
        return _notify_digest(sub_rest)
    if subcmd == "log":
        return _notify_log(sub_rest)
    return 0


def _notify_test(sub_rest: list[str]) -> int:
    repo = _parse_repo_arg(sub_rest)
    if not repo:
        print(
            "bluei: notify test requires --repo <name>. Try 'bluei help notify'.",
            file=sys.stderr,
        )
        return 1

    from bluei.engine.notify import deliver_escalations

    test_finding = [{"type": "test", "detail": "bluei notification test"}]
    results = deliver_escalations(test_finding, repo, bypass_rate_limit=True)

    if not results:
        print("No channels configured or notifications disabled.")
        return 0

    print(f"{'CHANNEL':<12} {'STATUS':<10} {'LATENCY':<12} ERROR")
    for r in results:
        status = "OK" if r.success else "FAIL"
        latency = f"{r.latency_ms:.0f}ms" if r.latency_ms else "-"
        error = r.error or ""
        print(f"{r.channel_type:<12} {status:<10} {latency:<12} {error}")

    if any(not r.success for r in results):
        return 1
    return 0


def _notify_init(sub_rest: list[str]) -> int:
    """Notif 1: Interactive wizard to scaffold notifications.yaml."""
    import yaml
    from pathlib import Path

    is_global = _has_flag(sub_rest, "--global")
    repo = _parse_repo_arg(sub_rest)

    if not is_global and not repo:
        print(
            "bluei: notify config --init requires --repo <name> or --global.",
            file=sys.stderr,
        )
        return 1

    print()
    print("bluei notification setup wizard")
    print("=" * 40)
    print()

    config: dict = {"enabled": True, "channels": []}

    # Slack
    print("Slack notifications (Block Kit via Incoming Webhook)")
    slack_url = input("  Slack webhook URL (or press Enter to skip): ").strip()
    if slack_url:
        config["channels"].append(
            {
                "type": "slack",
                "url": slack_url,
                "severity_filter": ["critical", "high"],
            }
        )
        print("  ✓ Slack configured")
    print()

    # Email
    print("Email notifications (SMTP)")
    smtp_host = input("  SMTP host (or press Enter to skip): ").strip()
    if smtp_host:
        smtp_port = input("  SMTP port [587]: ").strip() or "587"
        smtp_user = input("  SMTP username: ").strip()
        smtp_pass = input("  SMTP password (or ${ENV_VAR}): ").strip()
        smtp_to = input("  Recipient email: ").strip()
        config["channels"].append(
            {
                "type": "email",
                "host": smtp_host,
                "port": int(smtp_port) if smtp_port.isdigit() else 587,
                "username": smtp_user,
                "password": smtp_pass,
                "to": smtp_to,
                "severity_filter": ["critical", "high"],
            }
        )
        print("  ✓ Email configured")
    print()

    # Webhook
    print("Generic webhook notifications")
    webhook_url = input("  Webhook URL (or press Enter to skip): ").strip()
    if webhook_url:
        config["channels"].append(
            {
                "type": "webhook",
                "url": webhook_url,
                "severity_filter": ["critical", "high", "normal"],
            }
        )
        print("  ✓ Webhook configured")
    print()

    # Log channel (always enabled by default)
    config["channels"].append({"type": "log"})
    print("✓ Log channel (always enabled)")
    print()

    # Rate limiting
    print("Rate limiting")
    cooldown = input("  Cooldown per finding (hours) [24]: ").strip()
    hourly_cap = input("  Hourly cap per channel [10]: ").strip()
    config["rate_limiting"] = {
        "cooldown_hours": int(cooldown) if cooldown.isdigit() else 24,
        "hourly_cap": int(hourly_cap) if hourly_cap.isdigit() else 10,
    }

    # Determine output path
    if is_global:
        workspace = Path.cwd()
        config_path = workspace / "notifications.yaml"
    else:
        workspace = Path.cwd()
        config_path = workspace / "state" / repo / "notifications.yaml"

    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Confirm
    print()
    print(f"Will write to: {config_path}")
    preview = yaml.dump(config, default_flow_style=False).strip()
    print()
    print(preview)
    print()
    confirm = input("Write this configuration? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        print("Cancelled.")
        return 0

    config_path.write_text(preview + "\n")
    print(f"\n✓ Configuration written to {config_path}")
    print(
        f"  Test with: bluei notify test {'--global' if is_global else '--repo ' + repo}"
    )
    return 0


def _notify_config(sub_rest: list[str]) -> int:
    import yaml
    from bluei.engine.notify import load_notification_config, mask_sensitive

    # Notif 1: --init wizard
    if _has_flag(sub_rest, "--init"):
        return _notify_init(sub_rest)

    if _has_flag(sub_rest, "--global"):
        config = load_notification_config(None)
    else:
        repo = _parse_repo_arg(sub_rest)
        if not repo:
            print(
                "bluei: notify config requires --repo <name> or --global. Try 'bluei help notify'.",
                file=sys.stderr,
            )
            return 1
        config = load_notification_config(repo)

    display = dict(config)
    display.pop("_global", None)

    for ch in display.get("channels", []):
        if "url" in ch:
            ch["url"] = mask_sensitive(ch["url"])
        if "headers" in ch:
            for k in list(ch["headers"].keys()):
                ch["headers"][k] = mask_sensitive(str(ch["headers"][k]))

    for ch in display.get("digest", {}).get("channels", []):
        if "url" in ch:
            ch["url"] = mask_sensitive(ch["url"])

    print(yaml.dump(display, default_flow_style=False).strip())
    return 0


def _notify_digest(sub_rest: list[str]) -> int:
    from bluei.engine.notify import deliver_digest

    if _has_flag(sub_rest, "--all"):
        from bluei.app.config import ConfigManager

        cm = ConfigManager()
        repos = cm.list_repo_configs()
        any_sent = False
        for rc in repos:
            notif = rc.notifications or {}
            if notif.get("digest", {}).get("enabled"):
                results = deliver_digest(rc.name)
                sent = sum(1 for r in results if r.success)
                failed = sum(1 for r in results if not r.success)
                print(f"{rc.name}: {sent} delivered, {failed} failed")
                any_sent = True
        if not any_sent:
            print("No repos have digest.enabled=true")
        return 0

    repo = _parse_repo_arg(sub_rest)
    if not repo:
        print(
            "bluei: notify digest requires --repo <name> or --all. Try 'bluei help notify'.",
            file=sys.stderr,
        )
        return 1

    results = deliver_digest(repo)
    if not results:
        print("Digest not enabled or no channels configured.")
        return 0

    for r in results:
        status = "OK" if r.success else "FAIL"
        print(f"  {r.channel_type}: {status}")
    return 0 if all(r.success for r in results) else 1


def _notify_log(sub_rest: list[str]) -> int:
    repo = _parse_repo_arg(sub_rest)
    limit = _parse_positive_int(_parse_option(sub_rest, "--limit"), default=20)

    workspace = Path.cwd()
    log_path = workspace / "state" / "notification_log.jsonl"

    if not log_path.exists():
        print("No notification log found.")
        return 0

    try:
        lines = log_path.read_text().strip().splitlines()
    except OSError:
        print("Could not read notification log.", file=sys.stderr)
        return 1

    matching: list[dict] = []
    for line in lines[-200:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if repo and entry.get("repo") != repo:
            continue
        matching.append(entry)

    print(f"{'TIMESTAMP':<28} {'REPO':<20} {'SEVERITY':<10} {'TYPE':<35} CHANNELS")
    for entry in matching[-limit:]:
        ts = entry.get("timestamp", "?")[:27]
        repo_name = entry.get("repo", "?")[:19]
        severity = entry.get("severity", "?")[:9]
        esc_type = entry.get("escalation_type", "?")[:34]
        deliveries = entry.get("deliveries", [])
        channels = ", ".join(
            f"{d.get('channel_type', '?')}:{'OK' if d.get('success') else 'FAIL'}"
            for d in deliveries
        )[:50]
        print(f"{ts:<28} {repo_name:<20} {severity:<10} {esc_type:<35} {channels}")
    return 0
