#!/usr/bin/env python3
"""Standalone data types extracted from review.cycle for single-responsibility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ReviewCycleResult:
    """Counters tracking PR and finding outcomes for a single review cycle."""

    active_prs: int = 0
    blocked_prs: int = 0
    retry_eligible_prs: int = 0
    merge_ready_prs: int = 0
    paused_prs: int = 0
    retry_planned_prs: int = 0
    retry_prepared_prs: int = 0
    retry_executed_prs: int = 0
    retry_failed_prs: int = 0
    retry_exhausted_prs: int = 0
    # Phase G1: autonomous-review finding counters
    findings_detected: int = 0
    findings_published: int = 0
    findings_failed: int = 0
    findings_skipped: int = 0
    findings_absent: int = 0


@dataclass
class PublishFilterResult:
    """
    Result of Phase G6 limited-publish filter check.

    Attributes:
        passed: True only when all filters pass.
        decision: One of ``pass``, ``fail``, ``skipped``, ``bypassed``.
        failed_reason: Human-readable reason if decision is ``fail``,
                       empty string otherwise.
    """

    passed: bool
    decision: str  # "pass" | "fail" | "skipped" | "bypassed"
    failed_reason: str = ""


class CandidateValidationError(ValueError):
    """Raised when a candidate finding fails validation."""

    def __init__(self, message: str, errors: Optional[List[str]] = None):
        super().__init__(message)
        self.errors = errors or []
