"""Real ESLint diagnostics qualify one exact public cascade route.

The external adapter executes already-installed ESLint with frozen no-console
configuration. It invents no diagnostics, installs nothing, and invokes no real
model. This is public cascade composition, not native XO or model efficacy.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.test_ordinary_repair_loop import _bluei, _env, _git, _issues, _make_corpus, _register, _state


BEFORE = 'const answer = 42;\nconsole.warn(answer);\n'
AFTER = 'const answer = 42;\nif (answer !== 42) throw new Error("answer changed");\n'


def test_real_javascript_diagnostic_public_cascade_context(tmp_path):
    repo = _make_corpus(tmp_path, "javascript-lib", {
        "package.json": '{"name":"qualification-fixture","private":true,"scripts":{"check":"node --check app.js"}}',
        "app.js": BEFORE,
    })
    ws = tmp_path / "workspace"
    ws.mkdir()
    config_dir = _register(ws, "javascript-lib", repo, [["/tools/node", "--check", "app.js"]])
    tools = tmp_path / "tools"
    tools.mkdir()
    eslint_config = tools / "eslint.config.mjs"
    eslint_config.write_text('export default [{files:["**/*.js"], rules:{"no-console":"error"}}];\n')
    adapter = tools / "xo"
    adapter.write_text(
        "#!/bin/sh\nexec /tools/node /trusted-eslint/bin/eslint.js --no-config-lookup "
        f"--config {eslint_config} --format json .\n"
    )
    adapter.chmod(0o755)
    backend = tools / "opencode"
    calls = tmp_path / "backend-calls.jsonl"
    backend.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        f"Path({str(tmp_path / 'dispatched-prompt.md')!r}).write_text(Path(sys.argv[1]).read_text())\n"
        f"with Path({str(calls)!r}).open('a') as stream: stream.write('inert\\n')\n"
        f"Path('app.js').write_text({AFTER!r})\n"
    )
    backend.chmod(0o755)
    config = config_dir / "config.yaml"
    config.write_text(config.read_text().replace("language: python", "language: javascript")
                      .replace("fix_engine: deterministic", "fix_engine: opencode")
                      + "opencode_template: " + json.dumps(f"{backend} {{prompt_file}} {{finding_id}}") + "\n")
    env = _env(ws)
    env["PATH"] = str(tools) + os.pathsep + env["PATH"]
    # Explicit trust is confined to this synthetic repo; the real tool and its
    # frozen config live outside it. No network or install-capable command.
    env["BLUEI_TRUST_REPOSITORY_TOOLS"] = "1"
    detector_before = subprocess.run([str(adapter)], cwd=repo, env=env, capture_output=True, text=True)
    assert detector_before.returncode == 1, detector_before.stderr
    diagnostics = json.loads(detector_before.stdout)
    assert len(diagnostics) == 1
    assert [item["ruleId"] for item in diagnostics[0]["messages"]] == ["no-console"]
    run = _bluei(env, ws, "cascade", "run", "javascript-lib", "--fix-engine", "opencode",
                 "--no-dry-run")
    assert run.returncode == 0, run.stdout + run.stderr
    log = (ws / "logs/javascript-lib/run.log").read_text()
    assert "cascade-fix: succeeded" in log
    assert "falling to model" in log and "backend=opencode" in log
    assert "claude-fix:" in log and str(backend) in log
    assert "pr-cycle: unqualified queue finding=er-eslint-no-console" in log
    state = _state(ws, "javascript-lib")
    metrics = json.loads((state / "status.json").read_text())["latest_run_metrics"]
    assert metrics["fix_attempts"] == metrics["fixes_verified"] == 1
    assert metrics["claude_invocations"] == 1  # Legacy total-model counter, not a Claude provider claim.
    assert metrics["deterministic_invocations"] == 0
    assert metrics["detector_outcomes"]["plugin:plugin-typescript"]["tool_outcome"] == "tool-ran"
    assert metrics["model_invocations_by_backend"] == {"opencode": 1}
    assert metrics["prs_created"] == 0
    assert len(calls.read_text().splitlines()) == 1
    prompt = (tmp_path / "dispatched-prompt.md").read_text()
    assert "- rule: `no-console`" in prompt and "- file: `app.js`" in prompt
    assert "Baseline check 1: pre-fix: passed" in prompt
    assert "max_files_changed: `5`" in prompt and "max_loc_diff: `200`" in prompt
    assert "private configured commands" in prompt
    assert "/tools/node" not in prompt and "--check" not in prompt
    blocked = [item for item in _issues(ws, "javascript-lib") if item["rule"] == "er-eslint-no-console"]
    assert len(blocked) == 1 and "verified_fix_patch" not in blocked[0]
    issues = [item for item in _issues(ws, "javascript-lib") if item["rule"] == "no-console"]
    assert len(issues) == 1 and issues[0]["status"] == "open"
    patch = Path(issues[0]["verified_fix_patch"])
    independent = tmp_path / "independent"
    subprocess.run(["git", "clone", "--no-local", str(repo), str(independent)], check=True, capture_output=True)
    _git(independent, "remote", "remove", "origin")
    assert not _git(independent, "remote", "-v").stdout
    _git(independent, "apply", "--check", str(patch))
    _git(independent, "apply", str(patch))
    detector_after = subprocess.run([str(adapter)], cwd=independent, env=env, capture_output=True, text=True)
    assert detector_after.returncode == 0, detector_after.stderr
    clean_records = json.loads(detector_after.stdout)
    assert len(clean_records) == 1 and clean_records[0]["messages"] == []
    behavior_before = subprocess.run(["/tools/node", "-e", "console.warn = () => {};\n" + BEFORE
                                      + "\nif (answer !== 42) throw new Error('answer changed');"], capture_output=True)
    assert behavior_before.returncode == 0
    behavior = subprocess.run(["/tools/node", "app.js"], cwd=independent, capture_output=True)
    assert behavior.returncode == 0
    assert (repo / "app.js").read_text() == BEFORE
    ledger = [json.loads(line) for line in (state / "cascade_resolutions.jsonl").read_text().splitlines()]
    assert len(ledger) == 1 and ledger[0]["outcome"] == "resolved_llm"
    assert ledger[0]["finding_id"] == issues[0]["finding_id"]
    assert metrics["cost_total_usd"] > 0  # Nominal internal estimate, not provider billing.
    repeat = _bluei(env, ws, "repeat", "run", "javascript-lib", "--fix-engine", "opencode", "--no-dry-run")
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    repeated = json.loads((state / "status.json").read_text())["latest_run_metrics"]
    assert repeated["fix_attempts"] == repeated["fixes_verified"] == 0
    assert len(calls.read_text().splitlines()) == 1
    assert len((state / "cascade_resolutions.jsonl").read_text().splitlines()) == 1
