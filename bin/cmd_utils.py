"""Shared CLI utility functions.

Extracted from bin/bluei.py to break circular imports between
bin/bluei.py and the cmd_* modules."""

from __future__ import annotations

from typing import List, Optional


def parse_repo_arg(rest: list[str]) -> str | None:
    """Extract --repo <name> from argument list."""
    i = 0
    while i < len(rest):
        if rest[i] == "--repo" and i + 1 < len(rest):
            return rest[i + 1]
        i += 1
    return None


def parse_option(rest: list[str], name: str) -> str | None:
    i = 0
    while i < len(rest):
        if rest[i] == name and i + 1 < len(rest):
            return rest[i + 1]
        i += 1
    return None


def parse_csv_option(rest: list[str], name: str) -> list[str]:
    value = parse_option(rest, name)
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def has_flag(rest: list[str], name: str) -> bool:
    return name in rest


def parse_positive_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default
