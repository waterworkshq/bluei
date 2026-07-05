"""bluei.tools.benchmark — the Benchmark Harness (build-time dev tool).

Replays the synthetic Seed Library corpus through the cascade-simulation
proxy + Model Governor, producing a per-rule-family coverage gap analysis
+ Flywheel Score. Static analysis only — NO model invocation (C1/C2/C3).

NOT imported at runtime (mirrors ``tools/foundry/``, ``tools/graduator/``).
Drives the Deterministic Flywheel improvement loop: gaps identify where to
add Patterns/Recipes/Bundles.

Slice 2 of alpha.6 "Economics".
"""

from bluei.engine.model_discovery import ResolvedModel, resolve_model
from bluei.tools.benchmark.runner import (
    BenchmarkResult,
    FamilyCoverageGap,
    FindingBenchmark,
    FlywheelScore,
    MockModelDiscovery,
    default_mock_discovery,
    run_benchmark,
)

__all__ = [
    "BenchmarkResult",
    "FamilyCoverageGap",
    "FindingBenchmark",
    "FlywheelScore",
    "MockModelDiscovery",
    "ResolvedModel",
    "default_mock_discovery",
    "resolve_model",
    "run_benchmark",
]
