"""bluei.engine.model_discovery — runtime model discovery (Phase 0, beta.1).

The canonical runtime home for model discovery. Promotes ``ResolvedModel`` and
``resolve_model`` out of ``bluei/tools/benchmark/runner.py`` (the runtime
``engine/`` layer must not depend on ``tools/`` — layering rule, M3 fix in
ARCHITECTURE.md).

Per ADR-0022 (amendment 2): discovery is **operator-config-validated** against
the active backend's real model catalog. **No shipped defaults** — the operator
supplies a ``model_tiers.yaml``; absent or empty config preserves today's
identity behavior exactly (every tier resolves to ``None`` → the call site
emits the template unchanged). See CONTEXT.md "Model Discovery".

Symbols:
    * ``ResolvedModel``        — tier resolved to a concrete invocation.
    * ``ModelDiscovery``       — Protocol (``discover(backend) -> List[ResolvedModel]``).
    * ``resolve_model``        — pick the discovered model matching a tier.
    * ``inject_model_flag``    — splice ``--model <id>`` into a backend template.
    * ``load_model_tiers``     — YAML loader for ``model_tiers.yaml``.
    * ``BackendModelDiscovery``— real discovery (operator-config-validated).
    * ``_list_backend_models`` — best-effort CLI query (Phase 5 research thread).

This module contains zero vendor model-id literals (AC-P2-3 source-level test).
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

import yaml

from bluei.engine.model_governor import ModelTier

_logger = logging.getLogger(__name__)


# ─── ResolvedModel (relocated verbatim from tools/benchmark/runner.py:40-52) ──


@dataclass
class ResolvedModel:
    """A tier resolved to a concrete invocation.

    Populated by discovery (mocked in alpha.6). beta.1: real CLI discovery.
    """

    tier: ModelTier
    backend: str  # "claude" | "opencode" | "deterministic"
    model_id: str  # from discovery, NOT hardcoded in the Governor
    input_per_1k: float
    output_per_1k: float
    cli_template: Optional[str] = None


# ─── ModelDiscovery Protocol ───────────────────────────────────────────────


class ModelDiscovery(Protocol):
    """Discovery contract: list the models available for this discovery's backend.

    ``MockModelDiscovery`` (benchmark fixture) and ``BackendModelDiscovery``
    (real CLI-backed) both satisfy this shape. The discovery object owns its
    backend (``BackendModelDiscovery.backend``) — ``discover`` takes no backend
    argument (Phase 8 B1 fix: the previous ``discover(backend)`` signature led
    ``resolve_model`` to pass a hardcoded ``"benchmark"`` literal that
    ``BackendModelDiscovery`` honored, silently making production discovery a
    no-op). ``resolve_model`` is typed against this Protocol (B4 fix) so
    ``engine`` never imports from ``tools``.
    """

    def discover(self) -> List[ResolvedModel]: ...


# ─── resolve_model (relocated; discovery param re-typed to Protocol — B4) ────


def resolve_model(
    discovery: "ModelDiscovery", tier: ModelTier
) -> Optional[ResolvedModel]:
    """Pick the discovered model matching the requested tier."""
    for model in discovery.discover():
        if model.tier == tier:
            return model
    return None


# ─── inject_model_flag (DESIGN §6; backend auto-detected from template — M2) ──


def inject_model_flag(template: str, resolved: Optional[ResolvedModel]) -> str:
    """Insert ``--model <id>`` into the backend's command template.

    No-op when ``resolved`` is ``None`` or carries no ``model_id`` (identity
    behavior — empty operator config). The backend is **detected from the
    template's first command token** (M2 fix: there is no ``backend`` field on
    ctx/args; the template is a pre-resolved string). Detection anchors on the
    leading token so it works with bare names AND full paths
    (``/usr/local/bin/claude``). On an unrecognized backend the template is
    returned unchanged with a warning (degraded but safe).
    """
    if resolved is None or not resolved.model_id:
        return template  # identity behavior — no flag injected
    # m1 (Phase 8): quote the model id — it is spliced into a template later
    # run via ``bash -l -c``. Operator-trusted + CLI-validated, but defense-in-depth
    # (the other template fields are shlex.quote'd in apply_claude_fix).
    flag = f"--model {shlex.quote(resolved.model_id)}"
    stripped = template.lstrip()
    first_token = stripped.split(None, 1)[0] if stripped else ""
    base = first_token.rsplit("/", 1)[-1]
    if base in ("claude", "opencode"):
        # Insert the flag immediately after the first command token, preserving
        # any leading whitespace and the token itself.
        idx = len(template) - len(stripped) + len(first_token)
        return template[:idx] + f" {flag}" + template[idx:]
    _logger.warning(
        "inject_model_flag: unrecognized backend token %r; leaving template unchanged",
        first_token,
    )
    return template


# ─── load_model_tiers (T0.4, DESIGN §4) ────────────────────────────────────


def load_model_tiers(path: Optional[Path]) -> Dict[str, Dict[ModelTier, str]]:
    """Load ``model_tiers.yaml`` → ``{backend: {ModelTier: model_id}}``.

    Operator-supplied tier→model mapping. **No shipped defaults** (ADR-0022
    amendment 2). Returns ``{}`` when:

      * ``path`` is ``None``
      * the file does not exist
      * the file is empty / parses to ``None``

    On any parse error or unknown tier string, that entry is skipped with a
    warning (degraded-but-functional — never raises).
    """
    if path is None:
        return {}
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        _logger.warning("model_tiers: failed to parse %s: %s", path, exc)
        return {}
    if not raw:
        return {}

    out: Dict[str, Dict[ModelTier, str]] = {}
    for backend, tiers in raw.items():
        if not isinstance(tiers, dict):
            _logger.warning(
                "model_tiers: skipping backend %r (expected mapping, got %s)",
                backend,
                type(tiers).__name__,
            )
            continue
        per_backend: Dict[ModelTier, str] = {}
        for tier_str, model_id in tiers.items():
            try:
                tier = ModelTier(tier_str)
            except ValueError:
                _logger.warning(
                    "model_tiers: unknown tier %r under backend %r; skipping",
                    tier_str,
                    backend,
                )
                continue
            if isinstance(model_id, str):
                per_backend[tier] = model_id
            else:
                _logger.warning(
                    "model_tiers: non-string model id %r for %s/%s; skipping",
                    model_id,
                    backend,
                    tier_str,
                )
        out[str(backend)] = per_backend
    return out


# ─── BackendModelDiscovery (T0.3, DESIGN §3) ───────────────────────────────


@dataclass
class BackendModelDiscovery:
    """Real discovery: validates operator tier-config against the backend CLI.

    ``tier_config`` comes from ``load_model_tiers`` (operator-supplied,
    EMPTY under the inert-until-configured default C2). ``discover`` queries
    the backend CLI for the available-model catalog and emits one
    ``ResolvedModel`` per tier whose configured model is actually present.
    Unavailable models are warned-and-skipped (degraded but functional).
    """

    tier_config: Dict[ModelTier, str]
    backend: str

    def discover(self) -> List[ResolvedModel]:
        backend = (
            self.backend
        )  # Phase 8 B1 fix: use the configured backend, not a caller-supplied literal
        available = _list_backend_models(backend)  # Dict[model_id, Dict metadata]
        out: List[ResolvedModel] = []
        for tier, model_id in self.tier_config.items():
            if model_id in available:
                meta = available[model_id]
                out.append(
                    ResolvedModel(
                        tier=tier,
                        backend=backend,
                        model_id=model_id,
                        input_per_1k=float(meta.get("input_per_1k", 0.0)),
                        output_per_1k=float(meta.get("output_per_1k", 0.0)),
                    )
                )
            else:
                _logger.warning(
                    "model_tiers: %s unavailable on %s; tier %s falls back",
                    model_id,
                    backend,
                    tier.value,
                )
        return out


# ─── _list_backend_models — the Phase 5 CLI research thread ─────────────────
#
# The exact ``claude`` / ``opencode`` model-listing command + output format is
# the open research thread (DESIGN §"Open research thread"). The contract is
# robust to its absence: any failure → ``{}`` → all operator mappings fall
# back → identity behavior (ADR-0022 amendment 2). Phase 5 will pin the real
# command once a backend exposes clean metadata; until then this function is
# deliberately conservative.
#
# Chosen commands (best-effort, documented for Phase 1/2 consumers):
#   * claude   → ``claude --list-models --output-format json``
#                (Claude Code-style flag; output expected as a JSON list of
#                ``{"id": "...", "input_per_1k": ..., "output_per_1k": ...}``.
#                No widely documented standard exists today.)
#   * opencode → no standard listing command shipped today; returns ``{}``.
# Both are wrapped in ``try/except`` + non-zero-exit guards so a missing
# binary, a changed flag, or unparseable output all collapse to ``{}``.


def _list_backend_models(backend: str) -> Dict[str, Dict[str, Any]]:
    """Query the backend CLI for its available-model catalog.

    Returns ``{model_id: {"input_per_1k": float, "output_per_1k": float, ...}}``.
    Returns ``{}`` on any failure (missing binary, non-zero exit, parse error,
    unknown backend). The contract is robust to its absence — callers fall back
    to identity behavior (ADR-0022 amendment 2).

    The exact command + output format is the Phase 5 research thread; the
    attempts below are best-effort and documented in the module docstring.
    """
    if backend == "claude":
        cmd = ["claude", "--list-models", "--output-format", "json"]
    elif backend == "opencode":
        # No standard listing command today; Phase 5 research thread.
        _logger.debug(
            "model_tiers: opencode model-listing not yet supported; returning empty"
        )
        return {}
    else:
        _logger.debug(
            "model_tiers: unknown backend %r; returning empty catalog", backend
        )
        return {}

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.debug(
            "model_tiers: %s listing command failed (%s); returning empty",
            backend,
            exc,
        )
        return {}
    if proc.returncode != 0:
        _logger.debug(
            "model_tiers: %s listing command exited %d; returning empty",
            backend,
            proc.returncode,
        )
        return {}

    try:
        import json

        payload = json.loads(proc.stdout or "")
    except (ValueError, TypeError) as exc:
        _logger.debug(
            "model_tiers: %s listing output unparseable (%s); returning empty",
            backend,
            exc,
        )
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict) and "id" in entry:
                out[str(entry["id"])] = entry
    elif isinstance(payload, dict):
        # Tolerate a dict-of-models shape too.
        for mid, entry in payload.items():
            if isinstance(entry, dict):
                out[str(mid)] = entry
    return out
