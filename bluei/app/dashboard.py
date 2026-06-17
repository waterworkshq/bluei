"""Read-only observability dashboard generation.

Agates per-repo state files (health trends, findings, campaigns, emergent rules,
escalations, etc.) into a single HTML or JSON dashboard.  All output is
sanitized for secrets before being written to disk.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml

from .models import now_iso
from bluei.engine.jsonl import read_jsonl


_SECRET_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"gho_[A-Za-z0-9]{36}"),
    re.compile(r"ghs_[A-Za-z0-9]{36}"),
    re.compile(r"ghu_[A-Za-z0-9]{36}"),
    re.compile(r"sk_[A-Za-z0-9]{48}"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(r"xox[bpas]-[A-Za-z0-9-]+"),
]


def _scan_for_secrets(html_text: str) -> str:
    """Redact known secret patterns (GitHub tokens, AWS keys, Slack tokens) from HTML output."""
    for pattern in _SECRET_PATTERNS:
        html_text = pattern.sub("[REDACTED]", html_text)
    return html_text


def build_dashboard_data(
    *,
    repos_dir: Path,
    repo_name: str | None = None,
    history_limit: int = 30,
) -> Dict[str, Any]:
    """Aggregate dashboard data for one or all onboarded repos.

    Args:
        repos_dir: Directory containing per-repo state directories.
        repo_name: Optional single repo to include; all repos if None.
        history_limit: Maximum health-history entries per repo.

    Returns:
        Dict with ``generated_at``, ``repo_count``, and ``repos`` list.
    """
    repos_dir = Path(repos_dir)
    repo_dirs = _repo_dirs(repos_dir, repo_name)
    repos = [
        _build_repo_summary(repo_dir, history_limit=history_limit)
        for repo_dir in repo_dirs
    ]
    repos.sort(key=lambda item: item["name"])
    return {
        "generated_at": now_iso(),
        "repo_count": len(repos),
        "repos": repos,
    }


def write_dashboard(
    *,
    repos_dir: Path,
    output_path: Path,
    output_format: str = "html",
    repo_name: str | None = None,
) -> Path:
    """Build and write the dashboard to disk in HTML or JSON format.

    HTML output is capped at 5 MB (history is truncated if needed) and
    scanned for leaked secrets before writing.

    Args:
        repos_dir: Directory containing per-repo state directories.
        output_path: Destination file path.
        output_format: ``html`` or ``json``.
        repo_name: Optional single repo; all repos if None.

    Returns:
        The Path of the written file.

    Raises:
        ValueError: If ``output_format`` is not ``html`` or ``json``.
    """
    data = build_dashboard_data(repos_dir=repos_dir, repo_name=repo_name)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        output.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return output
    if output_format != "html":
        raise ValueError(f"unsupported dashboard format: {output_format}")
    html_text = render_dashboard_html(data)
    # File size cap: truncate history if over 5 MB
    if len(html_text.encode("utf-8")) > 5 * 1024 * 1024:
        for repo in data.get("repos", []):
            repo["health_history"] = repo.get("health_history", [])[-10:]
        html_text = render_dashboard_html(data)
    # No-secrets scan before writing
    html_text = _scan_for_secrets(html_text)
    output.write_text(html_text, encoding="utf-8")
    return output


def render_dashboard_html(data: Dict[str, Any]) -> str:
    repos = data.get("repos", [])
    rows = "\n".join(_render_repo_row(repo) for repo in repos)
    cards = "\n".join(_render_repo_card(repo) for repo in repos)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>bluei dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #101114;
      --panel: #181b20;
      --panel-2: #20242b;
      --text: #f3f5f7;
      --muted: #a5adb8;
      --line: #323842;
      --accent: #5fd0b5;
      --warn: #f6c76f;
      --bad: #ef7c7c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header, main {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
    header {{ padding-bottom: 8px; }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 32px; }}
    h2 {{ font-size: 20px; margin: 28px 0 12px; }}
    h3 {{ font-size: 16px; margin-bottom: 8px; }}
    .muted {{ color: var(--muted); }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; background: var(--panel-2); }}
    .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }}
    .metric {{ display: flex; justify-content: space-between; gap: 12px; padding: 4px 0; }}
    .score {{ color: var(--accent); font-weight: 700; }}
    .down {{ color: var(--bad); }}
    .up {{ color: var(--accent); }}
    .sparkline {{ width: 100%; height: 42px; margin-top: 10px; }}
    .bar {{ height: 8px; background: var(--panel-2); border-radius: 999px; overflow: hidden; }}
    .bar span {{ display: block; height: 100%; background: var(--accent); }}
    code {{ color: var(--warn); }}
    .band-excellent {{ background-color: #d4edda; }}
    .band-good {{ background-color: #d1ecf1; }}
    .band-needs-work {{ background-color: #fff3cd; }}
    .band-poor {{ background-color: #f8d7da; }}
    .band-critical {{ background-color: #f5c6cb; }}
    .tab-bar {{ display: flex; gap: 2px; background: var(--panel-2); padding: 4px; border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 100; }}
    .tab-bar button {{ padding: 6px 16px; border: 1px solid var(--line); background: var(--panel); color: var(--text); cursor: pointer; border-radius: 3px 3px 0 0; }}
    .tab-bar button.active {{ background: var(--accent); color: var(--bg); border-bottom-color: var(--accent); }}
    .section-panel {{ display: none; }}
    .section-panel.active {{ display: block; }}
    h2.collapsible {{ cursor: pointer; user-select: none; }}
    h2.collapsible::after {{ content: ' ▾'; }}
    h2.collapsible.collapsed::after {{ content: ' ▸'; }}
    .sparkline-dot {{ fill: var(--accent); stroke: var(--bg); stroke-width: 1; }}
    .sparkline-alert {{ fill: var(--bad); stroke: var(--bg); stroke-width: 1; }}
    .vitality-table {{ margin-top: 8px; font-size: 12px; }}
    .vitality-table td {{ padding: 2px 6px; }}
  </style>
</head>
<body>
  <div class="tab-bar">
    <button class="active" onclick="showTab('all')">All</button>
    <button onclick="showTab('section-fleet')">Fleet</button>
    <button onclick="showTab('section-learning')">Learning</button>
    <button onclick="showTab('section-campaigns')">Campaigns</button>
    <button onclick="showTab('section-review')">Review</button>
    <button onclick="showTab('section-escalations')">Escalations</button>
    <button onclick="showTab('section-notifications')">Notifications</button>
    <button onclick="showTab('section-raw')">Raw State</button>
  </div>
  <header>
    <h1>bluei dashboard</h1>
    <p class="muted">Generated {html.escape(str(data.get("generated_at", "")))} from {len(repos)} repo(s).</p>
  </header>
  <main>
    <div class="section-panel active" id="section-fleet">
    <section>
      <h2 class="collapsible">Fleet Overview</h2>
      <table id="fleet-table">
        <thead><tr><th onclick="sortTable('fleet-table', 0)" data-sort="repo">Repo</th><th onclick="sortTable('fleet-table', 1)" data-sort="language">Language</th><th onclick="sortTable('fleet-table', 2)" data-sort="vitality">Vitality</th><th onclick="sortTable('fleet-table', 3)" data-sort="trend">Trend</th><th onclick="sortTable('fleet-table', 4)" data-sort="open-findings">Open Findings</th><th onclick="sortTable('fleet-table', 5)" data-sort="last-run">Last Run</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
    </div>
    <div class="section-panel active" id="section-learning">
    <section>
      <h2 class="collapsible">Learning Systems</h2>
      <div class="grid">{cards}</div>
    </section>
    </div>
    <div class="section-panel active" id="section-campaigns">
    <section>
      <h2 class="collapsible">Campaign Tracker</h2>
      <div class="grid">{"".join(_render_campaigns(repo) for repo in repos)}</div>
    </section>
    </div>
    <div class="section-panel active" id="section-review">
    <section>
      <h2 class="collapsible">Review Cycle Health</h2>
      <div class="grid">{"".join(_render_review_card(repo) for repo in repos)}</div>
    </section>
    </div>
    <div class="section-panel active" id="section-escalations">
    <section>
      <h2 class="collapsible">Escalation Feed</h2>
      {_render_escalation_feed(data)}
    </section>
    </div>
    <div class="section-panel active" id="section-notifications">
    <section>
      <h2 class="collapsible">Notification Delivery</h2>
      {_render_notification_feed(data)}
    </section>
    </div>
    <div class="section-panel active" id="section-raw">
    <section>
      <h2 class="collapsible">Raw State Explorer</h2>
      <div class="grid">{"".join(_render_raw_state_card(repo) for repo in repos)}</div>
    </section>
    </div>
   </main>
  <script>
  function sortTable(tableId, colIndex) {{
    var table = document.getElementById(tableId);
    var rows = Array.from(table.rows).slice(1);
    var asc = table.getAttribute('data-sort-dir') !== 'asc';
    table.setAttribute('data-sort-dir', asc ? 'asc' : 'desc');
    rows.sort(function(a, b) {{
      var aVal = a.cells[colIndex].textContent.trim();
      var bVal = b.cells[colIndex].textContent.trim();
      var aNum = parseFloat(aVal);
      var bNum = parseFloat(bVal);
      if (!isNaN(aNum) && !isNaN(bNum)) return asc ? aNum - bNum : bNum - aNum;
      return asc ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
    }});
    rows.forEach(function(row) {{ table.appendChild(row); }});
  }}
  function showTab(tabId) {{
    document.querySelectorAll('.section-panel').forEach(function(el) {{ el.classList.remove('active'); }});
    document.querySelectorAll('.tab-bar button').forEach(function(el) {{ el.classList.remove('active'); }});
    if (tabId === 'all') {{
      document.querySelectorAll('.section-panel').forEach(function(el) {{ el.classList.add('active'); }});
    }} else {{
      document.getElementById(tabId).classList.add('active');
    }}
    event.target.classList.add('active');
  }}
  document.querySelectorAll('h2.collapsible').forEach(function(h2) {{
    h2.addEventListener('click', function() {{
      h2.classList.toggle('collapsed');
      var content = h2.nextElementSibling;
      while (content && content.tagName !== 'H2') {{
        content.style.display = h2.classList.contains('collapsed') ? 'none' : '';
        content = content.nextElementSibling;
      }}
    }});
  }});
  </script>
</body>
</html>
"""


def _build_repo_summary(repo_dir: Path, *, history_limit: int) -> Dict[str, Any]:
    """Collect all state for a single repo into a summary dict.

    Args:
        repo_dir: Path to a repo's workspace directory (contains ``state/``).
        history_limit: Max health-history entries to include.

    Returns:
        Dict with name, vitality, trend, findings count, component scores,
        health history, and subsections for learning, campaigns, reviews,
        escalations, and raw state.
    """
    state_dir = repo_dir / "state"
    config = _read_yaml(repo_dir / "config.yaml")
    health_history = read_jsonl(state_dir / "health_trend.jsonl", dicts_only=True)[
        -history_limit:
    ]
    latest_health = health_history[-1] if health_history else {}
    first_health = health_history[0] if health_history else {}
    current = _number(
        latest_health.get("health") or latest_health.get("score"), default=0
    )
    trend = current - _number(
        first_health.get("health") or first_health.get("score"), default=current
    )
    findings = read_jsonl(state_dir / "findings.jsonl", dicts_only=True)
    return {
        "name": repo_dir.name,
        "language": config.get("language", "unknown"),
        "current_vitality": current,
        "trend": trend,
        "last_run_at": latest_health.get("timestamp")
        or latest_health.get("generated_at"),
        "open_findings": len(findings),
        "component_scores": latest_health.get("components", {}),
        "health_history": health_history,
        "fix_patterns": _summarize_fix_patterns(state_dir / "fix_patterns.jsonl"),
        "emergent_rules": _summarize_emergent_rules(state_dir / "emergent_rules.json"),
        "campaigns": _summarize_campaigns(state_dir / "campaigns"),
        "escalations": {
            "recent": read_jsonl(state_dir / "escalation_log.jsonl", dicts_only=True)[
                -100:
            ]
        },
        "review_metrics": _summarize_review_metrics(state_dir / "review_stats.jsonl"),
        "auto_tune": _summarize_auto_tune(state_dir / "auto_tune.json"),
        "cycle_signals": _summarize_cycle_signals(state_dir / "cycle_signals.json"),
        "rebase_stats": _summarize_rebase_stats(state_dir / "rebase_stats.jsonl"),
        "notifications": _summarize_notifications(
            repo_dir.parent.parent / "state" / "notification_log.jsonl"
        ),
        "raw_state": _summarize_raw_state(state_dir),
    }


def _summarize_fix_patterns(path: Path) -> Dict[str, Any]:
    """Summarize fix patterns: active count and top-10 by success/confidence.

    Args:
        path: Path to ``fix_patterns.jsonl``.

    Returns:
        Dict with ``active_count`` and ``top`` list of pattern summaries.
    """
    patterns = read_jsonl(path, dicts_only=True)
    active = [
        item for item in patterns if _number(item.get("confidence"), default=0) >= 0.3
    ]
    top = sorted(
        active,
        key=lambda item: (
            _number(item.get("success_count"), default=0),
            _number(item.get("confidence"), default=0),
        ),
        reverse=True,
    )[:10]
    return {
        "active_count": len(active),
        "top": [
            {
                "pattern_id": item.get("pattern_id", ""),
                "rule": item.get("rule", ""),
                "confidence": _number(item.get("confidence"), default=0),
                "success_count": int(_number(item.get("success_count"), default=0)),
            }
            for item in top
        ],
    }


def _summarize_emergent_rules(path: Path) -> Dict[str, Any]:
    """Summarize emergent rules: status counts and worst false-positive rate.

    Args:
        path: Path to ``emergent_rules.json``.

    Returns:
        Dict with ``counts`` (status → count) and ``top_false_positive_rate``.
    """
    payload = _read_json(path)
    rules = payload.get("rules", []) if isinstance(payload, dict) else []
    counts = dict(Counter(str(rule.get("status", "unknown")) for rule in rules))
    fprs = [_number(rule.get("false_positive_rate"), default=0) for rule in rules]
    return {
        "counts": counts,
        "top_false_positive_rate": max(fprs) if fprs else 0,
    }


def _summarize_campaigns(campaigns_dir: Path) -> Dict[str, Any]:
    """Summarize campaign statuses from ``campaigns/*/campaign.json`` files.

    Args:
        campaigns_dir: Directory containing per-campaign subdirectories.

    Returns:
        Dict with ``active_count`` and up to 10 ``items`` with progress data.
    """
    items: List[Dict[str, Any]] = []
    if campaigns_dir.exists():
        for campaign_file in sorted(campaigns_dir.glob("*/campaign.json")):
            data = _read_json(campaign_file)
            if not data:
                continue
            total = _number(
                data.get("total_findings") or len(data.get("target_findings", [])),
                default=0,
            )
            fixed = _number(data.get("findings_fixed"), default=0)
            items.append(
                {
                    "campaign_id": data.get("campaign_id", campaign_file.parent.name),
                    "title": data.get("title", campaign_file.parent.name),
                    "status": data.get("status", "unknown"),
                    "progress": (fixed / total) if total else 0,
                    "total_findings": int(total),
                    "findings_fixed": int(fixed),
                }
            )
    active = [item for item in items if item["status"] not in {"completed", "aborted"}]
    return {"active_count": len(active), "items": items[:10]}


def _summarize_review_metrics(path: Path) -> Dict[str, Any]:
    """Aggregate review-cycle metrics from ``review_stats.jsonl``.

    Args:
        path: Path to ``review_stats.jsonl``.

    Returns:
        Dict with ``runs``, ``findings_detected``, ``findings_published``,
        ``retry_failures``, and ``publication_rate``.
    """
    rows = read_jsonl(path, dicts_only=True)
    detected = sum(_number(row.get("findings_detected"), default=0) for row in rows)
    published = sum(_number(row.get("findings_published"), default=0) for row in rows)
    retry_failures = sum(_number(row.get("retry_failures"), default=0) for row in rows)
    return {
        "runs": len(rows),
        "findings_detected": int(detected),
        "findings_published": int(published),
        "retry_failures": int(retry_failures),
        "publication_rate": (published / detected) if detected else 0,
    }


def _summarize_auto_tune(path: Path) -> Dict[str, Any]:
    """Summarize auto-tune configuration state.

    Args:
        path: Path to ``auto_tune.json``.

    Returns:
        Dict with ``mode``, ``override_count``, ``suggested_overrides``, and
        ``last_success_at``.
    """
    payload = _read_json(path)
    overrides = payload.get("suggested_overrides") or payload.get("overrides") or {}
    if not isinstance(overrides, dict):
        overrides = {}
    return {
        "mode": payload.get("mode") or payload.get("status") or "unknown",
        "override_count": len(overrides),
        "suggested_overrides": overrides,
        "last_success_at": payload.get("last_success_at"),
    }


def _summarize_cycle_signals(path: Path) -> Dict[str, Any]:
    """Summarize suppressed rules from cycle signal data.

    Args:
        path: Path to ``cycle_signals.json``.

    Returns:
        Dict with ``suppressed_count`` and ``suppressed_rules`` list.
    """
    payload = _read_json(path)
    suppressed = payload.get("suppressed_rules") or payload.get("suppressions") or {}
    if isinstance(suppressed, dict):
        rules = sorted(str(rule) for rule in suppressed.keys())
    elif isinstance(suppressed, list):
        rules = sorted(
            str(item.get("rule") if isinstance(item, dict) else item)
            for item in suppressed
        )
    else:
        rules = []
    return {
        "suppressed_count": len([rule for rule in rules if rule]),
        "suppressed_rules": [rule for rule in rules if rule],
    }


def _summarize_rebase_stats(path: Path) -> Dict[str, Any]:
    """Aggregate rebase statistics from ``rebase_stats.jsonl``.

    Args:
        path: Path to ``rebase_stats.jsonl``.

    Returns:
        Dict with ``attempted``, ``succeeded``, ``failed``, and ``success_rate``.
    """
    rows = read_jsonl(path, dicts_only=True)
    attempted = sum(_number(row.get("rebases_attempted"), default=0) for row in rows)
    succeeded = sum(_number(row.get("rebases_succeeded"), default=0) for row in rows)
    failed = sum(_number(row.get("rebases_failed"), default=0) for row in rows)
    return {
        "attempted": int(attempted),
        "succeeded": int(succeeded),
        "failed": int(failed),
        "success_rate": (succeeded / attempted) if attempted else 0,
    }


def _summarize_notifications(path: Path) -> Dict[str, Any]:
    """Aggregate notification delivery stats from ``notification_log.jsonl``.

    Args:
        path: Path to ``notification_log.jsonl``.

    Returns:
        Dict with ``total``, ``delivered``, ``failed``, ``channels``,
        ``last_delivery_at``, and ``recent`` entries.
    """
    rows = read_jsonl(path, dicts_only=True)
    if not rows:
        return {
            "total": 0,
            "delivered": 0,
            "failed": 0,
            "channels": [],
            "last_delivery_at": None,
            "recent": [],
        }
    total = 0
    delivered = 0
    failed = 0
    channel_set: set = set()
    for row in rows[-200:]:
        deliveries = row.get("deliveries", [])
        if not deliveries:
            continue
        total += 1
        has_success = any(
            d.get("success") for d in deliveries if d.get("channel_type") != "log"
        )
        if has_success:
            delivered += 1
        else:
            failed += 1
        for d in deliveries:
            ct = d.get("channel_type", "")
            if ct and ct != "log":
                channel_set.add(ct)
    last_ts = rows[-1].get("timestamp") if rows else None
    return {
        "total": total,
        "delivered": delivered,
        "failed": failed,
        "channels": sorted(channel_set),
        "last_delivery_at": last_ts,
        "recent": rows[-20:],
    }


def _summarize_raw_state(state_dir: Path) -> Dict[str, Any]:
    """Enumerate JSON/JSONL state files with their entry counts and sizes.

    Args:
        state_dir: Repository state directory.

    Returns:
        Dict with ``files`` mapping filenames to ``entries`` and ``bytes``.
    """
    files: Dict[str, Dict[str, Any]] = {}
    if not state_dir.exists():
        return {"files": files}
    for path in sorted(state_dir.iterdir()):
        if not path.is_file() or path.suffix not in {".json", ".jsonl"}:
            continue
        relative = path.name
        if path.suffix == ".jsonl":
            entries = len(read_jsonl(path, dicts_only=True))
        else:
            entries = 1 if _read_json(path) else 0
        files[relative] = {
            "entries": entries,
            "bytes": path.stat().st_size,
        }
    return {"files": files}


def _health_band(score: float) -> str:
    """Return a CSS class name representing a health score band.

    Args:
        score: Health score (0–100).

    Returns:
        One of ``band-excellent``, ``band-good``, ``band-needs-work``,
        ``band-poor``, or ``band-critical``.
    """
    if score >= 90:
        return "band-excellent"
    if score >= 75:
        return "band-good"
    if score >= 50:
        return "band-needs-work"
    if score >= 25:
        return "band-poor"
    return "band-critical"


def _render_repo_row(repo: Dict[str, Any]) -> str:
    """Render a single fleet-table row for a repo."""
    trend = _number(repo.get("trend"), default=0)
    trend_class = "up" if trend >= 0 else "down"
    vitality = _number(repo.get("current_vitality"), default=0)
    band_class = _health_band(vitality)
    return (
        "<tr>"
        f"<td><strong>{html.escape(str(repo.get('name', '')))}</strong></td>"
        f"<td>{html.escape(str(repo.get('language', 'unknown')))}</td>"
        f'<td class="score {band_class}">{_fmt(vitality)}</td>'
        f'<td class="{trend_class}">{_fmt(trend, signed=True)}</td>'
        f"<td>{int(_number(repo.get('open_findings'), default=0))}</td>"
        f"<td>{html.escape(str(repo.get('last_run_at') or 'never'))}</td>"
        "</tr>"
    )


def _render_repo_card(repo: Dict[str, Any]) -> str:
    """Render the learning-systems card for a repo (sparkline, patterns, rules)."""
    patterns = repo.get("fix_patterns", {})
    emergent = repo.get("emergent_rules", {})
    top_patterns = patterns.get("top", [])
    top = top_patterns[0] if top_patterns else {}
    history = repo.get("health_history", [])
    return f"""<article class="card">
  <h3>{html.escape(str(repo.get("name", "")))}</h3>
  {_render_sparkline(history)}
  {_render_vitality_table(history)}
  <div class="metric"><span>Fix patterns</span><strong>{patterns.get("active_count", 0)}</strong></div>
  <div class="metric"><span>Top pattern</span><code>{html.escape(str(top.get("rule", "none")))}</code></div>
  <div class="metric"><span>Emergent rules</span><strong>{html.escape(json.dumps(emergent.get("counts", {}), sort_keys=True))}</strong></div>
</article>"""


def _render_campaigns(repo: Dict[str, Any]) -> str:
    """Render campaign cards with progress bars for a repo."""
    items = repo.get("campaigns", {}).get("items", [])
    if not items:
        return f"""<article class="card"><h3>{html.escape(str(repo.get("name", "")))}</h3><p class="muted">No campaigns recorded.</p></article>"""
    rendered = []
    for item in items:
        progress = max(0, min(1, _number(item.get("progress"), default=0)))
        rendered.append(
            f"""<article class="card">
  <h3>{html.escape(str(item.get("title", "")))}</h3>
  <div class="metric"><span>{html.escape(str(item.get("campaign_id", "")))}</span><strong>{html.escape(str(item.get("status", "")))}</strong></div>
  <div class="bar"><span style="width: {progress * 100:.0f}%"></span></div>
</article>"""
        )
    return "".join(rendered)


def _render_review_card(repo: Dict[str, Any]) -> str:
    """Render the review-cycle health card for a repo."""
    review = repo.get("review_metrics", {})
    tune = repo.get("auto_tune", {})
    signals = repo.get("cycle_signals", {})
    rebase = repo.get("rebase_stats", {})
    return f"""<article class="card">
  <h3>{html.escape(str(repo.get("name", "")))}</h3>
  <div class="metric"><span>Review runs</span><strong>{int(_number(review.get("runs"), default=0))}</strong></div>
  <div class="metric"><span>Publication rate</span><strong>{_percent(review.get("publication_rate"))}</strong></div>
  <div class="metric"><span>Retry failures</span><strong>{int(_number(review.get("retry_failures"), default=0))}</strong></div>
  <div class="metric"><span>Auto-tune</span><code>{html.escape(str(tune.get("mode", "unknown")))}</code></div>
  <div class="metric"><span>Suppressed rules</span><strong>{int(_number(signals.get("suppressed_count"), default=0))}</strong></div>
  <div class="metric"><span>Rebase success</span><strong>{_percent(rebase.get("success_rate"))}</strong></div>
</article>"""


def _render_escalation_feed(data: Dict[str, Any]) -> str:
    """Render the aggregated escalation feed as an HTML table."""
    all_escalations: List[Dict[str, Any]] = []
    for repo in data.get("repos", []):
        for esc in repo.get("escalations", {}).get("recent", []):
            all_escalations.append({"repo": repo.get("name", ""), **esc})
    if not all_escalations:
        return '<p class="muted">No recent escalations.</p>'
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(esc.get('repo', '')))}</td>"
        f"<td>{html.escape(str(esc.get('reason') or esc.get('type') or ''))}</td>"
        f"<td>{_severity_badge(esc.get('severity', ''))}</td>"
        f"<td>{html.escape(str(esc.get('timestamp') or ''))}</td>"
        f"<td>{html.escape(str(esc.get('finding_id') or ''))}</td>"
        "</tr>"
        for esc in all_escalations
    )
    return (
        "<table>"
        "<thead><tr><th>Repo</th><th>Reason</th><th>Severity</th><th>Timestamp</th><th>Finding</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def _severity_badge(severity: str) -> str:
    """Render a color-coded severity badge span."""
    sev_lower = str(severity).lower()
    if sev_lower in ("critical", "error"):
        css_class = "band-critical"
    elif sev_lower in ("high", "warning"):
        css_class = "band-needs-work"
    elif sev_lower in ("medium", "info"):
        css_class = "band-good"
    else:
        css_class = "band-good"
    return f'<span class="{css_class}" style="padding:2px 8px;border-radius:3px;font-size:11px;font-weight:600">{html.escape(str(severity or "info"))}</span>'


def _render_notification_feed(data: Dict[str, Any]) -> str:
    """Render the notification delivery feed as an HTML table."""
    all_entries: List[Dict[str, Any]] = []
    for repo in data.get("repos", []):
        notif = repo.get("notifications", {})
        for entry in notif.get("recent", []):
            all_entries.append({"repo": repo.get("name", ""), **entry})
    if not all_entries:
        return '<p class="muted">No notification deliveries recorded.</p>'
    rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(e.get('repo', '')))}</td>"
        f"<td>{_severity_badge(e.get('severity', ''))}</td>"
        f"<td>{html.escape(str(e.get('escalation_type', '')))}</td>"
        f"<td>{html.escape(str(e.get('timestamp', ''))[:19])}</td>"
        f"<td>{html.escape(', '.join(d.get('channel_type', '?') + (':' + ('ok' if d.get('success') else 'fail')) for d in e.get('deliveries', []) if d.get('channel_type') != 'log') or '—')}</td>"
        "</tr>"
        for e in all_entries
    )
    return (
        "<table>"
        "<thead><tr><th>Repo</th><th>Severity</th><th>Type</th><th>Time</th><th>Channels</th></tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table>"
    )


def _render_raw_state_card(repo: Dict[str, Any]) -> str:
    """Render a card listing raw state files and their entry counts."""
    files = repo.get("raw_state", {}).get("files", {})
    if not files:
        body = '<p class="muted">No JSON state files found.</p>'
    else:
        body = "".join(
            f'<div class="metric"><code>{html.escape(name)}</code><span>{int(_number(meta.get("entries"), default=0))} entries</span></div>'
            for name, meta in sorted(files.items())
        )
    return f"""<article class="card">
  <h3>{html.escape(str(repo.get("name", "")))}</h3>
  {body}
</article>"""


def _render_sparkline(history: List[Dict[str, Any]]) -> str:
    """Render an SVG sparkline of health scores over time."""
    values = [
        _number(item.get("health") or item.get("score"), default=0) for item in history
    ]
    if len(values) < 2:
        return '<svg class="sparkline" role="img" aria-label="No health trend"></svg>'
    width = 240
    height = 42
    step = width / max(1, len(values) - 1)
    points = []
    circles = []
    for index, value in enumerate(values):
        x = index * step
        y = height - ((max(0, min(100, value)) / 100) * height)
        points.append(f"{x:.1f},{y:.1f}")
        prev = values[index - 1] if index > 0 else value
        cls = "sparkline-alert" if value < prev else "sparkline-dot"
        circles.append(f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="3"/>')
    return f'<svg class="sparkline" viewBox="0 0 {width} {height}" role="img" aria-label="Health trend"><polyline fill="none" stroke="#5fd0b5" stroke-width="3" points="{" ".join(points)}"/>{"".join(circles)}</svg>'


def _render_vitality_table(history: List[Dict[str, Any]]) -> str:
    """Render a small table of the last 5 health scores with timestamps."""
    recent = history[-5:]
    if not recent:
        return ""
    rows = []
    for entry in reversed(recent):
        score = _number(entry.get("health") or entry.get("score"), default=0)
        ts = html.escape(str(entry.get("timestamp", ""))[:19])
        color = (
            "var(--bad)"
            if score < 50
            else "var(--warn)"
            if score < 75
            else "var(--accent)"
        )
        rows.append(
            f'<tr><td style="color:{color}">{_fmt(score)}</td><td>{ts}</td></tr>'
        )
    return f'<table class="vitality-table"><thead><tr><th>Score</th><th>Time</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'


def _repo_dirs(repos_dir: Path, repo_name: str | None) -> List[Path]:
    if repo_name:
        target = repos_dir / repo_name
        return [target] if target.exists() else []
    if not repos_dir.exists():
        return []
    return [path for path in repos_dir.iterdir() if path.is_dir()]


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, dict) else {}


def _number(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt(value: Any, *, signed: bool = False) -> str:
    number = _number(value, default=0)
    prefix = "+" if signed and number > 0 else ""
    return f"{prefix}{number:.0f}"


def _percent(value: Any) -> str:
    return f"{_number(value, default=0) * 100:.0f}%"
