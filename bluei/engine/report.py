#!/usr/bin/env python3
"""Report data extraction for QA Agent.

Extracts data from status.json, issues.json, findings.jsonl,
health_history.jsonl, state.json reconciliation_events, and runs
into a canonical intermediate JSON format for dashboard consumption.
"""

import logging
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bluei.engine.jsonl import read_jsonl

_logger = logging.getLogger(__name__)


# ── Category inference for rules not in the detector catalog ──

_RULE_CATEGORY_PATTERNS = [
    (re.compile(r"^mdl-", re.I), "style"),
    (re.compile(r"^ruff-"), "lint"),
    (re.compile(r"^test-gap|^test-coverage|^test-"), "test"),
    (re.compile(r"^secret-"), "secret"),
    (re.compile(r"^shellcheck-", re.I), "lint"),
    (re.compile(r"^hadolint-", re.I), "lint"),
    (re.compile(r"^actionlint-", re.I), "lint"),
    (re.compile(r"^go-staticcheck-", re.I), "lint"),
    (re.compile(r"^go-unused"), "dead-code"),
    (re.compile(r"^debt-|^todo"), "debt"),
    (re.compile(r"^doc-|^docs-"), "docs"),
    (re.compile(r"^perf-"), "performance"),
    (re.compile(r"^type-"), "type-safety"),
    (re.compile(r"^refactor|^xo-complex|^xo-max"), "refactor"),
    (re.compile(r"^xo-"), "lint"),
    (re.compile(r"^discount-|^orders-|^inventory-|^notifications-|^catalog-"), "bug"),
    (re.compile(r"^broad-except"), "lint"),
    (re.compile(r"^hardcoded-"), "lint"),
    (re.compile(r"^trailing-"), "lint"),
]

_RULE_LANGUAGE_PATTERNS = [
    (re.compile(r"^mdl-", re.I), "markdown"),
    (re.compile(r"^ruff-"), "python"),
    (re.compile(r"^shellcheck-", re.I), "shell"),
    (re.compile(r"^hadolint-", re.I), "dockerfile"),
    (re.compile(r"^actionlint-", re.I), "github-actions"),
    (re.compile(r"^go-staticcheck|^go-unused", re.I), "go"),
    (re.compile(r"^secret-"), "generic"),
    (re.compile(r"^test-gap|^test-coverage"), "python"),
    (re.compile(r"^xo-"), "typescript"),
]


def _infer_category(rule: str, fallback: str = "other") -> str:
    """Infer a broad category from a rule name."""
    for pattern, cat in _RULE_CATEGORY_PATTERNS:
        if pattern.search(rule):
            return cat
    return fallback


def _infer_language(rule: str, fallback: str = "unknown") -> str:
    """Infer language from a rule name."""
    for pattern, lang in _RULE_LANGUAGE_PATTERNS:
        if pattern.search(rule):
            return lang
    return fallback


_PATH_LANGUAGE_SUFFIXES = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    # Aligned with codebase convention (48+ refs); was "typescript" pre-2026-06-17
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
}


def infer_language_from_path(path: str, fallback: str = "all") -> str:
    """Infer a language from a file path's suffix.

    Bridges the file-path-based language inference previously defined
    inline in ``bluei.app.emergent_rules``. Maps Python/TypeScript/Go/Rust
    suffixes to their canonical language label; anything else falls back
    to ``"all"`` (the historical emergent-rules default).
    """
    suffix = Path(path).suffix.lower()
    return _PATH_LANGUAGE_SUFFIXES.get(suffix, fallback)


def _normalize_category(cat: str) -> str:
    """Map various category names to the canonical set:
    bug, lint, style, security, secret, test, docs, performance,
    type-safety, refactor, debt, other."""
    cat_lower = cat.lower().replace(" ", "-").replace("_", "-")
    # Map common variants
    variant_map = {
        "todo/debt": "debt",
        "docs-mismatch": "docs",
        "docs-gap": "docs",
        "docs-drift": "docs",
        "perf-smell": "performance",
        "test-gap": "test",
        "test-coverage": "test",
        "type-safety": "type-safety",
        "dead-code": "refactor",
        "simplify": "refactor",
    }
    return variant_map.get(cat_lower, cat_lower)


def _build_rule_catalog(status_data: dict) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Build rule → category and rule → language mappings from the detector_catalog."""
    rule_category: Dict[str, str] = {}
    rule_language: Dict[str, str] = {}
    for entry in status_data.get("detector_catalog", []):
        rule = entry.get("rule", "")
        if rule:
            cat = entry.get("category", "")
            if cat:
                rule_category[rule] = _normalize_category(cat)
            lang = entry.get("language", "")
            if lang:
                rule_language[rule] = lang
    return rule_category, rule_language


def _classify_findings(findings: List[Dict], rule_category: Dict[str, str]) -> Counter:
    """Classify findings into canonical categories."""
    categories: Counter = Counter()
    for finding in findings:
        rule = finding.get("rule", "")
        cat = rule_category.get(rule)
        if cat:
            categories[cat] += 1
        else:
            categories[_infer_category(rule)] += 1
    return categories


def _build_findings_by_language(
    findings: List[Dict], rule_language: Dict[str, str]
) -> Counter:
    """Classify findings by language."""
    langs: Counter = Counter()
    for finding in findings:
        rule = finding.get("rule", "")
        lang = rule_language.get(rule)
        if lang:
            langs[lang] += 1
        else:
            langs[_infer_language(rule)] += 1
    return langs


def _build_top_rules(
    findings: List[Dict], rule_category: Dict[str, str], limit: int = 15
) -> List[Dict]:
    """Get top rules by frequency with severity."""
    rule_counts: Counter = Counter()
    rule_categories: Dict[str, str] = {}
    for finding in findings:
        rule = finding.get("rule", "")
        if rule:
            rule_counts[rule] += 1
            if rule not in rule_categories:
                cat = rule_category.get(rule)
                if not cat:
                    cat = _infer_category(rule)
                rule_categories[rule] = cat

    results = []
    for rule, count in rule_counts.most_common(limit):
        cat = rule_categories.get(rule, "other")
        # Infer severity from category
        if cat in ("security", "secret", "bug"):
            severity = "high"
        elif cat in ("performance", "type-safety", "test"):
            severity = "medium"
        else:
            severity = "low"
        results.append(
            {
                "rule": rule,
                "count": count,
                "category": cat,
                "severity": severity,
            }
        )
    return results


def _build_health_trend(
    state_dir: Path, health_history: List[Dict], days: int = 30
) -> List[Dict]:
    """Build health trend with daily aggregation."""
    if not health_history:
        return []

    # Health history already has timestamps and scores
    trend = []
    for entry in health_history:
        ts = entry.get("timestamp", "")
        score = entry.get("score", 0)
        findings_count = entry.get("findings_count", 0)
        # Format date from ISO timestamp
        date_str = ts[:10] if ts else "unknown"
        trend.append(
            {
                "date": date_str,
                "score": round(score, 1),
                "findings_count": findings_count,
            }
        )

    # Limit to last N entries
    return trend[-days:]


def _collect_run_metrics(runs_dir: Path) -> Dict[str, Any]:
    """Aggregate metrics from run files."""
    if not runs_dir.exists():
        return {"fix_attempts": 0, "fixes_verified": 0, "total_prs": 0, "runs": []}

    runs = []
    total_fix_attempts = 0
    total_fixes_verified = 0
    total_prs = 0
    for run_file in sorted(runs_dir.glob("*.json"), reverse=True):
        if run_file.name.endswith(".lock"):
            continue
        try:
            with open(run_file) as f:
                run = json.load(f)
            runs.append(
                {
                    "id": run.get("id", ""),
                    "phase": run.get("phase", ""),
                    "started_at": run.get("started_at", ""),
                    "ended_at": run.get("ended_at", ""),
                    "status": run.get("status", ""),
                    "findings_detected": run.get("findings_detected", 0),
                    "issues_created": run.get("issues_created", 0),
                    "fix_attempts": run.get("fix_attempts", 0),
                    "fixes_verified": run.get("fixes_verified", 0),
                    "prs_created": run.get("prs_created", 0),
                    "health_before": run.get("health_before"),
                    "health_after": run.get("health_after"),
                    "health_delta": run.get("health_delta"),
                    "dry_run": run.get("dry_run", True),
                }
            )
            total_fix_attempts += run.get("fix_attempts", 0)
            total_fixes_verified += run.get("fixes_verified", 0)
            total_prs += run.get("prs_created", 0)
        except (json.JSONDecodeError, OSError):
            continue

    return {
        "fix_attempts": total_fix_attempts,
        "fixes_verified": total_fixes_verified,
        "total_prs": total_prs,
        "runs": runs,
    }


def _load_reconciliation_events(state_data: dict) -> List[Dict]:
    """Extract reconciliation events from state.json."""
    events = state_data.get("reconciliation_events", [])
    return [
        {
            "timestamp": e.get("timestamp", ""),
            "reason": e.get("reason", ""),
            "before_open_issues": (e.get("before", {}) or {}).get("open_issues", 0),
            "after_open_issues": (e.get("after", {}) or {}).get("open_issues", 0),
            "before_open_prs": (e.get("before", {}) or {}).get("open_prs", 0),
            "after_open_prs": (e.get("after", {}) or {}).get("open_prs", 0),
        }
        for e in events
    ]


def extract_report_data(
    repo_path: str,
    repo_name: str,
    state_dir: Optional[Path] = None,
    repos_dir: Optional[Path] = None,
    days: int = 30,
) -> Dict[str, Any]:
    """Extract all report data for a repository.

    Args:
        repo_path: Path to the repository on disk.
        repo_name: Name of the repository in the registry.
        state_dir: Path to the state directory (state files).
        repos_dir: Path to the repos directory (contains runs/).
        days: Number of days of history to include.

    Returns:
        Canonical JSON data model for the report dashboard.
    """
    # Resolve state directory
    if state_dir is None and repos_dir is not None:
        state_dir = Path(repos_dir) / repo_name / "state"
    elif state_dir is not None:
        state_dir = Path(state_dir)
    else:
        raise ValueError("Either state_dir or repos_dir must be provided")

    runs_dir = state_dir.parent / "runs"

    # ── Load raw data files ──

    # status.json
    status_file = state_dir / "status.json"
    status_data: Dict = {}
    if status_file.exists():
        try:
            with open(status_file) as f:
                status_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            _logger.debug("Failed to read status JSON")

    # issues.json
    issues_file = state_dir / "issues.json"
    issues_data: Dict = {"issues": []}
    if issues_file.exists():
        try:
            with open(issues_file) as f:
                issues_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            _logger.debug("Failed to read issues JSON")

    # findings.jsonl
    findings_file = state_dir / "findings.jsonl"
    findings: List[Dict] = read_jsonl(findings_file)

    # health_history.jsonl
    history_file = state_dir / "health_history.jsonl"
    health_history: List[Dict] = read_jsonl(history_file)

    # state.json (for reconciliation_events)
    state_file = state_dir / "state.json"
    state_data: Dict = {}
    if state_file.exists():
        try:
            with open(state_file) as f:
                state_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            _logger.debug("Failed to read state JSON")

    # ── Build mappings ──

    rule_category, rule_language = _build_rule_catalog(status_data)

    # ── Extract counts ──

    current_counts = status_data.get("current_counts", {})
    total_findings = len(findings)
    open_issues = current_counts.get("open_issues", 0)
    open_prs = current_counts.get("open_prs", 0)

    # Get health score from latest health history entry
    last_health_score = status_data.get("health", {}).get("score", 0)
    if not last_health_score and health_history:
        last_health_score = health_history[-1].get("score", 0)
    if not last_health_score:
        last_health_score = current_counts.get("health_score", 0)

    # ── Build categories ──

    findings_by_category = _classify_findings(findings, rule_category)
    findings_by_language = _build_findings_by_language(findings, rule_language)
    top_rules = _build_top_rules(findings, rule_category)

    # Ensure canonical categories exist
    canonical_categories = [
        "bug",
        "lint",
        "style",
        "security",
        "secret",
        "test",
        "docs",
        "performance",
        "type-safety",
        "refactor",
        "debt",
        "other",
    ]
    for cat in canonical_categories:
        if cat not in findings_by_category:
            findings_by_category[cat] = 0

    # ── Health trend ──

    health_trend = _build_health_trend(state_dir, health_history, days)

    # ── Run metrics ──

    run_metrics = _collect_run_metrics(runs_dir)

    # ── Reconciliation events ──

    reconciliation_events = _load_reconciliation_events(state_data)

    # ── Last scan timestamp ──

    last_scan = status_data.get("last_run_at", "")
    if not last_scan:
        last_scan = state_data.get("last_run_at", "")

    # ── Repo info ──

    repo_path_str = str(repo_path) if repo_path else ""
    repo_url = None
    # Try to get URL from config or status
    config_file = state_dir.parent / "config.yaml"
    if config_file.exists():
        try:
            import yaml

            with open(config_file) as f:
                config = yaml.safe_load(f)
            repo_url = config.get("url", None)
        except Exception:
            _logger.debug("Failed to read config yaml for repo URL")

    # ── Assemble result ──

    return {
        "repo": {
            "name": repo_name,
            "path": repo_path_str,
            "health_score": round(float(last_health_score), 1)
            if last_health_score
            else 0,
            "last_scan": last_scan,
            "language": status_data.get("language", "unknown"),
            "url": repo_url,
        },
        "summary": {
            "total_findings": total_findings,
            "open_issues": open_issues,
            "open_prs": open_prs,
            "fix_attempts": run_metrics["fix_attempts"],
            "fixes_verified": run_metrics["fixes_verified"],
            "total_prs": run_metrics["total_prs"],
            "unresolved_issues": open_issues - run_metrics["total_prs"]
            if run_metrics["total_prs"] > 0
            else open_issues,
        },
        "health_trend": health_trend,
        "findings_by_category": dict(findings_by_category),
        "top_rules": top_rules,
        "findings_by_language": dict(findings_by_language),
        "reconciliation_events": reconciliation_events,
        "recent_runs": run_metrics["runs"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ═══ Template-Bound Data Format ═══════════════════════════════


def _to_template_format(data: Dict[str, Any]) -> Dict[str, Any]:
    """Map the canonical extraction data to the dashboard template format."""
    raw = data["findings_by_category"]
    tmpl_cats = {
        "bug": raw.get("bug", 0),
        "lint": raw.get("lint", 0),
        "style": raw.get("style", 0),
        "security": raw.get("security", 0),
        "secret": raw.get("secret", 0),
        "dead-code": raw.get("dead-code", 0) + raw.get("debt", 0),
        "simplify": raw.get("simplify", 0),
        "refactor": raw.get("refactor", 0)
        + raw.get("performance", 0)
        + raw.get("type-safety", 0),
        "other": raw.get("other", 0) + raw.get("test", 0) + raw.get("docs", 0),
    }

    # Map top_rules fields
    rules = []
    for r in data["top_rules"]:
        rules.append(
            {
                "rule": r["rule"],
                "category": r.get("category", "other"),
                "count": r["count"],
                "language": r.get("language", "—"),
            }
        )

    return {
        "repo": {
            "name": data["repo"]["name"],
            "path": data["repo"]["path"],
            "language": data["repo"]["language"],
            "safety_mode": "watch-only"
            if data["repo"].get("health_score", 0) < 30
            else "active",
        },
        "health_score": data["repo"]["health_score"],
        "counts": {
            "total_findings": data["summary"]["total_findings"],
            "open_issues": data["summary"]["open_issues"],
            "open_prs": data["summary"]["open_prs"],
            "findings_fixed": data["summary"]["fixes_verified"],
        },
        "findings_by_category": tmpl_cats,
        "health_trend": [
            {"date": p["date"], "score": p["score"]} for p in data["health_trend"]
        ],
        "top_rules": rules[:20],
        "language_distribution": data["findings_by_language"],
        "generated_at": data["generated_at"],
    }


def generate_report_html(
    data: Dict[str, Any], template_path: Optional[Path] = None
) -> str:
    """Generate an HTML report using the dashboard template.

    Args:
        data: Canonical extraction data from extract_report_data().
        template_path: Path to the dashboard template HTML file.
                       Defaults to bluei_report_template.html in the bluei dir.

    Returns:
        Fully rendered HTML report string.
    """
    # Resolve template path
    if template_path is None:
        script_dir = Path(__file__).resolve().parents[2]
        template_path = script_dir / "bluei_report_template.html"
    else:
        template_path = Path(template_path)

    if not template_path.exists():
        # Fallback to placeholder if template not found
        return _generate_placeholder_html(data)

    # Map data to template format
    tmpl_data = _to_template_format(data)

    # Read template
    template_html = template_path.read_text(encoding="utf-8")

    # Find and replace the DATA object
    # The template has: const DATA = { ... };
    marker_start = "const DATA = {"
    marker_end = ";\n\n// ═══ Render ═══"

    start_idx = template_html.find(marker_start)
    end_idx = template_html.find(marker_end)

    if start_idx == -1 or end_idx == -1:
        # Fallback
        return _generate_placeholder_html(data)

    # Find end of the actual DATA object (after the closing brace and before the semicolon)
    # We need to find the matching closing brace of the DATA object
    brace_start = template_html.index("{", start_idx)
    depth = 0
    end_pos = brace_start
    for i in range(brace_start, len(template_html)):
        if template_html[i] == "{":
            depth += 1
        elif template_html[i] == "}":
            depth -= 1
            if depth == 0:
                end_pos = i + 1
                break

    if end_pos == brace_start:
        return _generate_placeholder_html(data)

    # Generate new DATA block with real data
    json_str = json.dumps(tmpl_data, indent=2)
    new_data_block = f"const DATA = {json_str};"

    # Rebuild HTML
    before = template_html[:start_idx]
    after = template_html[end_pos + 1 :]  # skip the ';' after the closing brace
    # after should start with the next line
    after = after.lstrip("; \n")
    if after.startswith("// ═══ Render ═══"):
        rendered = before + new_data_block + "\n\n" + after
    else:
        rendered = before + new_data_block + "\n" + after

    return rendered


def _generate_placeholder_html(data: Dict[str, Any]) -> str:
    """Fallback placeholder HTML when the dashboard template is not available."""
    repo = data["repo"]
    summary = data["summary"]

    # Build category bars
    cat_bars = ""
    canonical_order = [
        "bug",
        "lint",
        "style",
        "security",
        "secret",
        "test",
        "docs",
        "performance",
        "type-safety",
        "refactor",
        "debt",
        "other",
    ]
    total_findings = summary["total_findings"] or 1
    for cat in canonical_order:
        count = data["findings_by_category"].get(cat, 0)
        pct = round(count / total_findings * 100, 1) if total_findings > 0 else 0
        cat_bars += (
            f'<div class="cat-row">'
            f'  <span class="cat-label">{cat}</span>'
            f'  <div class="cat-bar-bg">'
            f'    <div class="cat-bar" style="width:{pct}%"></div>'
            f"  </div>"
            f'  <span class="cat-count">{count}</span>'
            f"</div>\n"
        )

    # Build top rules table
    rules_rows = ""
    for rule in data["top_rules"][:10]:
        sev_color = {"high": "#e74c3c", "medium": "#f39c12", "low": "#3498db"}
        color = sev_color.get(rule["severity"], "#95a5a6")
        rules_rows += (
            f"<tr>"
            f"  <td><code>{rule['rule']}</code></td>"
            f"  <td>{rule['count']}</td>"
            f'  <td><span class="sev-badge" style="background:{color}">{rule["severity"]}</span></td>'
            f"</tr>\n"
        )

    # Build health trend table
    trend_rows = ""
    for entry in data["health_trend"][-10:]:
        score = entry["score"]
        color = "#2ecc71" if score >= 70 else ("#f39c12" if score >= 50 else "#e74c3c")
        trend_rows += (
            f"<tr>"
            f"  <td>{entry['date']}</td>"
            f'  <td><span style="color:{color};font-weight:bold">{score}</span></td>'
            f"  <td>{entry.get('findings_count', '—')}</td>"
            f"</tr>\n"
        )

    # Health score band and color
    health = repo["health_score"]
    if health >= 70:
        health_band = "Good"
        health_color = "#2ecc71"
    elif health >= 50:
        health_band = "Needs Work"
        health_color = "#f39c12"
    elif health >= 30:
        health_band = "Poor"
        health_color = "#e67e22"
    else:
        health_band = "Critical"
        health_color = "#e74c3c"

    html = (
        f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QA Report: {repo["name"]}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1923; color: #e0e0e0; padding: 20px; }}
  .container {{ max-width: 900px; margin: 0 auto; }}
  h1 {{ color: #00b4d8; font-size: 1.8em; margin-bottom: 5px; }}
  .subtitle {{ color: #7f8c8d; font-size: 0.9em; margin-bottom: 20px; }}
  .health-card {{ background: linear-gradient(135deg, #1a2a3a, #0f1923); border: 1px solid #2c3e50; border-radius: 12px; padding: 24px; margin-bottom: 20px; }}
   .vitality {{ font-size: 3em; font-weight: bold; }}
  .health-band {{ font-size: 1.1em; margin-left: 10px; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin: 20px 0; }}
  .stat-card {{ background: #1a2a3a; border: 1px solid #2c3e50; border-radius: 8px; padding: 16px; text-align: center; }}
  .stat-value {{ font-size: 1.6em; font-weight: bold; color: #00b4d8; }}
  .stat-label {{ font-size: 0.8em; color: #7f8c8d; margin-top: 4px; text-transform: uppercase; }}
  h2 {{ color: #00b4d8; font-size: 1.3em; margin: 24px 0 12px; border-bottom: 1px solid #2c3e50; padding-bottom: 6px; }}
  .cat-row {{ display: flex; align-items: center; margin: 6px 0; gap: 10px; }}
  .cat-label {{ width: 100px; font-size: 0.85em; color: #b0b0b0; text-transform: capitalize; }}
  .cat-bar-bg {{ flex: 1; height: 18px; background: #2c3e50; border-radius: 4px; overflow: hidden; }}
  .cat-bar {{ height: 100%; background: #00b4d8; border-radius: 4px; transition: width 0.3s; min-width: 2px; }}
  .cat-count {{ width: 40px; text-align: right; color: #e0e0e0; font-size: 0.85em; }}
  table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
  th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #2c3e50; font-size: 0.9em; }}
  th {{ color: #7f8c8d; text-transform: uppercase; font-size: 0.8em; }}
  td code {{ color: #00d4aa; font-size: 0.9em; }}
  .sev-badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; color: #fff; font-size: 0.8em; text-transform: uppercase; }}
  .footer {{ text-align: center; color: #7f8c8d; font-size: 0.8em; margin-top: 40px; padding: 20px; border-top: 1px solid #2c3e50; }}
  .raw-json {{ margin-top: 20px; }}
  .raw-json summary {{ cursor: pointer; color: #7f8c8d; font-size: 0.85em; }}
  .raw-json pre {{ background: #1a2a3a; border: 1px solid #2c3e50; border-radius: 8px; padding: 16px; overflow-x: auto; font-size: 0.8em; margin-top: 8px; color: #a0a0a0; max-height: 400px; }}
  .lang-row {{ display: flex; align-items: center; margin: 4px 0; gap: 10px; }}
  .lang-label {{ width: 120px; font-size: 0.85em; color: #b0b0b0; }}
  .lang-bar-bg {{ flex: 1; height: 14px; background: #2c3e50; border-radius: 4px; overflow: hidden; }}
  .lang-bar {{ height: 100%; background: #9b59b6; border-radius: 4px; min-width: 2px; }}
  .lang-count {{ width: 40px; text-align: right; color: #e0e0e0; font-size: 0.85em; }}
</style>
</head>
<body>
<div class="container">
  <h1>QA Report: {repo["name"]}</h1>
  <div class="subtitle">Generated {data["generated_at"][:19].replace("T", " ")} UTC · Last scan: {repo["last_scan"][:19].replace("T", " ") if repo["last_scan"] else "never"}</div>

  <div class="health-card">
    <div>
       <span class="vitality" style="color:{health_color}">{health}</span>
      <span class="health-band" style="color:{health_color}">{health_band}</span>
    </div>
    <div style="color:#7f8c8d;font-size:0.85em;margin-top:4px">
      Path: {repo["path"]} · Language: {repo["language"]}
    </div>
  </div>

  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-value">{summary["total_findings"]}</div>
       <div class="stat-label">Specks</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{summary["open_issues"]}</div>
      <div class="stat-label">Open Issues</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{summary["open_prs"]}</div>
      <div class="stat-label">Open PRs</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{summary["fix_attempts"]}</div>
      <div class="stat-label">Fix Attempts</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{summary["fixes_verified"]}</div>
      <div class="stat-label">Fixes Verified</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{summary["total_prs"]}</div>
      <div class="stat-label">PRs Created</div>
    </div>
  </div>

  <h2>Specks by Category</h2>
  {cat_bars}

  <h2>Specks by Language</h2>
  <div id="language-dist">
"""
        + "".join(
            f'<div class="lang-row">'
            f'  <span class="lang-label">{lang}</span>'
            f'  <div class="lang-bar-bg">'
            f'    <div class="lang-bar" style="width:{round(count / total_findings * 100, 1) if total_findings > 0 else 0}%"></div>'
            f"  </div>"
            f'  <span class="lang-count">{count}</span>'
            f"</div>\n"
            for lang, count in sorted(
                data["findings_by_language"].items(), key=lambda x: -x[1]
            )
        )
        + """
  </div>

  <h2>Top Rules</h2>
  <table>
    <tr><th>Rule</th><th>Count</th><th>Severity</th></tr>
"""
        + rules_rows
        + """
  </table>

  <h2>Vitality Trend</h2>
  <table>
    <tr><th>Date</th><th>Score</th><th>Specks</th></tr>
"""
        + trend_rows
        + """
  </table>

  <details class="raw-json">
    <summary>View Raw JSON Data</summary>
    <pre>"""
        + json.dumps(data, indent=2)
        + """</pre>
  </details>

  <div class="footer">
     Generated by <strong>bluei</strong> · bluei Vitality Report
  </div>
</div>
</body>
</html>"""
    )
    return html


def main():
    """CLI entry point for direct testing."""
    import argparse

    parser = argparse.ArgumentParser(description="Extract report data for a repository")
    parser.add_argument("--repo-name", required=True, help="Repository name")
    parser.add_argument("--repo-path", help="Repository path")
    parser.add_argument("--state-dir", help="State directory path")
    parser.add_argument("--repos-dir", help="Repos directory path")
    parser.add_argument("--days", type=int, default=30, help="Days of history")
    parser.add_argument(
        "--format", choices=["json", "html"], default="json", help="Output format"
    )
    args = parser.parse_args()

    data = extract_report_data(
        repo_path=args.repo_path or "",
        repo_name=args.repo_name,
        state_dir=Path(args.state_dir) if args.state_dir else None,
        repos_dir=Path(args.repos_dir) if args.repos_dir else None,
        days=args.days,
    )

    if args.format == "html":
        print(generate_report_html(data))
    else:
        print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
