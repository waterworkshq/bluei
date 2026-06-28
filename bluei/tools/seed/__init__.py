"""Build-time linter-rule ingestion pipeline (alpha.3).

NOT imported by runtime code. Lives outside enforce_architecture.py scope.
Parsers (ruff, eslint) emit ParsedRule tuples; package.py converts them to
committed product fixtures (Bundles, seeded Patterns, seeded Recipes).
"""
