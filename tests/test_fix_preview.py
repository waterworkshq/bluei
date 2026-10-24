"""Tests for `bluei fix-preview` — the bounded local patch-preview proof.

Drives the PUBLIC command (the bin/bluei dispatcher handler in-process, plus
the real bin/bluei script for CLI-shape cases) against real git and real Ruff
inside tmp_path fixtures. No network, no gh auth, no live repository.

A subprocess-transport spy wraps ``subprocess.run`` (the transport, never the
engine's business logic) and fails the test immediately if any forbidden
command — git push/fetch, gh, model CLIs, network tools, upload-pack — is
attempted, and records every argv for exact-invocation assertions.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BLUEI_BIN = ROOT / "bin" / "bluei"

REPO_NAME = "fix-preview-fixture"
REPO_FILES = {
    "mod_a.py": "import os\nimport sys\n\nprint(sys.path)\n",
    "mod_b.py": "import json\nimport os\n\nprint(os.getcwd())\n",
    ".gitignore": "ignored/\n",
    "ignored/never.py": "import os\nimport sys\n\nprint(sys.path)\n",
}
SYMLINK_NAME = "link.py"  # tracked symlink -> mod_a.py (not a candidate)

requires_ruff = pytest.mark.skipif(
    shutil.which("ruff") is None, reason="ruff not installed"
)

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}


def _git(args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )


def _commit_repo(repo: Path) -> None:
    _git(["add", "."], repo)
    _git(["commit", "-m", "baseline"], repo)


# ── Transport spy ───────────────────────────────────────────────────────────

FORBIDDEN_TOKENS = {
    # publishing / remote git / transport (upload-pack would run on --no-local clones)
    "push", "pull", "fetch", "remote", "upload-pack", "receive-pack",
    # gh / model CLIs
    "gh", "claude", "opencode", "codex", "aider", "ollama",
    # network tools
    "curl", "wget", "nc", "ncat", "ssh", "scp", "sftp", "ftp", "rsync",
}


def install_transport_spy(monkeypatch):
    """Wrap subprocess.run: forbid network/publish/model/transport commands.

    Delegates everything else to the real transport, so the engine code
    under test is fully exercised — only the transport is wrapped. Returns
    (calls, envs): recorded argv lists and the env dict each call ran with
    (None for calls made without an explicit env, e.g. test helpers).
    """
    real_run = subprocess.run
    calls = []
    envs = []

    def spy(cmd, *args, **kwargs):
        assert isinstance(cmd, list), f"non-list command (shell leak?): {cmd!r}"
        tokens = {Path(cmd[0]).name, *cmd[1:]}
        hit = tokens & FORBIDDEN_TOKENS
        assert not hit, f"forbidden command attempted: {sorted(hit)} in {cmd}"
        calls.append(list(cmd))
        envs.append(kwargs.get("env"))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    return calls, envs


# ── Fixtures ────────────────────────────────────────────────────────────────


def _build_fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.email", "test@bluei.dev"], repo)
    _git(["config", "user.name", "Bluei Test"], repo)
    for rel, content in REPO_FILES.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    (repo / SYMLINK_NAME).symlink_to(repo / "mod_a.py")
    _commit_repo(repo)
    return repo


def _register(monkeypatch, workspace: Path, repo_path: Path, name: str) -> None:
    import bin.bluei as bluei_main
    import bluei.app.config as cfg_mod
    from bluei.app.config import ConfigManager
    from bluei.app.registry import RepoRegistry

    monkeypatch.setattr(cfg_mod, "WORKSPACE", workspace)
    monkeypatch.setenv("QA_AGENT_WORKSPACE", str(workspace))
    monkeypatch.setattr(bluei_main, "BLUEI_DIR", workspace)
    RepoRegistry(ConfigManager(workspace)).create_from_path(repo_path, name=name)


@pytest.fixture
def preview_env(tmp_path, monkeypatch):
    """Registered fixture repo + isolated workspace; returns env dict."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo_path = _build_fixture_repo(tmp_path)
    _register(monkeypatch, workspace, repo_path, REPO_NAME)

    counter = {"n": 0}

    def fresh_out():
        counter["n"] += 1
        return tmp_path / f"preview-out-{counter['n']}"

    return {
        "tmp": tmp_path,
        "workspace": workspace,
        "repo_path": repo_path,
        "repo_name": REPO_NAME,
        "fresh_out": fresh_out,
    }


def _run_cli(ev, monkeypatch, args, name=None):
    """Invoke the public dispatcher handler for fix-preview in-process."""
    import bin.bluei as bluei_main

    monkeypatch.setattr(bluei_main, "BLUEI_DIR", ev["workspace"])
    monkeypatch.setenv("QA_AGENT_WORKSPACE", str(ev["workspace"]))
    return bluei_main._cmd_fix_preview(name or ev["repo_name"], args)


def _snapshot_git_state(repo: Path) -> dict:
    head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    porcelain = _git(["status", "--porcelain"], repo).stdout
    tree = {}
    for path in sorted((repo / ".git").rglob("*")):
        if path.is_file():
            key = str(path.relative_to(repo))
            tree[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"head": head, "porcelain": porcelain, "tree": tree}


# ── Happy path (real git + real ruff) ───────────────────────────────────────


class TestFixPreviewPatchProduced:
    @requires_ruff
    def test_patch_produced_end_to_end(self, preview_env, monkeypatch, capsys):
        ev = preview_env
        before = _snapshot_git_state(ev["repo_path"])
        calls, envs = install_transport_spy(monkeypatch)
        out_dir = ev["fresh_out"]()

        rc = _run_cli(ev, monkeypatch, ["--rule", "F401", "--output-dir", str(out_dir)])
        captured = capsys.readouterr()

        assert rc == 0, captured.err
        assert "patch_produced" in captured.out
        assert "mod_a.py" in captured.out

        # Artifacts persisted before cleanup (atomic_json_write also leaves
        # its .lock sidecar).
        patch = out_dir / "fix.patch"
        assert patch.is_file() and patch.stat().st_size > 0
        assert (out_dir / "report.json").is_file()
        assert {"fix.patch", "report.json"} <= {p.name for p in out_dir.iterdir()}

        report = json.loads((out_dir / "report.json").read_text())
        assert report["schema"] == "bluei.fix-preview.report/1"
        assert report["status"] == "patch_produced"
        assert report["rule"] == "F401"
        assert report["repo"]["name"] == ev["repo_name"]
        assert report["baseline_head"] == before["head"]
        assert report["tool"]["name"] == "ruff"
        assert report["tool"]["version"] and report["tool"]["version"][0].isdigit()
        assert report["scope"]["target_file"] == "mod_a.py"
        assert report["scope"]["max_files_fixed"] == 1
        assert report["findings"]["before_in_target_file"] >= 1
        assert report["findings"]["remaining_in_target_file"] == 0
        assert report["changed_files"] == ["mod_a.py"]
        assert report["validation"] == {
            "ruff_recheck": "pass",
            "ast_compile": "pass",
        }
        assert report["boundary"] == {
            "llm_calls": 0,
            "prs_created": 0,
            "remote_operations": 0,
            "source_worktree_mutations": 0,
        }

        # Bounded scope: exactly one file in the patch.
        patch_text = patch.read_text()
        assert "mod_a.py" in patch_text
        for untouched in ("mod_b.py", SYMLINK_NAME, "never.py"):
            assert untouched not in patch_text

        # Source checkout untouched: HEAD, status, and every .git byte.
        assert _snapshot_git_state(ev["repo_path"]) == before

        # Gitignored file content untouched on disk.
        assert (ev["repo_path"] / "ignored" / "never.py").read_text() == (
            REPO_FILES["ignored/never.py"]
        )

        # ── Exact-invocation assertions on the recorded argv ──
        assert calls, "expected at least one git/ruff subprocess"
        binaries = {Path(c[0]).name for c in calls}
        assert binaries <= {"git", "ruff", "ruff.exe"}

        # Local clone: no transport, no hardlinks, empty template.
        clone_calls = [c for c in calls if "clone" in c]
        assert clone_calls, "expected a git clone invocation"
        clone_cmd = clone_calls[0]
        assert "--local" in clone_cmd
        assert "--no-hardlinks" in clone_cmd
        assert any(a.startswith("--template=") for a in clone_cmd)
        assert not any("upload-pack" in a for a in clone_cmd)

        # Baseline pinned explicitly in the clone.
        checkout_calls = [c for c in calls if "checkout" in c]
        assert checkout_calls, "expected an explicit baseline checkout"
        assert ["--detach", before["head"]] == checkout_calls[0][
            checkout_calls[0].index("--detach") : checkout_calls[0].index("--detach") + 2
        ]

        # Diff must disable external diff drivers and textconv.
        git_calls = [c for c in calls if Path(c[0]).name == "git"]
        diff_calls = [c for c in git_calls if "diff" in c]
        assert diff_calls, "expected a git diff invocation"
        assert "--no-ext-diff" in diff_calls[0]
        assert "--no-textconv" in diff_calls[0]

        # Engine git calls carry the hook/fsmonitor-disabling overrides.
        # (The test's own snapshot helper also spawns plain `git` calls, so
        # assert on the engine's identified invocations.)
        for engine_call in (clone_cmd, checkout_calls[0], diff_calls[0]):
            assert "core.hooksPath=" in engine_call, engine_call
            assert "core.fsmonitor=false" in engine_call, engine_call

        # The pinned fixed-tool argv for the fix step.
        fix_calls = [
            c
            for c in calls
            if Path(c[0]).name.startswith("ruff") and "--fix" in c
        ]
        assert fix_calls, "expected a ruff --fix invocation"
        fix_cmd = fix_calls[0]
        for flag in ("--isolated", "--no-cache", "--no-unsafe-fixes", "--select"):
            assert flag in fix_cmd, f"missing {flag} in fix argv: {fix_cmd}"
        assert fix_cmd[fix_cmd.index("--select") + 1] == "F401"
        assert fix_cmd[fix_cmd.index("--") + 1 :] == ["mod_a.py"]

        # ── PATH hardening: absolute tools, sanitized child PATH ──
        # Every engine invocation (git calls carry the -c overrides; ruff
        # calls have a ruff basename) must use an absolute executable path.
        engine_calls = [
            c
            for c in calls
            if "core.hooksPath=" in c or Path(c[0]).name.startswith("ruff")
        ]
        assert engine_calls
        for c in engine_calls:
            assert Path(c[0]).is_absolute(), f"non-absolute tool path: {c[0]}"
        # Engine children never see relative or empty PATH entries.
        for env in envs:
            if env is None:
                continue  # test-helper call without explicit env
            entries = env.get("PATH", "").split(os.pathsep)
            assert entries and all(
                p and os.path.isabs(p) for p in entries
            ), f"relative/empty PATH entry leaked to child: {env.get('PATH')}"

    @requires_ruff
    def test_patch_applies_to_second_isolated_copy(self, preview_env, monkeypatch):
        ev = preview_env
        out_dir = ev["fresh_out"]()
        rc = _run_cli(ev, monkeypatch, ["--rule", "F401", "--output-dir", str(out_dir)])
        assert rc == 0

        second = ev["tmp"] / "second-copy"
        _git(
            ["clone", "--no-hardlinks", "--no-local", str(ev["repo_path"]), str(second)],
            ev["tmp"],
        )
        apply_res = subprocess.run(
            ["git", "apply", str(out_dir / "fix.patch")],
            cwd=str(second),
            capture_output=True,
            text=True,
        )
        assert apply_res.returncode == 0, apply_res.stderr

        check_a = subprocess.run(
            ["ruff", "check", "--isolated", "--select", "F401", "--", "mod_a.py"],
            cwd=str(second),
            capture_output=True,
            text=True,
        )
        assert check_a.returncode == 0, check_a.stdout + check_a.stderr

        # The one-file bound is visible in the second copy: mod_b is untouched.
        check_b = subprocess.run(
            ["ruff", "check", "--isolated", "--select", "F401", "--", "mod_b.py"],
            cwd=str(second),
            capture_output=True,
            text=True,
        )
        assert check_b.returncode == 1  # finding intentionally remains

    @requires_ruff
    def test_no_findings_is_honest_noop(self, tmp_path, monkeypatch, capsys):
        """A repo with no F401 findings is a clean, honest no-op."""
        repo = tmp_path / "clean-repo"
        repo.mkdir()
        _git(["init"], repo)
        _git(["config", "user.email", "t@t"], repo)
        _git(["config", "user.name", "t"], repo)
        (repo / "clean.py").write_text("import sys\n\nprint(sys.path)\n")
        _commit_repo(repo)

        workspace = tmp_path / "ws-clean"
        workspace.mkdir()
        _register(monkeypatch, workspace, repo, "clean-repo")

        import bin.bluei as bluei_main

        out_dir = tmp_path / "clean-out"
        rc = bluei_main._cmd_fix_preview(
            "clean-repo", ["--rule", "F401", "--output-dir", str(out_dir)]
        )
        captured = capsys.readouterr()

        assert rc == 0, captured.err
        assert "no_findings" in captured.out
        assert not (out_dir / "fix.patch").exists()
        report = json.loads((out_dir / "report.json").read_text())
        assert report["status"] == "no_findings"
        assert report["scope"]["target_file"] is None
        assert report["changed_files"] == []

    @requires_ruff
    def test_no_safe_fix_status_when_nothing_fixable(self, tmp_path, monkeypatch, capsys):
        """Findings exist but no SAFE fix applies (F401 in __init__.py offers
        no fix) — reported as a distinct no_safe_fix no-op, never a patch and
        never mislabeled no_findings."""
        repo = tmp_path / "nosafefix-repo"
        repo.mkdir()
        _git(["init"], repo)
        _git(["config", "user.email", "t@t"], repo)
        _git(["config", "user.name", "t"], repo)
        (repo / "__init__.py").write_text("import os\n")  # F401, fix=null
        _commit_repo(repo)

        workspace = tmp_path / "ws-nosafefix"
        workspace.mkdir()
        _register(monkeypatch, workspace, repo, "nosafefix-repo")

        import bin.bluei as bluei_main

        out_dir = tmp_path / "nosafefix-out"
        rc = bluei_main._cmd_fix_preview(
            "nosafefix-repo", ["--rule", "F401", "--output-dir", str(out_dir)]
        )
        captured = capsys.readouterr()

        assert rc == 0, captured.err
        assert "no_safe_fix" in captured.out
        assert not (out_dir / "fix.patch").exists()
        report = json.loads((out_dir / "report.json").read_text())
        assert report["status"] == "no_safe_fix"
        assert report["scope"]["target_file"] == "__init__.py"
        assert report["findings"]["before_in_target_file"] >= 1
        assert "unsafe fixes are never applied" in report["notes"][0]

    @requires_ruff
    def test_findings_only_behind_symlink_are_no_findings(
        self, tmp_path, monkeypatch, capsys
    ):
        """Tracked symlinks are not candidates: a repo whose only F401
        finding sits behind a tracked symlink reports no_findings (exit 0),
        never a silent partial fix."""
        repo = tmp_path / "symlink-only-repo"
        repo.mkdir()
        _git(["init"], repo)
        _git(["config", "user.email", "t@t"], repo)
        _git(["config", "user.name", "t"], repo)
        (repo / "ignored").mkdir()
        (repo / "ignored" / "victim.py").write_text(REPO_FILES["mod_a.py"])
        (repo / ".gitignore").write_text("ignored/\n")
        (repo / "link.py").symlink_to(repo / "ignored" / "victim.py")
        _commit_repo(repo)

        workspace = tmp_path / "ws2"
        workspace.mkdir()
        _register(monkeypatch, workspace, repo, "symlink-repo")

        import bin.bluei as bluei_main

        out_dir = tmp_path / "symlink-out"
        rc = bluei_main._cmd_fix_preview(
            "symlink-repo", ["--rule", "F401", "--output-dir", str(out_dir)]
        )
        captured = capsys.readouterr()
        assert rc == 0, captured.err
        assert "no_findings" in captured.out
        report = json.loads((out_dir / "report.json").read_text())
        assert report["status"] == "no_findings"
        assert not (out_dir / "fix.patch").exists()
        assert "symlinks are not candidates" in report["notes"][0]

    @requires_ruff
    def test_weird_filenames_are_discovered_and_fixed(self, tmp_path, monkeypatch):
        """Non-ASCII and spaced filenames survive NUL-parsed ls-files and
        match Ruff's reported paths (regression: core.quotePath mangling
        made these files invisible to discovery)."""
        repo = tmp_path / "weird-repo"
        repo.mkdir()
        _git(["init"], repo)
        _git(["config", "user.email", "t@t"], repo)
        _git(["config", "user.name", "t"], repo)
        (repo / "mod_é.py").write_text(REPO_FILES["mod_a.py"])
        (repo / "we ird.py").write_text(REPO_FILES["mod_a.py"])
        _commit_repo(repo)

        workspace = tmp_path / "ws-weird"
        workspace.mkdir()
        _register(monkeypatch, workspace, repo, "weird-repo")

        import bin.bluei as bluei_main

        out_dir = tmp_path / "weird-out"
        rc = bluei_main._cmd_fix_preview(
            "weird-repo", ["--rule", "F401", "--output-dir", str(out_dir)]
        )
        assert rc == 0
        report = json.loads((out_dir / "report.json").read_text())
        assert report["status"] == "patch_produced"
        assert report["scope"]["target_file"] == "mod_é.py"  # sorted first

        second = tmp_path / "weird-second"
        _git(["clone", "--no-hardlinks", "--no-local", str(repo), str(second)], tmp_path)
        apply_res = subprocess.run(
            ["git", "apply", str(out_dir / "fix.patch")],
            cwd=str(second),
            capture_output=True,
            text=True,
        )
        assert apply_res.returncode == 0, apply_res.stderr
        check = subprocess.run(
            ["ruff", "check", "--isolated", "--select", "F401", "--", "mod_é.py"],
            cwd=str(second),
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0, check.stdout + check.stderr

    @requires_ruff
    def test_detached_head_baseline_is_pinned(self, tmp_path, monkeypatch, capsys):
        """Source detached at a commit no branch points to: the patch must be
        against the detached HEAD, not a branch tip."""
        repo = tmp_path / "detached-repo"
        repo.mkdir()
        _git(["init", "-b", "trunk"], repo)
        _git(["config", "user.email", "t@t"], repo)
        _git(["config", "user.name", "t"], repo)
        (repo / "mod_a.py").write_text(
            "import os\nimport json\nimport sys\n\nprint(sys.path)\n"
        )
        _commit_repo(repo)  # c1: two unused imports
        c1 = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        (repo / "mod_a.py").write_text("import os\nimport sys\n\nprint(sys.path)\n")
        _commit_repo(repo)  # trunk tip (c2): different content
        _git(["checkout", "--detach", c1], repo)  # detached at c1, clean

        workspace = tmp_path / "ws-detached"
        workspace.mkdir()
        _register(monkeypatch, workspace, repo, "detached-repo")

        import bin.bluei as bluei_main

        out_dir = tmp_path / "detached-out"
        rc = bluei_main._cmd_fix_preview(
            "detached-repo", ["--rule", "F401", "--output-dir", str(out_dir)]
        )
        captured = capsys.readouterr()
        assert rc == 0, captured.err
        report = json.loads((out_dir / "report.json").read_text())
        assert report["status"] == "patch_produced"
        assert report["baseline_head"] == c1
        # Both c1-only unused imports are removed (os, json) — proves the
        # fix ran against c1 content, not the trunk tip.
        patch_text = (out_dir / "fix.patch").read_text()
        assert "import json" in patch_text

        second = tmp_path / "detached-second"
        _git(["clone", "--no-hardlinks", "--no-local", str(repo), str(second)], tmp_path)
        _git(["checkout", "--detach", c1], second)
        apply_res = subprocess.run(
            ["git", "apply", str(out_dir / "fix.patch")],
            cwd=str(second),
            capture_output=True,
            text=True,
        )
        assert apply_res.returncode == 0, apply_res.stderr


# ── Environment hardening ───────────────────────────────────────────────────


class TestEnvHardening:
    def test_git_env_whitelist_drops_all_inherited_git_vars(self, monkeypatch):
        for key, value in [
            ("GIT_CONFIG_COUNT", "1"),
            ("GIT_CONFIG_KEY_0", "core.hooksPath"),
            ("GIT_CONFIG_VALUE_0", "/evil/hooks"),
            ("GIT_CONFIG_PARAMETERS", "'core.hooksPath=/evil/hooks'"),
            ("GIT_EXTERNAL_DIFF", "/evil/diff"),
            ("GIT_EXEC_PATH", "/evil/exec"),
            ("GIT_SSH_COMMAND", "/evil/ssh"),
            ("GIT_OBJECT_DIRECTORY", "/evil/objects"),
            ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/evil/alt"),
        ]:
            monkeypatch.setenv(key, value)
        from bluei.engine.patch_preview import _git_env

        env = _git_env()
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_SYSTEM"] == os.devnull
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_OPTIONAL_LOCKS"] == "0"
        for leaked in (
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "GIT_CONFIG_PARAMETERS",
            "GIT_EXTERNAL_DIFF",
            "GIT_EXEC_PATH",
            "GIT_SSH_COMMAND",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        ):
            assert leaked not in env, f"{leaked} leaked into git env"
        # Non-git environment survives.
        assert env["PATH"] == os.environ["PATH"]

    @requires_ruff
    def test_injected_git_config_env_cannot_break_the_run(
        self, preview_env, monkeypatch, capsys
    ):
        """Config injected via GIT_CONFIG_* env vars is ignored end-to-end."""
        ev = preview_env
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/nonexistent-evil-hooks")
        monkeypatch.setenv("GIT_CONFIG_KEY_1", "diff.external")
        monkeypatch.setenv("GIT_CONFIG_VALUE_1", "/nonexistent-evil-diff")
        monkeypatch.setenv("GIT_EXTERNAL_DIFF", "/nonexistent-evil-diff")

        out_dir = ev["fresh_out"]()
        rc = _run_cli(ev, monkeypatch, ["--rule", "F401", "--output-dir", str(out_dir)])
        captured = capsys.readouterr()
        assert rc == 0, captured.err
        report = json.loads((out_dir / "report.json").read_text())
        assert report["status"] == "patch_produced"

    @requires_ruff
    def test_relative_and_empty_path_entries_cannot_shadow_tools(
        self, tmp_path, monkeypatch, capsys
    ):
        """A repo can track executable `git`/`ruff` files. With relative or
        empty PATH entries, a bare tool name resolved by a child running
        inside the clone would execute them. The engine must resolve tools
        to absolute paths and strip relative/empty entries from every child
        PATH — the tracked imposters are never invoked."""
        marker = tmp_path / "imposter-invoked"
        monkeypatch.setenv("BLUEI_FP_MARKER", str(marker))

        repo = tmp_path / "imposter-repo"
        repo.mkdir()
        _git(["init"], repo)
        _git(["config", "user.email", "t@t"], repo)
        _git(["config", "user.name", "t"], repo)
        (repo / "mod.py").write_text(REPO_FILES["mod_a.py"])
        # Tracked executable imposters inside the repository.
        imposter = '#!/bin/sh\nprintf x >> "$BLUEI_FP_MARKER"\nexit 7\n'
        for name in ("git", "ruff"):
            (repo / name).write_text(imposter)
            (repo / name).chmod(0o755)
        _commit_repo(repo)

        workspace = tmp_path / "ws-imposter"
        workspace.mkdir()
        _register(monkeypatch, workspace, repo, "imposter-repo")

        # PATH: relative dir first, an EMPTY entry (means cwd — inside the
        # clone that is the imposter directory), then the real tools.
        monkeypatch.setenv(
            "PATH", "relative-tools" + os.pathsep + os.pathsep + os.environ["PATH"]
        )

        import bin.bluei as bluei_main

        out_dir = tmp_path / "imposter-out"
        rc = bluei_main._cmd_fix_preview(
            "imposter-repo", ["--rule", "F401", "--output-dir", str(out_dir)]
        )
        captured = capsys.readouterr()

        assert rc == 0, captured.err
        report = json.loads((out_dir / "report.json").read_text())
        assert report["status"] == "patch_produced"
        assert report["scope"]["target_file"] == "mod.py"
        # The imposters were never executed.
        assert not marker.exists(), "tracked imposter executable was invoked"

    def test_relative_tool_resolution_rejected(self, tmp_path, monkeypatch):
        """If a needed tool only resolves through a relative PATH entry, the
        run is rejected rather than executed untrusted."""
        rel_dir = tmp_path / "cwd-relative"
        (rel_dir / "relative-tools").mkdir(parents=True)
        # A resolvable-but-relative git: shutil.which returns a relative path.
        fake = rel_dir / "relative-tools" / "git"
        fake.write_text("#!/bin/sh\nexit 7\n")
        fake.chmod(0o755)
        monkeypatch.chdir(rel_dir)
        monkeypatch.setenv("PATH", "relative-tools")

        from bluei.engine.patch_preview import PatchPreviewError, _resolve_tool

        with pytest.raises(PatchPreviewError, match="non-absolute"):
            _resolve_tool("git")


# ── Failure honesty ─────────────────────────────────────────────────────────


class TestArtifactWriteFailure:
    @requires_ruff
    def test_failed_patch_write_retains_recovery_dir(
        self, preview_env, monkeypatch, capsys
    ):
        """A verified fix whose patch artifact cannot be written is never lost."""
        ev = preview_env
        out_dir = ev["fresh_out"]()

        import bluei.engine.patch_preview as pp

        def broken_write(path, content):
            raise OSError("simulated disk full")

        monkeypatch.setattr(pp, "_write_patch_artifact", broken_write)

        rc = _run_cli(ev, monkeypatch, ["--rule", "F401", "--output-dir", str(out_dir)])
        captured = capsys.readouterr()

        assert rc == 1
        assert "artifact_write_failed" in captured.out
        assert "recovery directory retained" in captured.err

        report = json.loads((out_dir / "report.json").read_text())
        assert report["status"] == "artifact_write_failed"
        recovery = Path(report["recovery_dir"])
        assert recovery.is_dir(), "temporary clone must be retained as recovery"
        try:
            # The verified fix survives in the retained clone.
            fixed = recovery / "clone" / "mod_a.py"
            assert fixed.read_text() == "import sys\n\nprint(sys.path)\n"
            assert not (out_dir / "fix.patch").exists()
        finally:
            shutil.rmtree(recovery, ignore_errors=True)

    @requires_ruff
    def test_failed_report_write_exits_nonzero(
        self, preview_env, monkeypatch, capsys
    ):
        """patch+report contract: if report.json cannot be written the run
        must not exit 0, and the already-persisted patch stays on disk."""
        ev = preview_env
        out_dir = ev["fresh_out"]()

        import bluei.engine.patch_preview as pp

        def broken_report(path, data):
            raise OSError("simulated report write failure")

        monkeypatch.setattr(pp, "atomic_json_write", broken_report)

        rc = _run_cli(ev, monkeypatch, ["--rule", "F401", "--output-dir", str(out_dir)])
        captured = capsys.readouterr()

        assert rc == 1, "report write failure must not exit 0"
        assert "could not write report.json" in captured.err
        assert (out_dir / "fix.patch").is_file()  # patch itself is preserved
        assert not (out_dir / "report.json").exists()


class TestFixPreviewToolFailure:
    def test_ruff_crash_reports_tool_error_no_patch(self, tmp_path, monkeypatch, capsys):
        """A ruff binary that fails (rc=2) is reported honestly."""
        repo = tmp_path / "toolfail-repo"
        repo.mkdir()
        _git(["init"], repo)
        _git(["config", "user.email", "t@t"], repo)
        _git(["config", "user.name", "t"], repo)
        (repo / "mod.py").write_text(REPO_FILES["mod_a.py"])
        _commit_repo(repo)

        workspace = tmp_path / "ws3"
        workspace.mkdir()
        shim_dir = tmp_path / "shim-bin"
        shim_dir.mkdir()
        shim = shim_dir / "ruff"
        shim.write_text('#!/bin/sh\necho "simulated ruff crash" >&2\nexit 2\n')
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ["PATH"])
        _register(monkeypatch, workspace, repo, "toolfail-repo")

        import bin.bluei as bluei_main

        out_dir = tmp_path / "toolfail-out"
        rc = bluei_main._cmd_fix_preview(
            "toolfail-repo", ["--rule", "F401", "--output-dir", str(out_dir)]
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert "tool_error" in captured.out
        report = json.loads((out_dir / "report.json").read_text())
        assert report["status"] == "tool_error"
        assert "discovery failed" in report["notes"][0]
        assert not (out_dir / "fix.patch").exists()
        assert report["boundary"]["prs_created"] == 0

    @requires_ruff
    def test_ruff_timeout_during_fix_retains_recovery_dir(
        self, tmp_path, monkeypatch, capsys
    ):
        """A tool timeout AFTER the fix started must not delete the sole
        modified clone: tool_error + retained recovery evidence."""
        repo = tmp_path / "timeout-repo"
        repo.mkdir()
        _git(["init"], repo)
        _git(["config", "user.email", "t@t"], repo)
        _git(["config", "user.name", "t"], repo)
        (repo / "mod.py").write_text(REPO_FILES["mod_a.py"])
        _commit_repo(repo)

        workspace = tmp_path / "ws-timeout"
        workspace.mkdir()

        real_ruff = shutil.which("ruff")
        shim_dir = tmp_path / "slow-bin"
        shim_dir.mkdir()
        shim = shim_dir / "ruff"
        # Behaves like real ruff, but hangs when asked to fix.
        shim.write_text(
            "#!/bin/sh\n"
            "for a in \"$@\"; do\n"
            "  if [ \"$a\" = \"--fix\" ]; then sleep 10; fi\n"
            "done\n"
            f'exec "{real_ruff}" "$@"\n'
        )
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ["PATH"])

        import bin.bluei as bluei_main
        import bluei.engine.patch_preview as pp

        monkeypatch.setattr(pp, "_TOOL_TIMEOUT", 1)
        _register(monkeypatch, workspace, repo, "timeout-repo")

        out_dir = tmp_path / "timeout-out"
        rc = bluei_main._cmd_fix_preview(
            "timeout-repo", ["--rule", "F401", "--output-dir", str(out_dir)]
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert "tool_error" in captured.out
        assert "recovery directory retained" in captured.err
        report = json.loads((out_dir / "report.json").read_text())
        assert report["status"] == "tool_error"
        assert "timeout" in report["notes"][0]
        assert not (out_dir / "fix.patch").exists()
        recovery = Path(report["recovery_dir"])
        assert recovery.is_dir(), "modified clone must be retained on timeout"
        shutil.rmtree(recovery, ignore_errors=True)


# ── Precondition rejections (no ruff required) ──────────────────────────────


class TestFixPreviewRejections:
    def test_dirty_source_rejected(self, preview_env, monkeypatch, capsys):
        ev = preview_env
        out_dir = ev["fresh_out"]()
        (ev["repo_path"] / "mod_a.py").write_text(
            "import os\nimport sys\n\nprint(sys.path)\n# local edit\n"
        )
        rc = _run_cli(ev, monkeypatch, ["--rule", "F401", "--output-dir", str(out_dir)])
        captured = capsys.readouterr()
        assert rc == 2
        assert "dirty" in captured.err
        assert not out_dir.exists()

    def test_untracked_file_counts_as_dirty(self, preview_env, monkeypatch, capsys):
        ev = preview_env
        out_dir = ev["fresh_out"]()
        (ev["repo_path"] / "untracked.py").write_text("import os\n")
        rc = _run_cli(ev, monkeypatch, ["--rule", "F401", "--output-dir", str(out_dir)])
        captured = capsys.readouterr()
        assert rc == 2
        assert "dirty" in captured.err
        assert not out_dir.exists()

    def test_submodules_rejected(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "submodule-repo"
        repo.mkdir()
        _git(["init"], repo)
        _git(["config", "user.email", "t@t"], repo)
        _git(["config", "user.name", "t"], repo)
        (repo / ".gitmodules").write_text(
            '[submodule "vendor"]\n\tpath = vendor\n\turl = https://example.com/v\n'
        )
        (repo / "mod.py").write_text(REPO_FILES["mod_a.py"])
        _commit_repo(repo)
        workspace = tmp_path / "ws-sub"
        workspace.mkdir()
        _register(monkeypatch, workspace, repo, "submodule-repo")

        import bin.bluei as bluei_main

        out_dir = tmp_path / "sub-out"
        rc = bluei_main._cmd_fix_preview(
            "submodule-repo", ["--rule", "F401", "--output-dir", str(out_dir)]
        )
        captured = capsys.readouterr()
        assert rc == 2
        assert "submodules" in captured.err
        assert not out_dir.exists()

    def test_preexisting_output_dir_rejected(self, preview_env, monkeypatch, capsys):
        ev = preview_env
        out_dir = ev["fresh_out"]()
        out_dir.mkdir()
        rc = _run_cli(ev, monkeypatch, ["--rule", "F401", "--output-dir", str(out_dir)])
        captured = capsys.readouterr()
        assert rc == 2
        assert "already exists" in captured.err
        assert list(out_dir.iterdir()) == []

    def test_output_dir_inside_repo_rejected(self, preview_env, monkeypatch, capsys):
        ev = preview_env
        out_dir = ev["repo_path"] / "preview-out"
        rc = _run_cli(ev, monkeypatch, ["--rule", "F401", "--output-dir", str(out_dir)])
        captured = capsys.readouterr()
        assert rc == 2
        assert "outside the source checkout" in captured.err
        assert not out_dir.exists()

    def test_unregistered_project_rejected(self, preview_env, monkeypatch, capsys):
        ev = preview_env
        rc = _run_cli(
            ev,
            monkeypatch,
            ["--rule", "F401", "--output-dir", str(ev["fresh_out"]())],
            name="no-such-project",
        )
        captured = capsys.readouterr()
        assert rc == 2
        assert "not found" in captured.err

    @pytest.mark.parametrize(
        "bad_rule", ["f401", "F40", "F1", "W291", "FETCHHEAD", "F401;rm -rf /"]
    )
    def test_malformed_or_non_f401_rule_rejected(
        self, preview_env, monkeypatch, capsys, bad_rule
    ):
        ev = preview_env
        out_dir = ev["fresh_out"]()
        rc = _run_cli(ev, monkeypatch, ["--rule", bad_rule, "--output-dir", str(out_dir)])
        captured = capsys.readouterr()
        assert rc == 2
        assert ("malformed rule" in captured.err) or (
            "not supported for fix-preview" in captured.err
        )
        assert not out_dir.exists()

    def test_unsupported_rule_message_shows_allowlist(self, preview_env, monkeypatch, capsys):
        ev = preview_env
        out_dir = ev["fresh_out"]()
        rc = _run_cli(ev, monkeypatch, ["--rule", "E501", "--output-dir", str(out_dir)])
        captured = capsys.readouterr()
        assert rc == 2
        assert "not supported for fix-preview" in captured.err
        assert "F401" in captured.err  # allowlist is shown
        assert not out_dir.exists()

    def test_no_ruff_on_path_rejected(self, preview_env, monkeypatch, tmp_path, capsys):
        ev = preview_env
        binlite = tmp_path / "binlite"
        binlite.mkdir()
        os.symlink(shutil.which("git"), binlite / "git")
        monkeypatch.setenv("PATH", str(binlite))
        out_dir = ev["fresh_out"]()
        rc = _run_cli(ev, monkeypatch, ["--rule", "F401", "--output-dir", str(out_dir)])
        captured = capsys.readouterr()
        assert rc == 2
        assert "ruff" in captured.err.lower()
        assert not out_dir.exists()


# ── CLI option parsing (public surface, in-process) ─────────────────────────


class TestFixPreviewCliParsing:
    def _reject(self, ev, monkeypatch, capsys, args, expect):
        rc = _run_cli(ev, monkeypatch, args)
        captured = capsys.readouterr()
        assert rc == 2, (rc, captured.out, captured.err)
        assert expect in captured.err

    def test_missing_rule(self, preview_env, monkeypatch, capsys):
        self._reject(
            preview_env, monkeypatch, capsys,
            ["--output-dir", str(preview_env["fresh_out"]())],
            "'--rule' is required",
        )

    def test_missing_output_dir(self, preview_env, monkeypatch, capsys):
        self._reject(
            preview_env, monkeypatch, capsys,
            ["--rule", "F401"],
            "'--output-dir' is required",
        )

    def test_flag_without_value(self, preview_env, monkeypatch, capsys):
        self._reject(
            preview_env, monkeypatch, capsys,
            ["--rule", "--output-dir", "x"],
            "needs a value",
        )

    def test_duplicate_flag(self, preview_env, monkeypatch, capsys):
        out = str(preview_env["fresh_out"]())
        self._reject(
            preview_env, monkeypatch, capsys,
            ["--rule", "F401", "--rule", "W291", "--output-dir", out],
            "more than once",
        )

    def test_unknown_flag(self, preview_env, monkeypatch, capsys):
        self._reject(
            preview_env, monkeypatch, capsys,
            ["--rule", "F401", "--output-dir", "x", "--turbo"],
            "unknown option '--turbo'",
        )

    @pytest.mark.parametrize(
        "flag_args",
        [
            ["--live"],
            ["--fix-engine", "deterministic"],
            ["--dry-run"],
            ["--no-dry-run"],
            ["--phase", "pr-cycle"],
        ],
        ids=lambda v: v[0],
    )
    def test_contradictory_flags_rejected(self, preview_env, monkeypatch, capsys, flag_args):
        args = ["--rule", "F401", "--output-dir", str(preview_env["fresh_out"]())] + flag_args
        self._reject(
            preview_env, monkeypatch, capsys, args,
            "not applicable to fix-preview",
        )

    def test_extra_positional_rejected(self, preview_env, monkeypatch, capsys):
        self._reject(
            preview_env, monkeypatch, capsys,
            ["--rule", "F401", "--output-dir", "x", "stray-arg"],
            "unexpected argument 'stray-arg'",
        )


# ── Pure engine-boundary unit tests (unconditional) ─────────────────────────


class TestEngineUnits:
    def test_validate_rule_table(self):
        from bluei.engine.patch_preview import (
            ALLOWED_RULES,
            PatchPreviewError,
            validate_rule,
        )

        assert list(ALLOWED_RULES) == ["F401"]  # first proof is F401-only
        assert validate_rule("F401") == "F401"
        for bad in [
            "", "f401", "F40", "F99990", "F401;", "E501", "W291", "I001",
            "RUF100", None, "F 1",
        ]:
            with pytest.raises(PatchPreviewError) as exc:
                validate_rule(bad)
            assert exc.value.exit_code == 2

    def test_validate_output_dir_rules(self, tmp_path):
        from bluei.engine.patch_preview import PatchPreviewError, validate_output_dir

        source = tmp_path / "src"
        source.mkdir()

        exists = tmp_path / "exists-dir"
        exists.mkdir()
        with pytest.raises(PatchPreviewError, match="already exists"):
            validate_output_dir(exists, source)

        inside = source / "nested" / "out"
        with pytest.raises(PatchPreviewError, match="outside the source checkout"):
            validate_output_dir(inside, source)

        ok = tmp_path / "brand-new-out"
        assert validate_output_dir(ok, source) == ok.resolve()


# ── Public script entry point (subprocess, CLI-shape only) ──────────────────


class TestPublicScript:
    def _bluei(self, *args):
        return subprocess.run(
            [sys.executable, str(BLUEI_BIN), *args],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_main_help_lists_fix_preview(self):
        result = self._bluei("help")
        assert result.returncode == 0
        assert "fix-preview" in result.stdout

    def test_command_help_shows_options_and_exit_codes(self):
        result = self._bluei("help", "fix-preview")
        assert result.returncode == 0
        for token in ("--rule", "--output-dir", "F401", "Exit codes"):
            assert token in result.stdout

    def test_fix_preview_dash_h(self):
        result = self._bluei("fix-preview", "-h")
        assert result.returncode == 0
        assert "--output-dir" in result.stdout

    def test_fix_preview_rejects_live_flag_via_script(self, tmp_path):
        result = self._bluei(
            "fix-preview",
            "ghost-project",
            "--rule",
            "F401",
            "--output-dir",
            str(tmp_path / "out"),
            "--live",
        )
        assert result.returncode == 2
        assert "not applicable to fix-preview" in result.stderr

    def test_fix_preview_requires_rule_via_script(self, tmp_path):
        result = self._bluei(
            "fix-preview", "ghost-project", "--output-dir", str(tmp_path / "out")
        )
        assert result.returncode == 2
        assert "'--rule' is required" in result.stderr
