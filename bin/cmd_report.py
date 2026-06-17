"""Report and dashboard command handlers.

Extracted from bin/bluei.py to reduce its surface area. Handles the
`bluei report` family (json/html/text/pdf/webhook/watch) and
`bluei dashboard` (html/json with optional watch mode).
"""

from __future__ import annotations

import sys
from pathlib import Path

from bin.cmd_utils import (
    has_flag,
    parse_option,
    parse_repo_arg,
)

# Backward-compat aliases for tests that import these names from bin.bluei
_parse_repo_arg = parse_repo_arg
_parse_option = parse_option
_has_flag = has_flag


def _cmd_report(name: str, passthrough: list[str]):
    """Generate a vitality report. I3: uses extract_report_data() directly."""
    import json as _json
    from bluei.app.config import ConfigManager
    from bluei.app.registry import RepoRegistry
    from bluei.engine.report import extract_report_data, generate_report_html

    fmt = "text"
    output = None
    days = 30
    for arg in passthrough:
        if arg.startswith("--format="):
            fmt = arg.split("=", 1)[1]
        elif arg == "--format" and passthrough.index(arg) + 1 < len(passthrough):
            fmt = passthrough[passthrough.index(arg) + 1]
        elif arg.startswith("--output="):
            output = arg.split("=", 1)[1]
        elif arg == "-o" and passthrough.index(arg) + 1 < len(passthrough):
            output = passthrough[passthrough.index(arg) + 1]
        elif arg.startswith("--days="):
            try:
                days = int(arg.split("=", 1)[1])
            except ValueError:
                pass

    config_mgr = ConfigManager()
    registry = RepoRegistry(config_mgr)
    repo = registry.find_by_name(name)
    if not repo:
        print(f"bluei: project not found: {name}", file=sys.stderr)
        return 1

    repo_path = str(repo.config.path)
    repos_dir = config_mgr.repos_dir

    try:
        data = extract_report_data(repo_path, name, repos_dir=repos_dir, days=days)
    except Exception as exc:
        print(f"bluei: report generation failed: {exc}", file=sys.stderr)
        return 1

    if fmt == "json":
        output_text = _json.dumps(data, indent=2, default=str)
        if output:
            Path(output).write_text(output_text)
            print(f"Report written to {output}")
        else:
            print(output_text)
        return 0

    if fmt == "html":
        html = generate_report_html(data)
        if output:
            Path(output).write_text(html)
            print(f"Report written to {output}")
        else:
            print(html)
        return 0

    if fmt == "text":
        from bluei.app.report import ReportGenerator

        try:
            text = ReportGenerator.from_state_dir(
                name, config_mgr.repos_dir / name / "state", repo_path=repo_path
            )
            if output:
                Path(output).write_text(text)
                print(f"Report written to {output}")
            else:
                print(text)
            return 0
        except Exception:
            # Fall back to JSON summary if text report fails
            print(f"Repository: {data['repo']['name']}")
            print(f"Health: {data['repo']['health_score']}/100")
            print(f"Findings: {data['summary']['total_findings']}")
            print(f"Open issues: {data['summary']['open_issues']}")
            print(f"Open PRs: {data['summary']['open_prs']}")
            return 0

    # PDF and other formats — delegate to engine (requires external skill)
    if fmt in ("pdf",):
        from bin.bluei import _run_engine

        return _run_engine(["report", "--repo", name, "--format", fmt] + passthrough)

    # I4: --notify-webhook <url> — POST the report JSON to a webhook
    webhook_url = None
    for i, arg in enumerate(passthrough):
        if arg.startswith("--notify-webhook="):
            webhook_url = arg.split("=", 1)[1]
        elif arg == "--notify-webhook" and i + 1 < len(passthrough):
            webhook_url = passthrough[i + 1]

    if webhook_url:
        import urllib.request

        payload = _json.dumps(data, default=str).encode("utf-8")
        try:
            req = urllib.request.Request(
                webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                print(f"Report delivered to webhook (HTTP {resp.status})")
        except Exception as exc:
            print(f"bluei: webhook delivery failed: {exc}", file=sys.stderr)
            return 1
        return 0

    # I5: --watch — regenerate periodically
    if _has_flag(passthrough, "--watch"):
        import time

        interval = 30
        for i, arg in enumerate(passthrough):
            if arg.startswith("--interval="):
                interval = int(arg.split("=")[1])
            elif arg == "--interval" and i + 1 < len(passthrough):
                interval = int(passthrough[i + 1])
        print(f"Watching with interval={interval}s. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(interval)
                try:
                    data = extract_report_data(
                        repo_path, name, repos_dir=repos_dir, days=days
                    )
                    score = data["repo"]["health_score"]
                    findings = data["summary"]["total_findings"]
                    print(
                        f"[{time.strftime('%H:%M:%S')}] Health={score} Findings={findings}"
                    )
                except Exception as exc:
                    print(f"[{time.strftime('%H:%M:%S')}] Error: {exc}")
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    return 0


def _cmd_dashboard(rest: list[str]) -> int:
    import webbrowser

    from bluei.app.config import ConfigManager
    from bluei.app.dashboard import write_dashboard
    from bluei.app.dashboard import build_dashboard_data, render_dashboard_html

    output_format = _parse_option(rest, "--format") or "html"
    if output_format not in {"html", "json"}:
        print("bluei: dashboard --format must be html or json.", file=sys.stderr)
        return 1
    state_root = _parse_option(rest, "--state-root")
    config = ConfigManager()
    repos_dir = Path(state_root) if state_root else config.repos_dir
    output = _parse_option(rest, "--output") or _parse_option(rest, "-o")
    if output:
        output_path = Path(output)
    else:
        suffix = "json" if output_format == "json" else "html"
        output_path = config.workspace / f"bluei-dashboard.{suffix}"
    try:
        written = write_dashboard(
            repos_dir=repos_dir,
            output_path=output_path,
            output_format=output_format,
            repo_name=_parse_repo_arg(rest),
        )
    except ValueError as exc:
        print(f"bluei: {exc}", file=sys.stderr)
        return 1
    print(f"Dashboard written: {written}")
    if _has_flag(rest, "--open"):
        webbrowser.open(f"file://{written}")
    if _has_flag(rest, "--watch"):
        import time

        interval = 30
        for i, arg in enumerate(rest):
            if arg.startswith("--interval="):
                interval = int(arg.split("=")[1])
            elif arg == "--interval" and i + 1 < len(rest):
                interval = int(rest[i + 1])
        print(f"Watching with interval={interval}s. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(interval)
                write_dashboard(
                    repos_dir=repos_dir,
                    output_path=output_path,
                    output_format=output_format,
                    repo_name=_parse_repo_arg(rest),
                )
                print(f"Refreshed at {time.strftime('%H:%M:%S')}")
        except KeyboardInterrupt:
            print("Watch stopped.")
    return 0
