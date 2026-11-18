"""Public refusal controls for the single explicitly supported cascade route."""

import json
import os
import sys
from pathlib import Path

import pytest

from tests.test_ordinary_repair_loop import _bluei, _env, _issues, _make_corpus, _register, _state


SOURCE = 'const answer = 42;\nconsole.warn(answer);\n'
FIXED = 'const answer = 42;\nif (answer !== 42) throw new Error("answer changed");\n'


def _case(tmp_path, *, checks=None, body="", tool="valid", extra_source=""):
    repo = _make_corpus(tmp_path, "refusal", {
        "package.json": '{"name":"refusal","private":true,"scripts":{"check":"node --check app.js"}}',
        "app.js": SOURCE, **({"other.py": extra_source} if extra_source else {}),
    })
    ws = tmp_path / "workspace"
    ws.mkdir()
    config_dir = _register(ws, "refusal", repo, checks if checks is not None else [["/tools/node", "--check", "app.js"]])
    tools = tmp_path / "tools"
    tools.mkdir()
    config = tools / "eslint.config.mjs"
    config.write_text('export default [{files:["**/*.js"], rules:{"no-console":"error"}}];\n')
    adapter = tools / "xo"
    adapter.write_text(
        "#!/bin/sh\nexec /tools/node /trusted-eslint/bin/eslint.js --no-config-lookup "
        f"--config {config} --format json .\n" if tool == "valid" else "#!/bin/sh\nexit 2\n"
    )
    adapter.chmod(0o755 if tool != "missing" else 0o644)
    backend = tools / "opencode"
    calls = tmp_path / "calls.txt"
    backend.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as stream: stream.write('inert\\n')\n" + body
    )
    backend.chmod(0o755)
    path = config_dir / "config.yaml"
    path.write_text(path.read_text().replace("language: python", "language: javascript")
                    .replace("fix_engine: deterministic", "fix_engine: opencode")
                    + "opencode_template: " + json.dumps(f"{backend} {{prompt_file}} {{finding_id}}") + "\n")
    env = _env(ws)
    env["PATH"] = str(tools) + os.pathsep + env["PATH"]
    env["BLUEI_TRUST_REPOSITORY_TOOLS"] = "1"
    return repo, ws, env, calls


def _run(ws, env, engine="opencode"):
    result = _bluei(env, ws, "refused", "run", "refusal", "--fix-engine", engine, "--no-dry-run")
    state = _state(ws, "refusal")
    status = json.loads((state / "status.json").read_text())
    return result, state, status["latest_run_metrics"]


def _no_positive(state):
    assert not list(state.rglob("*.patch"))
    ledger = state / "cascade_resolutions.jsonl"
    records = [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
    assert not any(record.get("outcome") in {"resolved_llm", "resolved_deterministic"} for record in records)
    patterns = state / "fix_patterns.jsonl"
    assert not patterns.exists() or not patterns.read_text().strip()


@pytest.mark.parametrize("tool", ["missing", "error"])
def test_unavailable_or_failed_producer_never_dispatches(tmp_path, tool):
    repo, ws, env, calls = _case(tmp_path, tool=tool)
    result, state, metrics = _run(ws, env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert metrics["run_completed"] is False
    assert metrics["detector_outcomes"]["plugin:plugin-typescript"]["status"] in {"failed", "unavailable"}
    assert not calls.exists()
    assert (repo / "app.js").read_text() == SOURCE
    _no_positive(state)


@pytest.mark.parametrize("output", ["crash", "{}", "[null]", "[]", '[{"filePath":"app.js","messages":[]}]',
                                    '[{"filePath":"app.js"}]', '[{"filePath":"app.js","messages":[null]}]'])
def test_malformed_zero_or_one_exit_tool_output_is_not_clean_evidence(tmp_path, output):
    repo, ws, env, calls = _case(tmp_path)
    adapter = tmp_path / "tools/xo"
    adapter.write_text(f"#!{sys.executable}\nprint({output!r})\nraise SystemExit(1)\n")
    result, state, metrics = _run(ws, env)
    assert result.returncode == 1
    producer = metrics["detector_outcomes"]["plugin:plugin-typescript"]
    assert producer["status"] == "failed" and producer["tool_outcome"] == "tool-error"
    assert not calls.exists()
    assert (repo / "app.js").read_text() == SOURCE
    _no_positive(state)


def test_missing_checks_refuses_before_backend(tmp_path):
    repo, ws, env, calls = _case(tmp_path, checks=[])
    _result, state, metrics = _run(ws, env)
    assert metrics["fixes_verified"] == metrics["claude_invocations"] == 0
    assert any("validation" in reason for reason in metrics["blocked_reasons"])
    assert not calls.exists()
    assert (repo / "app.js").read_text() == SOURCE
    _no_positive(state)


@pytest.mark.parametrize("rejection", ["scope", "closure"])
def test_zero_exit_rejected_candidate_never_confirms_positive(tmp_path, rejection):
    body = (
        f"Path('app.js').write_text({FIXED!r})\nPath('large.txt').write_text('untracked')\n"
        if rejection == "scope" else
        "Path('app.js').write_text(Path('app.js').read_text() + '\\n// candidate touched\\n')\n"
    )
    repo, ws, env, calls = _case(tmp_path, body=body)
    if rejection == "scope":
        path = ws / "repos/refusal/config.yaml"
        path.write_text(path.read_text().replace("max_loc_diff: 200", "max_loc_diff: 1"))
    _result, state, metrics = _run(ws, env)
    assert len(calls.read_text().splitlines()) == 1
    assert metrics["claude_invocations"] == metrics["fixes_failed_verification"] == 1
    assert metrics["fixes_verified"] == 0
    issues = [item for item in _issues(ws, "refusal") if item["rule"] == "no-console"]
    assert len(issues) == 1 and issues[0]["status"] == "fix_failed_verification"
    if rejection == "scope":
        assert "needs-human-scope-limit-exceeded" in metrics["blocked_reasons"]
        assert issues[0]["status_detail"] == "needs-human-scope-limit-exceeded"
    else:
        assert issues[0]["status_detail"] == "detector still firing after fix + validation"
        assert "still_firing=1" in (ws / "logs/refusal/run.log").read_text()
    assert (repo / "app.js").read_text() == SOURCE
    _no_positive(state)


def test_deterministic_engine_never_runs_model_fallback(tmp_path):
    repo, ws, env, calls = _case(tmp_path)
    _result, state, metrics = _run(ws, env, engine="deterministic")
    assert metrics["claude_invocations"] == metrics["fixes_verified"] == 0
    assert not calls.exists()
    assert (repo / "app.js").read_text() == SOURCE
    _no_positive(state)


def test_tool_failure_during_post_repair_rescan_never_confirms_candidate(tmp_path):
    # Candidate removes the diagnostic, but an independently controlled marker
    # makes the detector return malformed output only during closure rescan.
    marker = tmp_path / "detector-broken"
    body = f"Path('app.js').write_text({FIXED!r})\nPath({str(marker)!r}).write_text('broken')\n"
    repo, ws, env, calls = _case(tmp_path, body=body)
    adapter = tmp_path / "tools/xo"
    original = adapter.read_text()
    adapter.write_text(original.replace(
        "#!/bin/sh\n", f"#!/bin/sh\nif [ -f {marker} ]; then printf 'crash\\n'; exit 1; fi\n"
    ))
    _result, state, metrics = _run(ws, env)
    assert len(calls.read_text().splitlines()) == 1
    assert metrics["claude_invocations"] == metrics["fixes_failed_verification"] == 1
    assert metrics["fixes_verified"] == 0
    assert (repo / "app.js").read_text() == SOURCE
    _no_positive(state)


def test_unrelated_failed_producer_still_vetoes_supported_rule(tmp_path):
    repo, ws, env, calls = _case(tmp_path, extra_source="def invalid(:\n")
    result, state, metrics = _run(ws, env)
    assert result.returncode == 1
    assert any(record["status"] in {"failed", "unavailable"} for record in metrics["detector_outcomes"].values())
    assert not calls.exists()
    assert (repo / "app.js").read_text() == SOURCE
    _no_positive(state)
