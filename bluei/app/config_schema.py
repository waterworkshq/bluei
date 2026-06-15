"""Formal config schema versioning (H3).

Each schema version declares its required fields, optional fields, and
defaults. The registry provides:
- Validation: check a config dict matches its declared schema version
- Migration paths: documented upgrade sequence between versions
- Field documentation: what each version expects

The actual migration logic lives in OnboardEngine.upgrade_config(); this
module provides the declarative schema layer that validates and documents.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

CURRENT_SCHEMA_VERSION = 2


# ── Schema Definitions ──────────────────────────────────────────

SCHEMA_V1: Dict[str, Any] = {
    "version": 1,
    "description": "Initial onboarding schema — basic repo config without safety framework",
    "required_top_level": ["id", "name", "path", "language"],
    "optional_top_level": ["enabled", "discovery", "fix_engine", "baseline_checks"],
    "defaults": {
        "enabled": True,
        "fix_engine": "deterministic",
        "baseline_checks": [],
    },
    "known_fields": {
        "id": str,
        "name": str,
        "path": str,
        "language": str,
        "enabled": bool,
        "discovery": dict,
        "fix_engine": str,
        "baseline_checks": list,
    },
}

SCHEMA_V2: Dict[str, Any] = {
    "version": 2,
    "description": "Current schema — adds safety, github, limits, meta, plugin_id, rule_pack",
    "required_top_level": ["id", "name", "path", "language"],
    "optional_top_level": [
        "enabled",
        "discovery",
        "fix_engine",
        "fallback_engines",
        "baseline_checks",
        "safety",
        "github",
        "limits",
        "cooldowns",
        "meta",
        "plugin_id",
        "rule_pack",
        "framework",
        "claude_template",
        "opencode_template",
        "review_claude_template",
        "review_opencode_template",
    ],
    "defaults": {
        "enabled": True,
        "fix_engine": "auto",
        "fallback_engines": ["deterministic"],
        "baseline_checks": [],
        "safety": {
            "mode": "observe",
            "profile": "conservative",
            "protected_branches": ["main", "master"],
        },
        "github": {"live_actions": False, "auto_merge": False},
        "meta": {"onboarding_version": 2},
        "plugin_id": "",
    },
    "known_fields": {
        "id": str,
        "name": str,
        "path": str,
        "language": str,
        "enabled": bool,
        "discovery": dict,
        "fix_engine": str,
        "fallback_engines": list,
        "baseline_checks": list,
        "safety": dict,
        "github": dict,
        "limits": dict,
        "cooldowns": dict,
        "meta": dict,
        "plugin_id": str,
        "rule_pack": str,
        "framework": str,
    },
}

SCHEMAS: Dict[int, Dict[str, Any]] = {
    1: SCHEMA_V1,
    2: SCHEMA_V2,
}


# ── Migration Paths ─────────────────────────────────────────────

MIGRATIONS: Dict[Tuple[int, int], str] = {
    (1, 2): (
        "V1→V2: Re-detect language/framework, regenerate config from template, "
        "add safety/github/limits sections, set plugin_id from selected plugin. "
        "Executed by OnboardEngine.upgrade_config()."
    ),
}


def get_schema(version: int) -> Optional[Dict[str, Any]]:
    """Get the schema definition for a specific version."""
    return SCHEMAS.get(version)


def get_latest_version() -> int:
    """Get the latest/current schema version."""
    return CURRENT_SCHEMA_VERSION


def get_migration_path(from_version: int, to_version: int) -> Optional[str]:
    """Get the migration description between two versions."""
    return MIGRATIONS.get((from_version, to_version))


def validate_config(
    config_dict: Dict[str, Any], version: Optional[int] = None
) -> List[str]:
    """Validate a config dict against its schema version.

    Args:
        config_dict: The configuration to validate.
        version: Schema version to validate against. If None, reads from
                 config_dict.get('meta', {}).get('onboarding_version', 1).

    Returns:
        List of validation error messages. Empty list means valid.
    """
    if version is None:
        meta = config_dict.get("meta", {})
        if isinstance(meta, dict):
            version = meta.get("onboarding_version", 1)
        else:
            version = 1

    schema = SCHEMAS.get(version)  # type: ignore[arg-type]
    if schema is None:
        return [f"Unknown schema version: {version}"]

    errors: List[str] = []

    # Check required fields
    for field in schema.get("required_top_level", []):
        if field not in config_dict:
            errors.append(f"Missing required field: {field}")

    # Check field types for known fields
    known_fields = schema.get("known_fields", {})
    for field, expected_type in known_fields.items():
        if field in config_dict:
            actual = config_dict[field]
            if actual is not None and not isinstance(actual, expected_type):
                errors.append(
                    f"Field '{field}' should be {expected_type.__name__}, "
                    f"got {type(actual).__name__}"
                )

    return errors


def apply_defaults(
    config_dict: Dict[str, Any], version: Optional[int] = None
) -> Dict[str, Any]:
    """Apply schema defaults for any missing optional fields.

    Returns a new dict with defaults filled in. Does not mutate the input.
    """
    if version is None:
        meta = config_dict.get("meta", {})
        if isinstance(meta, dict):
            version = meta.get("onboarding_version", 1)
        else:
            version = 1

    schema = SCHEMAS.get(version)  # type: ignore[arg-type]
    if schema is None:
        return dict(config_dict)

    result = dict(config_dict)
    for field, default in schema.get("defaults", {}).items():
        if field not in result:
            result[field] = default

    return result
