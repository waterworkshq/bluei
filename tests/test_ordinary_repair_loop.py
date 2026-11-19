"""Ordinary public repair-loop regressions (ORDINARY-REPAIR-LOOP-fqfrwzbd, S2-06).

Drives the canonical public CLI only — ``bin/bluei run <name> --fix-engine
deterministic --no-dry-run --no-learning`` (repository discovery scope) and
``bin/bluei scan <name>`` for queue seeding — against a fresh, remote-free,
ordinary public Python corpus. Nothing is imported from the product: the
workspace is authored as the public state artifacts the CLI reads, and every
behavioral claim is checked by running corpus scripts as subprocesses or by
static ``ast`` parsing of corpus sources.

Corpus (independent of the Stage 1 F401 preview/selected demo):

- ``parser_util.py`` — ``except ValueError: raise RuntimeError(...)`` (ruff
  B904: no explicit ``from`` cause). ``__cause__`` is None before the repair
  and the caught ValueError after it. This is NOT a claim of lost data or a
  functional defect: Python already chains the caught exception implicitly
  via ``__context__``. The repair under test is the bounded diagnostic
  requirement that B904 pins down — the re-raise must declare its cause
  explicitly — proven by independent output, while normal-path value and
  raised type/message are asserted unchanged before and after.
- ``mixer.py`` — ``except (ValueError, TypeError):`` raise (ruff B904 fires,
  no built-in recipe pattern matches the tuple except-clause: the
  deterministic engine must report a truthful no-op).
- ``banner.py`` — one >88 char line (ruff E501, catalog confidence 0.65,
  below the 0.7 issue threshold: discovered but never queued).
- ``stock_NN.py`` × 20 — distinct B904 findings filling the open-issues cap
  (20) with real tool-produced finding IDs.

Contracts asserted: (1) one retained patch, pristine source, issue stays
open, no model; (2) the retained patch applied to a pristine clone flips the
explicit-cause check rc 1→0 with syntax and identity preserved; (3) truthful
no-op for the recipe mismatch alongside a completed repair, E501 never
queued; (4) at a full cap-20 queue the fresh finding is repaired through its
existing record and disappeared records stay open/unpatched; (5) a new
finding at a full queue is not admitted. Offline only; raw run output and
engine logs are retained under the tmp workspace as proof.
"""
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BLUEI_CLI = ROOT / "bin" / "bluei"
RUN_TIMEOUT = 300

PARSER_SOURCE = (
    "def parse_quantity(raw):\n"
    "    try:\n"
    "        return int(raw)\n"
    "    except ValueError:\n"
    '        raise RuntimeError("quantity is not an integer")\n'
)
MIXER_SOURCE = (
    "def blend_values(first, second):\n"
    "    try:\n"
    "        return int(first) + int(second)\n"
    "    except (ValueError, TypeError):\n"
    '        raise RuntimeError("values are not numeric")\n'
)
BANNER_SOURCE = (
    'BANNER = "inventory reconciliation summary — quarterly turnover,'
    ' shrinkage, backlog and returns across every regional warehouse"\n'
)
# Syntax control for baseline checks: a `python -c` AST parse — equivalent
# syntax evidence to py_compile but writes no __pycache__ artifact (py_compile
# writes bytecode explicitly, which would leave untracked files in the repair
# worktree and fail patch retention).
AST_SYNTAX_CHECK = (
    "import ast, sys; ast.parse(open(sys.argv[1]).read())"
)

CHECK_CAUSE_SOURCE = '''\
import sys

# No bytecode artifacts anywhere this check runs, regardless of environment.
sys.dont_write_bytecode = True

from parser_util import parse_quantity

# Preservation, before and after any repair: normal-path value, raised
# exception type, and message must be identical.
assert parse_quantity("42") == 42
try:
    parse_quantity("not-a-number")
except RuntimeError as exc:
    assert str(exc) == "quantity is not an integer"
    # B904 explicit-cause requirement: __cause__ is None before the repair
    # (the caught ValueError is still reachable via __context__); after it
    # the ValueError must be the declared direct cause.
    sys.exit(0 if type(exc.__cause__).__name__ == "ValueError" else 1)
sys.exit(1)
'''


def _git(repo: Path, *args: str, check: bool = True):
    identity = [
        "-c", "user.name=ordinary-corpus",
        "-c", "user.email=corpus@localhost",
    ]
    return subprocess.run(
        ["git", "-C", str(repo), *identity, *args],
        check=check, capture_output=True, text=True, timeout=60,
    )


def _make_corpus(parent: Path, name: str, files: dict) -> Path:
    """Create a committed, remote-free ordinary public corpus repository."""
    repo = parent / name
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "-q", str(repo)],
        check=True, capture_output=True, text=True,
    )
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "ordinary corpus fixture")
    return repo


def _yaml(value) -> str:
    """Emit one YAML scalar/flow-sequence node (JSON strings are YAML)."""
    return json.dumps(value)


def _register(ws: Path, name: str, repo: Path, baseline_checks, max_issues=10):
    """Author the public workspace artifacts the CLI reads (no product import)."""
    (ws / "bluei").symlink_to(ROOT / "bluei", target_is_directory=True)
    (ws / "plugins").symlink_to(ROOT / "plugins", target_is_directory=True)
    limits = {
        "open_issues_cap": 20,
        "open_prs_cap": 5,
        "max_prs_per_run": 2,
        "max_issues_per_run": max_issues,
        "max_files_changed": 5,
        "max_loc_diff": 200,
        "max_fix_attempts": 3,
    }
    # Engine-validated ranges: staleness must be >= 1 (default 7200 kept);
    # finding/merge cooldowns may be 0 so queue items stay actionable now.
    cooldowns = {"finding_seconds": 0, "merge_minutes": 0,
                 "staleness_seconds": 7200}
    lines = [
        "id: " + _yaml("repo-" + name),
        "name: " + _yaml(name),
        "path: " + _yaml(str(repo)),
        "language: python",
        "enabled: true",
        "fix_engine: deterministic",
        "fallback_engines:",
        "  - deterministic",
        "baseline_checks:",
    ]
    lines += ["  - " + _yaml(list(cmd)) for cmd in baseline_checks]
    lines.append("limits:")
    lines += ["  %s: %d" % (key, value) for key, value in limits.items()]
    lines.append("cooldowns:")
    lines += ["  %s: %d" % (key, value) for key, value in cooldowns.items()]
    lines += ["github:", "  live_actions: false", "  auto_merge: false"]
    repo_dir = ws / "repos" / name
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "config.yaml").write_text("\n".join(lines) + "\n",
                                           encoding="utf-8")
    return repo_dir


def _env(ws: Path):
    home = ws / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["QA_AGENT_WORKSPACE"] = str(ws)
    env["HOME"] = str(home)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    return env


def _bluei(env, ws: Path, label: str, *args: str, timeout=RUN_TIMEOUT):
    """Run the canonical public CLI; retain raw output as proof."""
    result = subprocess.run(
        [sys.executable, str(BLUEI_CLI), *args],
        env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
    )
    proofs = ws / "proof-runs"
    proofs.mkdir(parents=True, exist_ok=True)
    (proofs / f"{label}.out").write_text(result.stdout, encoding="utf-8")
    (proofs / f"{label}.err").write_text(result.stderr, encoding="utf-8")
    return result


def _state(ws: Path, name: str) -> Path:
    return ws / "repos" / name / "state"


def _issues(ws: Path, name: str) -> list:
    path = _state(ws, name) / "issues.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("issues", [])


def _findings(ws: Path, name: str) -> list:
    path = _state(ws, name) / "findings.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run_log(ws: Path, name: str) -> str:
    path = ws / "logs" / name / "run.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _rule_of(record: dict) -> str:
    """Canonical case-insensitive rule key (producer casing may vary)."""
    return str(record.get("rule", "")).casefold()


def _patches(ws: Path, name: str) -> list:
    return sorted(_state(ws, name).glob("local-fix-*.patch"))


def _repair_run(ws: Path, name: str, label: str, env=None):
    return _bluei(env or _env(ws), ws, label, "run", name,
                  "--fix-engine", "deterministic",
                  "--no-dry-run", "--no-learning")


def _primary_scenario(base: Path):
    """One ordinary corpus + one canonical deterministic run (shared proof)."""
    ws = base / "workspace"
    ws.mkdir(parents=True)
    corpus = _make_corpus(base, "ordinary-lib", {
        "parser_util.py": PARSER_SOURCE,
        "check_cause.py": CHECK_CAUSE_SOURCE,
    })
    _register(ws, "ordinary-lib", corpus, [
        ["python3", "-c", AST_SYNTAX_CHECK, "parser_util.py"],
        ["python3", "check_cause.py"],
    ])
    run = _repair_run(ws, "ordinary-lib", "primary")
    return ws, corpus, run


@pytest.fixture(scope="module")
def primary(tmp_path_factory):
    return _primary_scenario(tmp_path_factory.mktemp("ordinary-primary"))


def test_repair_retains_single_patch_and_keeps_source_pristine(primary):
    ws, corpus, run = primary
    assert run.returncode == 0, run.stdout + run.stderr

    # Registered source is never mutated by the ordinary loop.
    assert (corpus / "parser_util.py").read_text(encoding="utf-8") \
        == PARSER_SOURCE

    # Exactly one retained local patch, referenced by the open issue.
    patches = _patches(ws, "ordinary-lib")
    assert len(patches) == 1
    issues = [i for i in _issues(ws, "ordinary-lib")
              if _rule_of(i) == "ruff-b904"]
    assert len(issues) == 1
    issue = issues[0]
    assert issue["status"] == "open"
    assert Path(issue["verified_fix_patch"]) == patches[0]
    attempts = [e for e in issue.get("history", [])
                if e.get("event") == "fix_attempted"]
    assert len(attempts) == 1

    # Deterministic lane only: one recipe application, no model dispatch.
    log = _run_log(ws, "ordinary-lib")
    applied = [line for line in log.splitlines()
               if "autofix: recipe=" in line and "applied" in line
               and "b904" in line.casefold()]
    assert len(applied) == 1
    assert "claude rc=" not in log
    assert "cost-limit" not in log


@pytest.mark.parametrize("stage,expected_rc", [("pristine", 1), ("patched", 0)])
def test_retained_patch_declares_cause_on_pristine_clone(primary, stage,
                                                         expected_rc,
                                                         tmp_path):
    ws, corpus, _run = primary
    patch = _patches(ws, "ordinary-lib")[0]
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(corpus), str(clone)],
                   check=True, capture_output=True, text=True, timeout=60)
    # Strictly remote-free copy: drop the local origin the clone created.
    _git(clone, "remote", "remove", "origin")
    remotes = subprocess.run(
        ["git", "-C", str(clone), "remote", "-v"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    assert remotes.stdout.strip() == ""
    if stage == "patched":
        _git(clone, "apply", str(patch))

    behavior = subprocess.run(
        ["python3", "check_cause.py"], cwd=str(clone),
        capture_output=True, text=True, timeout=60,
    )
    assert behavior.returncode == expected_rc, behavior.stdout + behavior.stderr

    # Static syntax check of the corpus source actually present in the clone.
    source = (clone / "parser_util.py").read_text(encoding="utf-8")
    ast.parse(source)
    if stage == "patched":
        # The patch touches exactly the one target module.
        touched = [line.split(" b/", 1)[1] for line in
                   patch.read_text(encoding="utf-8").splitlines()
                   if line.startswith("+++ b/")]
        assert touched == ["parser_util.py"]


def test_repeated_ordinary_run_is_bounded_noop(tmp_path):
    """Bounded repeat control: a second ordinary run on an already-verified
    retained patch must not re-attempt, add artifacts, or invoke a model
    while the registered source is unchanged (the finding ID is the same).
    A red result here means the loop re-does verified work — parent proof
    needed for the pending "skip issue with active retained patch" guard."""
    ws, corpus, first = _primary_scenario(tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr
    prior = _patches(ws, "ordinary-lib")
    assert len(prior) == 1

    again = _repair_run(ws, "ordinary-lib", "repeat")
    # A completed no-work run must finish cleanly; a crash is a failure.
    assert again.returncode == 0, again.stdout + again.stderr

    assert _patches(ws, "ordinary-lib") == prior
    assert (corpus / "parser_util.py").read_text(encoding="utf-8") \
        == PARSER_SOURCE
    issue = [i for i in _issues(ws, "ordinary-lib")
             if _rule_of(i) == "ruff-b904"][0]
    assert issue["status"] == "open"
    assert issue["verified_fix_patch"] == str(prior[0])
    attempts = [e for e in issue.get("history", [])
                if e.get("event") == "fix_attempted"]
    assert len(attempts) == 1  # first run only: zero new attempts on repeat
    repeat_proof = (
        (ws / "proof-runs" / "repeat.out").read_text(encoding="utf-8")
        + (ws / "proof-runs" / "repeat.err").read_text(encoding="utf-8")
        + _run_log(ws, "ordinary-lib")
    )
    assert "retained patch pending" in repeat_proof
    assert "claude rc=" not in _run_log(ws, "ordinary-lib")


def test_recipe_mismatch_noop_is_truthful_and_repair_continues(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    corpus = _make_corpus(tmp_path, "ordinary-mixed", {
        "parser_util.py": PARSER_SOURCE,
        "mixer.py": MIXER_SOURCE,
        "banner.py": BANNER_SOURCE,
        "check_cause.py": CHECK_CAUSE_SOURCE,
    })
    _register(ws, "ordinary-mixed", corpus, [
        ["python3", "-c", AST_SYNTAX_CHECK, "parser_util.py"],
        ["python3", "-c", AST_SYNTAX_CHECK, "mixer.py"],
        ["python3", "check_cause.py"],
    ])
    run = _repair_run(ws, "ordinary-mixed", "mixed")
    assert run.returncode == 0, run.stdout + run.stderr

    findings = _findings(ws, "ordinary-mixed")
    # Real discovery output: both B904 shapes plus the below-threshold E501.
    rules = {_rule_of(f) for f in findings}
    assert "ruff-b904" in rules and "ruff-e501" in rules

    issues = _issues(ws, "ordinary-mixed")
    by_path = {i["path"]: i for i in issues}
    # E501 (0.65) is never admitted to the actionable queue.
    assert "banner.py" not in by_path
    assert len(issues) == 2

    mixer = by_path["mixer.py"]
    # Truthful no-op: one attempt, failed verification, nothing changed.
    assert mixer["status"] == "fix_failed_verification"
    mixer_attempts = [e for e in mixer.get("history", [])
                      if e.get("event") == "fix_attempted"]
    assert len(mixer_attempts) == 1
    assert "verified_fix_patch" not in mixer

    # The matching finding is still repaired in the same ordinary run.
    parser = by_path["parser_util.py"]
    assert parser["status"] == "open"
    patches = _patches(ws, "ordinary-mixed")
    assert len(patches) == 1
    assert Path(parser["verified_fix_patch"]) == patches[0]
    parser_attempts = [e for e in parser.get("history", [])
                       if e.get("event") == "fix_attempted"]
    assert len(parser_attempts) == 1

    assert (corpus / "mixer.py").read_text(encoding="utf-8") == MIXER_SOURCE
    assert (corpus / "parser_util.py").read_text(encoding="utf-8") \
        == PARSER_SOURCE

    log = _run_log(ws, "ordinary-mixed")
    applied = [line for line in log.splitlines()
               if "autofix: recipe=" in line and "applied" in line
               and "b904" in line.casefold()]
    assert len(applied) == 1
    assert "claude rc=" not in log


def _cap_scenario(tmp_path, keep: int):
    """Seed the actionable queue to the cap (20) with real tool finding IDs."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    stock = {
        "stock_%02d.py" % i: (
            "def check_stock_%02d(code):\n"
            "    try:\n"
            "        value = int(code)\n"
            "    except ValueError:\n"
            '        raise RuntimeError("stock %02d is not numeric")\n'
            "    return value\n" % (i, i)
        )
        for i in range(20)
    }
    corpus = _make_corpus(tmp_path, "ordinary-queue", stock)
    _register(ws, "ordinary-queue", corpus, [
        ["python3", "-c", AST_SYNTAX_CHECK, "stock_00.py"],
    ], max_issues=20)
    env = _env(ws)
    seed = _bluei(env, ws, "queue-seed", "scan", "ordinary-queue")
    assert seed.returncode == 0, seed.stdout + seed.stderr
    seeded = _issues(ws, "ordinary-queue")
    assert len(seeded) == 20
    if keep < 20:
        for i in range(keep, 20):
            (corpus / ("stock_%02d.py" % i)).unlink()
        _git(corpus, "add", "-A")
        _git(corpus, "commit", "-qm", "corpus narrows to fresh findings")
    return ws, corpus


def test_cap_full_queue_repairs_fresh_and_keeps_disappeared_open(tmp_path):
    ws, corpus = _cap_scenario(tmp_path, keep=1)
    run = _repair_run(ws, "ordinary-queue", "queue-repair")
    assert run.returncode in (0, 1), run.stdout + run.stderr

    after = _issues(ws, "ordinary-queue")
    # No new admission: still exactly the 20 real records.
    assert len(after) == 20

    repaired = [i for i in after if i.get("verified_fix_patch")]
    assert len(repaired) == 1
    assert repaired[0]["path"] == "stock_00.py"
    assert repaired[0]["status"] == "open"
    assert len(_patches(ws, "ordinary-queue")) == 1

    # Disappeared records: still open, unpatched, not re-attempted.
    for issue in after:
        if issue["path"] == "stock_00.py":
            continue
        assert issue["status"] == "open", issue
        assert "verified_fix_patch" not in issue
        attempts = [e for e in issue.get("history", [])
                    if e.get("event") == "fix_attempted"]
        assert attempts == []

    log = _run_log(ws, "ordinary-queue")
    assert "claude rc=" not in log


def test_cap_full_queue_blocks_new_admission(tmp_path):
    ws, corpus = _cap_scenario(tmp_path, keep=0)
    # A brand-new finding appears while the queue is at the cap.
    (corpus / "fresh_defect.py").write_text(
        "def parse_batch(raw):\n"
        "    try:\n"
        "        return int(raw)\n"
        "    except ValueError:\n"
        '        raise RuntimeError("batch is not an integer")\n',
        encoding="utf-8",
    )
    _git(corpus, "add", "-A")
    _git(corpus, "commit", "-qm", "new finding appears at full queue")
    run = _repair_run(ws, "ordinary-queue", "queue-cap-block")
    assert run.returncode in (0, 1), run.stdout + run.stderr

    after = _issues(ws, "ordinary-queue")
    assert len(after) == 20
    assert all(i["path"] != "fresh_defect.py" for i in after)
    assert all("verified_fix_patch" not in i for i in after)
    assert _patches(ws, "ordinary-queue") == []

    proof = (ws / "proof-runs" / "queue-cap-block.out").read_text(
        encoding="utf-8") + (ws / "proof-runs" / "queue-cap-block.err").read_text(
        encoding="utf-8")
    log = _run_log(ws, "ordinary-queue")
    assert "meets/exceeds issue cap" in (proof + log)
