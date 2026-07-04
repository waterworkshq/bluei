"""bluei.tools.benchmark.report — dev report renderer.

Renders a ``BenchmarkResult`` to markdown for the ``bluei benchmark`` CLI.
Internal dev tool — NOT the user-facing stat surface (that's Slice 3:
finalize.py / report.py flywheel_ledger).
"""

from __future__ import annotations

from bluei.tools.benchmark.runner import BenchmarkResult


def render_benchmark_markdown(result: BenchmarkResult) -> str:
    """Render a BenchmarkResult to a markdown dev report."""
    lines: list[str] = []
    lines.append("# Benchmark Report — Deterministic Flywheel Gap Analysis")
    lines.append("")
    lines.append(f"- **Corpus size:** {result.corpus_size}")
    lines.append(f"- **Policy version:** {result.policy_version}")
    lines.append(f"- **Generated at:** {result.generated_at}")
    lines.append("")

    # Tier distribution
    lines.append("## Tier Distribution (whole corpus)")
    lines.append("")
    lines.append("| Tier | Count |")
    lines.append("|------|-------|")
    for tier, count in sorted(result.tier_distribution.items()):
        lines.append(f"| {tier} | {count} |")
    lines.append("")

    # Family gap table
    lines.append("## Per-Family Coverage Gaps")
    lines.append("")
    lines.append(
        "| Rule family | Findings | Deterministic | Governor | Tier-2 | Gap? |"
    )
    lines.append(
        "|-------------|----------|---------------|----------|--------|------|"
    )
    for gap in result.family_gaps:
        tier2 = gap.tier_distribution.get("tier-2", 0)
        flag = "**GAP**" if gap.is_gap else ""
        lines.append(
            f"| {gap.rule_family} | {gap.findings_in_corpus} | "
            f"{gap.deterministic_resolved} | {gap.governor_reached} | "
            f"{tier2} | {flag} |"
        )
    lines.append("")

    # Flywheel score
    fs = result.flywheel_score
    lines.append("## Flywheel Score")
    lines.append("")
    lines.append(f"- **Total $ avoided:** ${fs.total_usd:.4f}")
    lines.append(f"- **Per-finding $ avoided:** ${fs.per_finding_usd:.6f}")
    lines.append(
        f"- **Deterministic savings:** ${fs.deterministic_savings_usd:.4f} "
        f"(cascade-resolved findings × full frontier cost)"
    )
    lines.append(
        f"- **Routing savings:** ${fs.routing_savings_usd:.4f} "
        f"(Governor downgrades × tier-2 delta)"
    )
    lines.append("")
    lines.append("### Mocked rates (reproducibility audit)")
    lines.append("")
    lines.append("| Tier | Input $/1k | Output $/1k |")
    lines.append("|------|-----------|------------|")
    for tier, rates in sorted(fs.mocked_rates.items()):
        lines.append(
            f"| {tier} | ${rates['input_per_1k']:.5f} | ${rates['output_per_1k']:.5f} |"
        )
    lines.append("")
    lines.append(
        f"_Token estimates: input={3000}, output={300} (mirrors pr_cycle.py:900-901)._"
    )
    lines.append("")

    return "\n".join(lines)
