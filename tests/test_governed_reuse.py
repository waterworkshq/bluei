"""Governed reuse begins at ordinary public repair acceptance."""
import hashlib
import json

from tests.test_ordinary_repair_loop import (
    AST_SYNTAX_CHECK,
    CHECK_CAUSE_SOURCE,
    PARSER_SOURCE,
    _bluei,
    _env,
    _git,
    _make_corpus,
    _patches,
    _register,
    _state,
)


def test_ordinary_repair_records_exact_retained_evidence(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    name = "registered-parser"
    repo = _make_corpus(tmp_path, name, {
        "parser_util.py": PARSER_SOURCE, "check_cause.py": CHECK_CAUSE_SOURCE,
    })
    _register(ws, name, repo, [
        ["python3", "-c", AST_SYNTAX_CHECK, "parser_util.py"],
        ["python3", "check_cause.py"],
    ])
    run = _bluei(_env(ws), ws, "accepted", "run", name,
                 "--fix-engine", "deterministic", "--no-dry-run")
    assert run.returncode == 0, run.stdout + run.stderr
    patch, = _patches(ws, name)
    records = _state(ws, name) / "accepted_fix_evidence.jsonl"
    evidence, = map(json.loads, records.read_text().splitlines())
    assert evidence["repo_id"] == "repo-registered-parser"
    assert evidence["base"] == _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert evidence["patch_sha256"] == hashlib.sha256(patch.read_bytes()).hexdigest()
    assert evidence["path"] == "parser_util.py"
    assert evidence["before"] == PARSER_SOURCE
    assert PARSER_SOURCE != evidence["after"]
    assert (repo / "parser_util.py").read_text() == PARSER_SOURCE
    clone = _make_corpus(tmp_path, "independent", {
        "parser_util.py": PARSER_SOURCE, "check_cause.py": CHECK_CAUSE_SOURCE,
    })
    _git(clone, "apply", str(patch))
    assert (clone / "parser_util.py").read_bytes() == evidence["after"].encode()
    assert evidence["local_asset_ref"].startswith("pattern:fp-")
    assert evidence["shared_asset_ref"].startswith("pattern:xp-")

    # Use a local author callback. The real matcher, detector and checks see
    # the proposed artifact; neither the later repair nor its state is seeded.
    import subprocess
    import sys

    from bluei.app import config
    from scripts.run_foundry import run_registered_foundry
    import yaml

    author = {
        "id": "governed-parser-b904", "rule": "ruff-b904", "language": "python",
        "match": {"type": "rule_exact", "rule": "ruff-b904"},
        "replacement": {"type": "text",
                        "value": 'except ValueError:\n        raise RuntimeError("quantity is not an integer")',
                        "replacement": 'except ValueError as err:\n        raise RuntimeError("quantity is not an integer") from err'},
    }
    def qualify(proposal):
        old_workspace = config.WORKSPACE
        config.WORKSPACE = ws
        try:
            return run_registered_foundry(
                name, evidence["evidence_id"],
                llm_callback=lambda _: yaml.safe_dump(proposal, sort_keys=False),
            )
        finally:
            config.WORKSPACE = old_workspace

    assert qualify(author) == 1  # one registered repository is not a fleet
    # The fleet fixtures check syntax on every candidate. The source fixture
    # also checks the exception cause for its one specific target.
    checks = [["python3", "-c", AST_SYNTAX_CHECK, "parser_util.py"]]
    for index, count in enumerate((5, 5, 4, 3, 1)):
        other_name = f"other-parser-{index}"
        files = {f"parser_{number}.py": PARSER_SOURCE for number in range(1, count)}
        files.update({"parser_util.py": PARSER_SOURCE, "check_cause.py": CHECK_CAUSE_SOURCE})
        other = _make_corpus(tmp_path, other_name, files)
        config_file = ws / "repos" / other_name / "config.yaml"
        config_file.parent.mkdir(parents=True)
        raw = yaml.safe_load((ws / "repos" / name / "config.yaml").read_text())
        raw.update(name=other_name, path=str(other), baseline_checks=checks)
        raw["id"] = evidence["repo_id"] if index == 0 else f"repo-{other_name}"
        config_file.write_text(yaml.safe_dump(raw))
        if index == 4:
            assert qualify(author) == 1  # only four distinct registered IDs
        observed = _bluei(_env(ws), ws, other_name, "run", other_name,
                          "--fix-engine", "deterministic", "--no-dry-run")
        assert observed.returncode == 0, observed.stderr
        assert len(_patches(ws, other_name)) == count

    assert qualify(author) == 1  # five registered IDs, but only 19 observations
    (other / "parser_extra.py").write_text(PARSER_SOURCE)
    _git(other, "add", "parser_extra.py")
    _git(other, "commit", "-qm", "twentieth fixture")
    observed = _bluei(_env(ws), ws, "twentieth", "run", other_name,
                      "--fix-engine", "deterministic", "--no-dry-run")
    assert observed.returncode == 0, observed.stderr
    shared = ws / "state" / "shared_patterns" / "accepted.jsonl"
    observations = [json.loads(line) for line in shared.read_text().splitlines()]
    assert len(observations) == 20
    assert len({r["repo_id"] for r in observations}) == 5

    wrong = {**author, "replacement": {**author["replacement"], "replacement": "pass"}}
    assert qualify(wrong) == 1
    assert qualify(author) == 0

    def learn(label, *args):
        return _bluei(_env(ws), ws, label, "learn", *args, "--repo", name)

    bundled = learn("bundle", "bundle", "--evidence", evidence["evidence_id"])
    assert bundled.returncode == 0, bundled.stderr
    bundle_path, = (_state(ws, name) / "golden_bundles").glob("*.yaml")
    assert yaml.safe_load(bundle_path.read_bytes())["asset_ref"] == evidence["shared_asset_ref"]

    item = learn("inbox", "inbox")
    assert item.returncode == 0, item.stderr
    ref = "recipe:governed-parser-b904"
    token, = (line.split()[-1] for line in item.stdout.splitlines() if "proposal:" in line)
    assert ref in item.stdout
    assert learn("wrong-token", "approve", ref, "--proposal", "stale").returncode != 0
    assert learn("wrong-ref", "approve", "recipe:wrong", "--proposal", token).returncode != 0
    staged = _state(ws, name) / "recipes" / "staged" / "governed-parser-b904.yaml"
    assert not staged.exists()
    retained = patch.read_bytes()
    patch.write_bytes(retained + b"\n")
    assert learn("patch-drift", "approve", ref, "--proposal", token).returncode != 0
    patch.write_bytes(retained)
    fleet_patch = _patches(ws, "other-parser-4")[0]
    fleet_bytes = fleet_patch.read_bytes()
    fleet_patch.write_bytes(fleet_bytes + b"\n")
    assert qualify(author) == 1
    assert learn("fleet-patch-drift", "approve", ref, "--proposal", token).returncode != 0
    fleet_patch.write_bytes(fleet_bytes)
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"conflicting stage\n")
    assert learn("stage-conflict", "approve", ref, "--proposal", token).returncode != 0
    assert staged.read_bytes() == b"conflicting stage\n"
    staged.unlink()
    approved = learn("approve", "approve", ref, "--proposal", token)
    assert approved.returncode == 0, approved.stderr
    assert json.loads((_state(ws, name) / "approval_records.jsonl").read_text().splitlines()[-1])["native_state_after"] == "staged-retained-recipe"
    trail = _state(ws, name) / "approval_records.jsonl"
    before_repeat = trail.read_bytes()
    assert learn("approve-repeat", "approve", ref, "--proposal", token).returncode == 0
    assert trail.read_bytes() == before_repeat
    payload = staged.read_bytes()
    staged.write_bytes(payload + b"\n# drift\n")
    assert learn("resume-drift", "resume", ref, "--proposal", token).returncode != 0
    staged.write_bytes(payload)
    resumed = learn("resume", "resume", ref, "--proposal", token)
    assert resumed.returncode == 0, resumed.stderr
    assert learn("pause", "pause", ref).returncode == 0
    paused = trail.read_bytes()
    assert learn("pause-repeat", "pause", ref).returncode == 0
    assert trail.read_bytes() == paused
    assert learn("resume-paused", "resume", ref, "--proposal", token).returncode == 0

    # A new base and line number produce a fresh finding in the same registered repo.
    (repo / "parser_util.py").write_text("# later revision\n\n" + PARSER_SOURCE)
    _git(repo, "add", "parser_util.py")
    _git(repo, "commit", "-qm", "later fixture")
    later = _bluei(_env(ws), ws, "later", "run", name,
                   "--fix-engine", "deterministic", "--no-dry-run")
    assert later.returncode == 0, later.stderr
    from tests.test_ordinary_repair_loop import _run_log

    assert "autofix: recipe=governed-parser-b904 applied" in _run_log(ws, name)
    new_patch, = set(_patches(ws, name)) - {patch}
    later_clone = _make_corpus(tmp_path, "independent-later", {
        "parser_util.py": "# later revision\n\n" + PARSER_SOURCE,
        "check_cause.py": CHECK_CAUSE_SOURCE,
    })
    _git(later_clone, "apply", str(new_patch))
    for checkout in (clone, later_clone):
        subprocess.run([sys.executable, "-B", "check_cause.py"], cwd=checkout, check=True)
    assert (repo / "parser_util.py").read_text() == "# later revision\n\n" + PARSER_SOURCE

    # A real rejected candidate supplies an adverse outcome, not a negative detector example.
    blocked_name = "blocked-parser"
    blocked_repo = _make_corpus(tmp_path, blocked_name, {"parser_util.py": PARSER_SOURCE})
    blocked_config = ws / "repos" / blocked_name / "config.yaml"
    blocked_config.parent.mkdir()
    raw = yaml.safe_load((ws / "repos" / name / "config.yaml").read_text())
    raw.update(id="repo-blocked", name=blocked_name, path=str(blocked_repo), baseline_checks=[
        ["python3", "-c", "assert 'as err' not in open('parser_util.py').read()"],
    ])
    blocked_config.write_text(yaml.safe_dump(raw))
    blocked = _bluei(_env(ws), ws, "rejected", "run", blocked_name,
                     "--fix-engine", "deterministic", "--no-dry-run")
    assert blocked.returncode == 0, blocked.stderr
    assert "post-fix: check=baseline-0 rc=1 available=True" in _run_log(ws, blocked_name)
    assert not _patches(ws, blocked_name)
    assert not (_state(ws, blocked_name) / "accepted_fix_evidence.jsonl").exists()
    assert qualify({**author, "id": "governed-parser-blocked"}) == 1
