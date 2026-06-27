# ADR-0016: Golden Validation Bundle schema is minimal and forward-compatible

**Status:** accepted

The Golden Validation Bundle schema ships minimal and grows additively. Old bundles load with defaults for absent fields; new fields never break existing bundles.

## The schema

```yaml
id: gb-ruff-b904-raise-with-from      # globally unique
asset_class: pattern                   # pattern | recipe | transform | emergent_rule
asset_ref: pattern:fp-a1b2c3d4e5f6     # AssetRef this bundle validates (null for product fixtures)
rule: ruff-b904
rule_family: ""                        # placeholder; populated by alpha.3 self-seeding
language: python
before: |
    raise ValueError("oops")
after: |
    raise ValueError("oops") from None
detector_before: |
    file.py:3:9: B904 Within an `except` clause, raise ... from err
detector_after: ""                     # empty = clean
validation_command: "ruff check --select B904"  # declared check, not observed Dry Replay results
source_finding_id: null                # null for product fixtures; finding_id for repo-created
extracted_at: "2026-06-27T00:00:00Z"
```

## Why minimal

The schema captures exactly what a regression fixture needs: the code before, the code after, what the detector said before, what it says after, and the command to verify. Everything else (`negative_examples`, `imports_touched`, `validation_commands_passed`, `rule_family` populated) is enrichment that lands later without breaking bundles written against this minimal shape.

## Consequences

- Product fixtures and repo-state bundles use the same schema.
- A bundle with no `negative_examples` is valid; a reader treats absent fields as empty/default.
- The `validation_command` field is the bundle's *declared* check (what the bundle says to run), not Dry Replay's *observed* results (which live in `dry_replay.jsonl` via ADR-0011). This separation prevents circular reasoning: the bundle declares what should pass; Dry Replay records what actually passed.
