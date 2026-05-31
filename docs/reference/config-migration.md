<!-- CATEGORY: reference -->

# Config Migration Guide

This document describes the evolution of the bluei configuration schema.

## Version 1 (Initial)

The original config format used by early versions of bluei.

### Fields
- `id`: Repo ID (auto-generated)
- `name`: Repo name
- `path`: Absolute path to repo
- `language`: Primary language string
- `plugin_id`: Plugin identifier
- `enabled`: Boolean
- `discovery`: Dict with discovery settings
- `baseline_checks`: List of command lists

### Limitations
- No `onboarding_version` field
- No `framework` field
- No `limits` section (used hardcoded defaults)
- No `safety` section
- No `meta` section
- Flat dict merge during template rendering

## Version 2 (Current)

The current config format, introduced with template-based onboarding.

### New Fields
- `meta.onboarding_version`: Integer (currently 2)
- `meta.template`: Template name used during onboarding
- `meta.inferred_by`: How config was generated ("template" or "heuristic")
- `meta.secondary_languages`: List of secondary languages detected
- `meta.detected_frameworks`: List of framework identifiers
- `meta.framework_detection_date`: ISO timestamp
- `framework`: Primary framework string or null
- `limits`: Dict with max_files_changed, max_loc_diff, max_fix_attempts, etc.
- `safety`: Dict with mode, profile, require_clean_worktree, etc.
- `fix_engine`: Fix backend strategy ("auto", "deterministic", "claude")
- `fallback_engines`: List of available fix backends
- `github`: Dict with live_actions, auto_merge
- `discovery.ecosystem`: Language ecosystem identifier
- `discovery.max_findings`: Cap on discovery findings
- `discovery.use_docker`: Whether Docker-backed discovery is needed

### Migrating from Version 1

Version 1 configs load safely — missing fields get sensible defaults:

- `onboarding_version` defaults to 1 (triggers upgrade prompt)
- `limits` defaults to conservative profile values
- `safety.mode` defaults to "observe"
- `safety.profile` defaults to "conservative"
- `framework` defaults to null

To upgrade a version 1 config:

```bash
bluei upgrade-config --repo <name>
```

This re-applies current heuristics and template selection while preserving safety settings.

### Template System

Version 2 introduced structured YAML templates in `templates/repos/`. Templates use deep merge — template defaults survive when inference only overrides specific sub-keys.

### Rule Packs

Version 2 added rule packs in `templates/rules/`. Each pack defines:
- `rules_enabled`: List of active rule IDs
- `rules_disabled`: List of suppressed rule IDs
- `severity_overrides`: Rule-specific severity adjustments
- `quick_win_bias`: Float 0-1 bias toward quick wins

## Upgrade Path

| From | To | Changes | Command |
|------|----|---------|---------|
| v1 | v2 | Template selection, limits, safety, framework detection | `bluei upgrade-config --repo <name>` |
