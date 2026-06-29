"""Build-time Recipe authoring. NOT imported by runtime code.

Mirrors tools/seed/ (ADR-0018 precedent). Consumes seeded Patterns, invokes
an LLM (via injectable callback) to author Recipe YAML, validates each
proposal (strict schema + detector oracle), and writes to recipes/staged/
under policy routing (ADR-0020).
"""
