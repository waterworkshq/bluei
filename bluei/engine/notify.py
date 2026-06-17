"""Notification dispatcher — loads config, manages rate limits, routes to channels."""

from __future__ import annotations

import logging
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

import yaml

from bluei.engine.notifiers import (
    BaseNotifier,
    DeliveryResult,
    NotificationPayload,
    CHANNEL_REGISTRY,
)
from bluei.engine.notifiers.log import LogNotifier
from bluei.engine.jsonl import read_jsonl


SEVERITY_MAP = {
    "consecutive_merge_failures": "error",
    "reappearing_finding": "warning",
    "dedup_saturation": "warning",
    "rebase_conflict_trend": "warning",
    "max_duplicate_prs": "error",
    "config_validation_failure": "warning",
    "cycle_escalation": "error",
}

_DEFAULT_SEVERITY = "warning"

_DEFAULT_NOTIFICATIONS = {
    "enabled": False,
    "channels": [],
    "digest": {"enabled": False, "schedule": "never", "channels": []},
    "rate_limit": {"cooldown_seconds": 300, "max_per_hour": 20},
}


def _deep_defaults():
    return {
        "enabled": False,
        "channels": [],
        "digest": {"enabled": False, "schedule": "never", "channels": []},
        "rate_limit": {"cooldown_seconds": 300, "max_per_hour": 20},
    }


def load_notification_config(
    repo_name: Optional[str],
    workspace: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load merged notification config: global yaml + per-repo config."""
    ws = (workspace or Path.cwd()).resolve()
    defaults = _deep_defaults()

    global_path = ws / "notifications.yaml"
    if global_path.exists():
        try:
            global_cfg = yaml.safe_load(global_path.read_text()) or {}
            for k in ("enabled", "channels", "digest", "rate_limit"):
                if k in global_cfg:
                    defaults[k] = global_cfg[k]
            defaults["_global"] = global_cfg
        except (yaml.YAMLError, OSError):
            _logger.debug("Failed to read global notification config")

    if repo_name:
        repo_cfg_path = ws / "repos" / repo_name / "config.yaml"
        if repo_cfg_path.exists():
            try:
                repo_cfg = yaml.safe_load(repo_cfg_path.read_text()) or {}
                repo_notif = repo_cfg.get("notifications")
                if repo_notif:
                    if "channels" in repo_notif:
                        defaults["channels"] = repo_notif["channels"]
                    if "digest" in repo_notif:
                        defaults["digest"] = repo_notif["digest"]
                    if "rate_limit" in repo_notif:
                        rl = dict(defaults.get("rate_limit", {}))
                        rl.update(repo_notif["rate_limit"])
                        defaults["rate_limit"] = rl
                    if "enabled" in repo_notif:
                        defaults["enabled"] = repo_notif["enabled"]
            except (yaml.YAMLError, OSError):
                _logger.debug("Failed to read repo notification config")

    return defaults


def _check_cooldown(
    config: Dict[str, Any],
    escalation_type: str,
    detail_hash: str,
    workspace: Path,
) -> bool:
    """Return True if this finding type+hash is not in cooldown."""
    rl = config.get("rate_limit", {})
    cooldown = int(rl.get("cooldown_seconds", 300))
    log_path = workspace / "state" / "notification_log.jsonl"

    if not log_path.exists():
        return True

    now = datetime.now(timezone.utc).timestamp()
    cooldown_cutoff = now - cooldown

    try:
        lines = log_path.read_text().strip().splitlines()
    except (OSError, UnicodeDecodeError):
        return True

    for line in lines[-500:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_str = entry.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str).timestamp()
        except (ValueError, TypeError):
            continue
        if ts >= cooldown_cutoff:
            if (
                entry.get("escalation_type") == escalation_type
                and entry.get("detail_hash") == detail_hash
            ):
                return False

    return True


def _check_hourly_cap(
    config: Dict[str, Any],
    channel_url: str,
    workspace: Path,
) -> bool:
    """Return True if this channel URL has not exceeded its hourly cap."""
    if not channel_url:
        return True
    max_per_hour = int(config.get("rate_limit", {}).get("max_per_hour", 20))
    log_path = workspace / "state" / "notification_log.jsonl"

    if not log_path.exists():
        return True

    hour_cutoff = datetime.now(timezone.utc).timestamp() - 3600

    try:
        lines = log_path.read_text().strip().splitlines()
    except (OSError, UnicodeDecodeError):
        return True

    count = 0
    for line in lines[-500:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_str = entry.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str).timestamp()
        except (ValueError, TypeError):
            continue
        if ts < hour_cutoff:
            continue
        for d in entry.get("deliveries", []):
            if d.get("channel_type") == "log":
                continue
            if not d.get("success"):
                continue
            if not channel_url or d.get("url") == channel_url:
                count += 1
                if count >= max_per_hour:
                    return False

    return True


def _detail_hash(detail: str) -> str:
    return hashlib.sha256(detail[:200].encode()).hexdigest()[:12]


def _instantiate_channels(
    config: Dict[str, Any],
    workspace: Path,
    channel_configs: Optional[List[Dict[str, Any]]] = None,
) -> List[BaseNotifier]:
    channels = []
    for ch_config in channel_configs or config.get("channels", []):
        ch_type = ch_config.get("type", "")
        cls = CHANNEL_REGISTRY.get(ch_type)
        if cls:
            cfg = dict(ch_config)
            cfg["_global"] = config.get("_global", {})
            channels.append(cls(cfg))
    return channels


def _build_title(finding: Dict[str, Any]) -> str:
    esc_type = finding.get("type", "unknown")
    detail = finding.get("detail", "")
    if detail:
        return detail[:120]
    return f"bluei escalation: {esc_type}"


def deliver_escalations(
    escalation_findings: List[Dict[str, Any]],
    repo_name: str,
    workspace: Optional[Path] = None,
    bypass_rate_limit: bool = False,
) -> List[DeliveryResult]:
    """Deliver escalation findings to configured notification channels."""
    ws = (workspace or Path.cwd()).resolve()
    config = load_notification_config(repo_name, ws)

    if not config.get("enabled"):
        return []

    channels = _instantiate_channels(config, ws)
    if not channels:
        return []

    results: List[DeliveryResult] = []
    for finding in escalation_findings:
        esc_type = finding.get("type", "unknown")
        detail = finding.get("detail", "")
        severity = SEVERITY_MAP.get(esc_type, _DEFAULT_SEVERITY)
        dh = _detail_hash(detail)

        if not bypass_rate_limit and not _check_cooldown(config, esc_type, dh, ws):
            continue

        payload = NotificationPayload(
            title=_build_title(finding),
            body=detail,
            severity=severity,
            escalation_type=esc_type,
            repo_name=repo_name,
            timestamp=datetime.now(timezone.utc).isoformat(),
            raw_findings=[finding],
            metadata={"detail_hash": dh},
        )

        all_delivery_results: List[Dict[str, Any]] = []
        for ch in channels:
            sev_filter = ch.config.get("severity_filter", [])
            if not ch.should_send(payload, sev_filter):
                continue
            ch_url = ch.config.get("url", "")
            if not bypass_rate_limit and not _check_hourly_cap(config, ch_url, ws):
                continue
            r = ch.send(payload)
            all_delivery_results.append(
                {
                    "channel_type": r.channel_type,
                    "success": r.success,
                    "status_code": r.status_code,
                    "latency_ms": round(r.latency_ms, 1),
                    "url": ch_url,
                }
            )
            results.append(r)

        log_notifier = LogNotifier(
            {"log_path": str(ws / "state" / "notification_log.jsonl")}
        )
        payload.metadata["deliveries"] = all_delivery_results
        log_notifier.send(payload)

    return results


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _generate_digest(state_dir: Path) -> str:
    """Generate a compact health digest from repo state files."""
    blocks: List[str] = []

    trend = read_jsonl(state_dir / "health_trend.jsonl", limit=50)
    if trend:
        repos: Dict[str, List[Dict[str, Any]]] = {}
        for r in trend:
            repo = r.get("repo", "unknown")
            repos.setdefault(repo, []).append(r)
        lines = []
        for repo, recs in sorted(repos.items()):
            latest = recs[-1]
            score = latest.get("vitality", "?")
            issues = latest.get("open_issues", 0)
            lines.append(f"{repo}: vitality={score} issues={issues}")
        if lines:
            blocks.append("\n".join(lines))

    review = read_jsonl(state_dir / "review_stats.jsonl", limit=1)
    if review:
        latest = review[-1]
        parts = []
        for key, label in [
            ("active_prs", "active"),
            ("blocked_prs", "blocked"),
            ("retry_failed", "retry-fail"),
            ("merge_ready", "merge-ready"),
        ]:
            val = latest.get(key, 0)
            if val > 0:
                parts.append(f"{label}={val}")
        if parts:
            blocks.append("review cycle: " + " ".join(parts))
        else:
            blocks.append("review cycle: idle")

    escalation = read_jsonl(state_dir / "escalation_log.jsonl", limit=20)
    active = []
    for rec in escalation:
        for finding in rec.get("findings", []):
            if finding.get("type") != "resolved":
                active.append(finding.get("detail", "?"))
    if active:
        lines = ["escalations:"]
        for detail in active[-5:]:
            lines.append(f"  - {detail[:120]}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) if blocks else "no state data available"


def deliver_digest(
    repo_name: str,
    workspace: Optional[Path] = None,
) -> List[DeliveryResult]:
    """Deliver a health digest for a repo."""
    ws = (workspace or Path.cwd()).resolve()
    config = load_notification_config(repo_name, ws)

    if not config.get("digest", {}).get("enabled"):
        return []

    digest_channels = config.get("digest", {}).get("channels")
    channels = _instantiate_channels(config, ws, channel_configs=digest_channels)
    if not channels:
        channels = _instantiate_channels(config, ws)

    if not channels:
        return []

    state_dir = ws / "repos" / repo_name / "state"
    digest_text = _generate_digest(state_dir)

    payload = NotificationPayload(
        title=f"Health digest: {repo_name}",
        body=digest_text,
        severity="info",
        escalation_type="health_digest",
        repo_name=repo_name,
        timestamp=datetime.now(timezone.utc).isoformat(),
        raw_findings=[],
        metadata={},
    )

    results = []
    for ch in channels:
        sev_filter = ch.config.get("severity_filter", [])
        if not ch.should_send(payload, sev_filter):
            continue
        r = ch.send(payload)
        results.append(r)

    log_notifier = LogNotifier(
        {"log_path": str(ws / "state" / "notification_log.jsonl")}
    )
    payload.metadata["deliveries"] = [
        {"channel_type": r.channel_type, "success": r.success} for r in results
    ]
    log_notifier.send(payload)

    return results


def mask_sensitive(value: str) -> str:
    """Mask sensitive values for display. Replaces content after :// or : with ***."""
    if "://" in value:
        prefix = value.split("://")[0] + "://"
        return prefix + "***"
    if ":" in value:
        prefix = value.rsplit(":", 1)[0]
        return prefix + ":***"
    if len(value) > 8:
        return value[:4] + "***"
    return "***"
