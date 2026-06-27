# ADR-0014: SUPERSEDED — release scoping decision, not architectural

**Status:** superseded (2026-06-27)

This ADR documented a release-scoping decision (cascade-internal savings instrumentation IN alpha.2; envelope fields OUT to a later release). It does not meet ADR criteria: the decision is not hard to reverse (irrelevant once both items ship), not surprising (include cheap plumbing, defer expensive enrichment is obvious), and not the result of a real architectural trade-off (no alternatives with different consequences — just release sizing).

The information lives in the ROADMAP release sections and seed docs where it can be updated as scope evolves. Architectural decisions about what the cascade-internal savings instrumentation looks like, if any are non-obvious, will get their own ADR.
