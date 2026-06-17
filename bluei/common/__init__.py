"""bluei.common — Cross-layer shared types and utilities.

Sits between engine/ (lowest layer) and app/ + review/ + campaigns/.
Both app/ and review/ import from common/ to avoid lateral coupling
(review → app was a known H5 violation; common/ is the clean resolution).

Dependency direction:
    common/ → engine/   (uses engine primitives like now_iso)
    app/    → common/   (uses shared types)
    review/ → common/   (uses shared types — NEVER reaches into app/)
    review/ → engine/   (uses engine directly when needed)

Contents:
    Utilities: now_iso (re-exported), generate_id
    Domain types: LanguageInfo, RepoConfig, Repo, Run
    Review-shared types: FeedbackEvent, FeedbackSentiment, FeedbackSource,
                         ReviewRun, ReviewRunStatus

History: extracted from app/models.py during H5 review→app decoupling (2026-06-18).
Review-specific types (ReviewMode, LiveRolloutMode, MonitoredSafetyState, etc.)
live in bluei/review/models.py.
"""
