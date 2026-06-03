"""Lesson command handler.

Extracted from bin/bluei.py to reduce its surface area."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from bin.help_text import HELP_TEXT
from bin.cmd_utils import parse_option, parse_repo_arg

# Backward-compat aliases
_parse_repo_arg = parse_repo_arg
_parse_option = parse_option


def _cmd_lesson(rest: list[str]) -> int:
    if not rest or rest[0] in ("-h", "--help"):
        print(HELP_TEXT["lesson"])
        return 0

    subcmd = rest[0]
    sub_rest = rest[1:]
    if subcmd not in ("add", "list", "show"):
        print(
            f"bluei: unknown lesson subcommand '{subcmd}'. Try 'bluei help lesson'.",
            file=sys.stderr,
        )
        return 1

    repo = _parse_repo_arg(sub_rest)
    if not repo:
        print(
            f"bluei: lesson {subcmd} requires --repo <name>. Try 'bluei help lesson'.",
            file=sys.stderr,
        )
        return 1

    from bluei.app.config import ConfigManager, WORKSPACE
    from bluei.engine.constants import repo_lessons_path

    cm = ConfigManager(WORKSPACE)
    repo_config = cm.load_repo_config(repo)
    if not repo_config:
        print(f"bluei: repo '{repo}' not found in registry.", file=sys.stderr)
        return 1

    try:
        repo_path = Path(repo_config.path)
        lessons_file = repo_lessons_path(repo_path)
    except Exception as e:
        print(f"bluei: failed to resolve repo path: {e}", file=sys.stderr)
        return 1

    if subcmd == "add":
        return _lesson_add(lessons_file, sub_rest)
    elif subcmd == "list":
        return _lesson_list(lessons_file, sub_rest)
    elif subcmd == "show":
        return _lesson_show(lessons_file, sub_rest)
    return 0


def _lesson_add(lessons_file: Path, rest: list[str]) -> int:
    broke = _parse_option(rest, "--broke")
    changed = _parse_option(rest, "--changed")
    worked = _parse_option(rest, "--worked")
    finding_id = _parse_option(rest, "--finding-id")

    if not any([broke, changed, worked]):
        print(
            "bluei: lesson add requires at least one of --broke, --changed, --worked. Try 'bluei help lesson'.",
            file=sys.stderr,
        )
        return 1

    from bluei.engine.utils import append_lesson

    append_lesson(
        lessons_file=lessons_file,
        cycle_type="manual",
        finding_id=finding_id or "",
        what_broke=broke or "",
        what_changed=changed or "",
        what_worked=worked or "",
    )
    print(f"Lesson added to {lessons_file}")
    return 0


def _lesson_list(lessons_file: Path, rest: list[str]) -> int:
    from bluei.engine.utils import load_lessons_for_rule

    rule = _parse_option(rest, "--rule")
    limit_str = _parse_option(rest, "--limit")
    limit = int(limit_str) if limit_str else 20

    if rule:
        entries = load_lessons_for_rule(rule, lessons_file, limit=limit)
    else:
        entries = _list_all_lessons(lessons_file, limit=limit)

    if not entries:
        filter_desc = f" for rule '{rule}'" if rule else ""
        print(f"No lesson entries found{filter_desc}.")
        return 0

    for entry in entries:
        date = entry.get("date", "?")
        cycle = entry.get("cycle_type", "?")
        fid = entry.get("finding_id", "")
        status = (
            entry.get("changed")
            or entry.get("broke")
            or entry.get("worked")
            or "(empty)"
        )
        decayed = " [decayed]" if entry.get("decayed") else ""
        print(f"{date} ({cycle}){decayed}: {status}")
        if fid:
            print(f"  finding_id: {fid}")
    return 0


def _list_all_lessons(lessons_file: Path, limit: int = 20) -> list:
    if not lessons_file.exists():
        return []
    content = lessons_file.read_text(encoding="utf-8")
    entries = [e for e in content.split("\n## ") if e.strip()]
    result = []
    for entry_text in entries[-limit:]:
        lines = entry_text.strip().split("\n")
        entry = {
            "date": "",
            "cycle_type": "",
            "finding_id": "",
            "broke": "",
            "changed": "",
            "worked": "",
            "decayed": False,
        }
        if lines:
            parts = lines[0].split("|")
            entry["date"] = parts[0].strip() if parts else ""
            entry["cycle_type"] = parts[1].strip() if len(parts) > 1 else ""
        for line in lines[1:]:
            if line.startswith("finding_id:"):
                entry["finding_id"] = line.split(":", 1)[1].strip()
            elif line.startswith("- **Broke:**"):
                entry["broke"] = line.split("**Broke:**", 1)[1].strip()
            elif line.startswith("- **Changed:**"):
                entry["changed"] = line.split("**Changed:**", 1)[1].strip()
            elif line.startswith("- **Worked:**"):
                entry["worked"] = line.split("**Worked:**", 1)[1].strip()
        result.append(entry)
    result.reverse()
    return result


def _lesson_show(lessons_file: Path, rest: list[str]) -> int:
    finding_id = None
    for arg in rest:
        if not arg.startswith("-"):
            finding_id = arg
            break
    if not finding_id:
        print(
            "bluei: lesson show requires a finding_id. Try 'bluei help lesson'.",
            file=sys.stderr,
        )
        return 1

    from bluei.engine.utils import load_lessons_for_finding

    entries = load_lessons_for_finding(finding_id, lessons_file)
    if not entries:
        print(f"No lesson entries for finding_id '{finding_id}'.")
        return 0

    print(f"{len(entries)} entries for finding_id '{finding_id}':\n")
    for entry in entries:
        date = entry.get("date", "?")
        cycle = entry.get("cycle_type", "?")
        print(f"  ## {date} | {cycle}")
        print(f"  finding_id: {entry.get('finding_id', '')}")
        broke = entry.get("broke", "")
        changed = entry.get("changed", "")
        worked = entry.get("worked", "")
        if broke:
            print(f"  - **Broke:** {broke}")
        if changed:
            print(f"  - **Changed:** {changed}")
        if worked:
            print(f"  - **Worked:** {worked}")
        print()
    return 0
