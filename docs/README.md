<!-- CATEGORY: meta -->
<!-- PURPOSE: Index of all documentation files in docs/ -->

# Documentation Index

All documentation for **bluei** — the autonomous QA agent for GitHub repositories.

## Reference

Documents covering configuration keys, CLI flags, detection rules, and migration procedures.

| Doc | Description |
|-----|-------------|
| [CONFIG_REFERENCE.md](reference/CONFIG_REFERENCE.md) | All configuration keys, values, schemas, recipe YAML format, batch rules, LLM fixable rules |
| [RULES_REFERENCE.md](reference/RULES_REFERENCE.md) | Complete reference for all 43 detection rules, routing behavior, context rules, recipes, cascade stages |
| [CLI_REFERENCE.md](reference/CLI_REFERENCE.md) | All 27 commands with flags, subcommand trees, exit codes, examples |
| [config-migration.md](reference/config-migration.md) | Config v1→v2 migration guide |
| [TEMPLATE_CATALOG.md](reference/TEMPLATE_CATALOG.md) | All 11 repo templates and 7 rule packs with selection heuristics |

## User

Documents for end users installing and operating bluei.

| Doc | Description |
|-----|-------------|
| [INSTALL.md](user/INSTALL.md) | Installation instructions and platform setup |
| [../README.md](../README.md) | Project overview, quick start, command reference, requirements |

## Operator

Documents for DevOps/SRE running bluei in production.

| Doc | Description |
|-----|-------------|
| [OPERATOR_GUIDE.md](operator/OPERATOR_GUIDE.md) | Day-to-day operations manual, campaigns, emergent rules, CI/CD |

## Developer

Documents for contributors understanding and extending the codebase.

| Doc | Description |
|-----|-------------|
| [ARCHITECTURE.md](developer/ARCHITECTURE.md) | Full system architecture, layers, data flow, all module responsibilities |
| [LANGUAGE_PACKS.md](developer/LANGUAGE_PACKS.md) | Plugin development guide, contract reference, creating custom plugins |
| [TESTING.md](developer/TESTING.md) | Testing philosophy, writing tests, fixture patterns, CI integration |
| [REVIEW_LIFECYCLE.md](developer/REVIEW_LIFECYCLE.md) | Review lifecycle engine: modes, remediation workflow, circuit breaker, publish filters |
| [FIX_PIPELINE.md](developer/FIX_PIPELINE.md) | End-to-end fix pipeline: classification, routing, tiering, validation, fix, verify |
| [AST_ENGINE.md](developer/AST_ENGINE.md) | AST engine: cross-language AST detection patterns and transforms |
| [SCAFFOLD_GENERATORS.md](developer/SCAFFOLD_GENERATORS.md) | Scaffold generators: test file and doc section generation from module inspection |
| [HEALTH_METHODOLOGY.md](developer/HEALTH_METHODOLOGY.md) | Health scoring methodology: components, bands, dashboard, report formats, cost tracking |
| [GITHUB_OPERATIONS.md](developer/GITHUB_OPERATIONS.md) | GitHub operations: PR merge lifecycle, rebase sweep, cleanup, regression detection |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Contributing guidelines, dev setup, architecture overview, first contribution |

## Design

Historical design documents describing architectural intent and decisions. These describe what was planned, not necessarily the current operational state. For current operational reference, see the Developer section above.

| Doc | Description |
|-----|-------------|
| [CONTEXTUAL_FIX_ARCHITECTURE.md](design/CONTEXTUAL_FIX_ARCHITECTURE.md) | Contextual fix engine architecture and component design |
| [BATCH_PR_ARCHITECTURE.md](design/BATCH_PR_ARCHITECTURE.md) | Batch PR engine architecture and grouping strategies |
| [REFACTOR_AUTONOMOUS_DESIGN.md](design/REFACTOR_AUTONOMOUS_DESIGN.md) | Autonomous large-refactor design contract |
| [LLM_FIXABLE_RULES_PLAN.md](design/LLM_FIXABLE_RULES_PLAN.md) | LLM-fixable rules implementation plan |
| [REFACTOR_CLASS_SCAFFOLDING.md](design/REFACTOR_CLASS_SCAFFOLDING.md) | Refactor-class scaffolding design |
| [advanced-care-capability-matrix-2026-04-09.md](design/advanced-care-capability-matrix-2026-04-09.md) | Capability readiness audit (point-in-time, archived) |

## Meta

Project governance and maintenance documents.

| Doc | Description |
|-----|-------------|
| [ROADMAP.md](meta/ROADMAP.md) | Shipped and remaining work |
| [../CHANGELOG.md](../CHANGELOG.md) | Release history |
| [../SECURITY.md](../SECURITY.md) | Security policy and vulnerability reporting |
| [../CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) | Contributor Covenant |
| [../OPEN_SOURCE_POLICY.md](../OPEN_SOURCE_POLICY.md) | Public core, commercial offering, and contribution-back policy |
| [../TRADEMARK.md](../TRADEMARK.md) | Brand and trademark use policy |
| [../OWNERSHIP.md](../OWNERSHIP.md) | Ownership, stewardship, and licensing notes |
| [../CLA.md](../CLA.md) | Contributor License Agreement |
