"""Exact staged recipes for the bounded registered-repository Foundry route."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

import yaml

from bluei.engine.governance import ApprovalRecord
from bluei.engine.jsonl import append_jsonl, read_jsonl
from bluei.engine.models import now_iso
from bluei.engine.recipe_schema import parse_recipe


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def trail_path(state_dir):
    return Path(state_dir) / "approval_records.jsonl"


@contextmanager
def trail_lock(state_dir):
    path = trail_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path


def latest(path, asset_ref=None):
    records = read_jsonl(path, skip_errors=False)
    if any(not isinstance(r, dict) for r in records):
        raise ValueError("invalid approval trail")
    if asset_ref is None:
        return {r["asset_ref"]: r for r in records}
    return next((r for r in reversed(records) if r.get("asset_ref") == asset_ref), {})


def bound_artifact(record, repo_id, state_dir, *, current_attempt=None):
    proof = record.get("evidence_snapshot") or {}
    if proof.get("contract") != "retained-ruff-b904-v1" or proof.get("repo_id") != repo_id:
        raise ValueError("unbound proposal")
    token = proof.get("proposal_id")
    if token != digest({k: v for k, v in proof.items() if k != "proposal_id"}):
        raise ValueError("proposal identity changed")
    text = proof["proposed_recipe_yaml"]
    if hashlib.sha256(text.encode()).hexdigest() != proof["recipe_sha256"]:
        raise ValueError("recipe bytes changed")
    recipe = parse_recipe(text)
    if record.get("asset_ref") != f"recipe:{recipe.id}" or recipe.rule != "ruff-b904":
        raise ValueError("recipe identity changed")
    observations = read_jsonl(Path(state_dir) / "accepted_fix_evidence.jsonl", skip_errors=False)
    matches = [r for r in observations if digest(r) == proof["evidence_sha256"] and r.get("repo_id") == repo_id]
    if len(matches) != 1:
        raise ValueError("accepted evidence changed")
    verify_retained_patch(state_dir, matches[0])
    require_fleet_evidence(state_dir, matches[0], current_attempt=current_attempt)
    return proof, recipe, text.encode()


def require_fleet_evidence(state_dir, evidence, *, current_attempt=None):
    from bluei.app.config import ConfigManager
    from bluei.engine.shared_pattern_library import SharedPatternLibrary

    manager = ConfigManager(Path(state_dir).resolve().parents[2])
    registered = {}
    for name in manager.list_repo_configs():
        repo = manager.load_repo_config(name)
        directory = manager.repos_dir / name / "state"
        if repo and repo.id and directory.resolve() == directory:
            registered.setdefault(repo.id, []).append(directory)
    if Path(state_dir).resolve() not in registered.get(evidence["repo_id"], []):
        raise ValueError("accepted evidence is not in registered state")
    library = SharedPatternLibrary(manager.workspace / "state" / "shared_patterns")
    pattern_id = evidence["shared_asset_ref"].removeprefix("pattern:")
    if not library.eligible_accepted(
        pattern_id, evidence["before"], registered, current_attempt=current_attempt,
        required_observation=(evidence["repo_id"], evidence["evidence_id"]),
    ):
        raise ValueError("shared promotion thresholds or retained evidence not satisfied")


def load_accepted_evidence(state_dir, repo_id, evidence_id):
    rows = read_jsonl(Path(state_dir) / "accepted_fix_evidence.jsonl", skip_errors=False)
    evidence, = (r for r in rows if r.get("evidence_id") == evidence_id and r.get("repo_id") == repo_id)
    raw = {k: evidence[k] for k in ("repo_id", "rule", "path", "base", "patch_sha256", "before", "after")}
    if digest(raw) != evidence_id:
        raise ValueError("accepted evidence changed")
    verify_retained_patch(state_dir, evidence)
    return evidence


def verify_retained_patch(state_dir, observation):
    name = observation["patch_name"]
    if not isinstance(name, str) or Path(name).name != name or not name.startswith("local-fix-"):
        raise ValueError("invalid retained patch reference")
    patch = Path(state_dir) / name
    if patch.is_symlink() or not patch.is_file():
        raise ValueError("retained patch unavailable")
    with patch.open("rb") as stream:
        data = stream.read((1 << 20) + 1)
    if len(data) > (1 << 20) or hashlib.sha256(data).hexdigest() != observation["patch_sha256"]:
        raise ValueError("retained patch changed")


def stage_path(state_dir, recipe):
    # Do not expose pending, staged or interrupted writes to the shared loader.
    return Path(state_dir) / "recipes" / "staged" / f"{recipe.id}.yaml"


def stage_exact(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        try:
            os.link(stream.name, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise ValueError("staged recipe already has different bytes") from None
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_decision(path, ref, decision, before, after, proof, actor):
    append_jsonl(path, ApprovalRecord(
        asset_ref=ref, decision=decision, native_state_before=before,
        native_state_after=after, reason="Exact retained repair recipe",
        evidence_snapshot=proof, actor=actor, timestamp=now_iso(),
    ).to_dict())


def select_active_recipe(state_dir, repo_id, checks, current_attempt, finding, file_text):
    """Use only explicitly resumed artifacts whose staged bytes still match."""
    from bluei.engine.models import canonical_ruff_rule
    from bluei.engine.recipe_engine import RecipeEngine

    if canonical_ruff_rule(finding.rule) != "ruff-b904":
        return None
    engine = RecipeEngine()
    try:
        entries = latest(trail_path(state_dir))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    for ref, entry in entries.items():
        if not isinstance(ref, str) or not ref.startswith("recipe:"):
            continue
        if entry.get("decision") != "resume" or entry.get("native_state_after") != "active-retained-recipe":
            continue
        try:
            proof, recipe, data = bound_artifact(entry, repo_id, state_dir, current_attempt=current_attempt)
            if digest(checks) != proof["checks_sha256"]:
                continue
            path = stage_path(state_dir, recipe)
            if not path.is_symlink() and path.read_bytes() == data and recipe.replacement.value in file_text:
                engine._index_recipe(recipe)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError):
            continue
    return engine.match(finding.rule, "python", file_text=file_text)
