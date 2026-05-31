import json
import subprocess
import sys
from pathlib import Path


def test_bluei_campaign_plan_dry_run_outputs_plan(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    findings_file = state_dir / "findings.jsonl"
    findings_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "finding_id": "f-1",
                        "repo": "demo",
                        "path": "src/api/users.ts",
                        "line": 10,
                        "rule": "type-explicit-any",
                        "snippet": "let value: any",
                        "confidence": 0.9,
                        "quick_win": True,
                        "safe_to_autofix": True,
                    }
                ),
                json.dumps(
                    {
                        "finding_id": "f-2",
                        "repo": "demo",
                        "path": "src/api/orders.ts",
                        "line": 3,
                        "rule": "discount-math-sign",
                        "snippet": "total - discount",
                        "confidence": 0.9,
                        "quick_win": True,
                        "safe_to_autofix": True,
                    }
                ),
            ]
        )
        + "\n"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "plan",
            "--repo",
            "demo",
            "--rules",
            "discount-math-sign,type-explicit-any",
            "--paths",
            "src/api/**",
            "--state-root",
            str(repos_dir),
        ],
        cwd=Path(__file__).resolve().parent.parent,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Campaign plan:" in result.stdout
    assert "Phase 1" in result.stdout
    assert "discount-math-sign" in result.stdout
    assert "type-explicit-any" in result.stdout
    assert not (state_dir / "campaigns").exists()


def test_bluei_campaign_save_list_and_status(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "findings.jsonl").write_text(
        json.dumps(
            {
                "finding_id": "f-1",
                "repo": "demo",
                "path": "src/api/users.ts",
                "line": 10,
                "rule": "type-explicit-any",
                "snippet": "let value: any",
                "confidence": 0.9,
                "quick_win": True,
                "safe_to_autofix": True,
            }
        )
        + "\n"
    )

    root = Path(__file__).resolve().parent.parent
    save_result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "plan",
            "--repo",
            "demo",
            "--rules",
            "type-explicit-any",
            "--paths",
            "src/api/**",
            "--state-root",
            str(repos_dir),
            "--save",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert save_result.returncode == 0
    assert "Saved:" in save_result.stdout
    campaign_id = save_result.stdout.split("Saved:", 1)[1].splitlines()[0].strip()
    assert (state_dir / "campaigns" / campaign_id / "campaign.json").exists()

    list_result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "list",
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert list_result.returncode == 0
    assert campaign_id in list_result.stdout
    assert "planning" in list_result.stdout

    status_result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "status",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert status_result.returncode == 0
    assert f"Campaign: {campaign_id}" in status_result.stdout
    assert "Phase 1" in status_result.stdout


def test_bluei_campaign_run_dry_run_updates_saved_status(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "findings.jsonl").write_text(
        json.dumps(
            {
                "finding_id": "f-1",
                "repo": "demo",
                "path": "src/api/users.ts",
                "line": 10,
                "rule": "type-explicit-any",
                "snippet": "let value: any",
                "confidence": 0.9,
                "quick_win": True,
                "safe_to_autofix": True,
            }
        )
        + "\n"
    )
    root = Path(__file__).resolve().parent.parent

    save_result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "plan",
            "--repo",
            "demo",
            "--rules",
            "type-explicit-any",
            "--paths",
            "src/api/**",
            "--state-root",
            str(repos_dir),
            "--save",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    campaign_id = save_result.stdout.split("Saved:", 1)[1].splitlines()[0].strip()

    run_result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "run",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--dry-run",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert run_result.returncode == 0
    assert "Dry-run complete" in run_result.stdout
    assert "phases=1" in run_result.stdout
    assert "fixed=0 failed=0 skipped=1" in run_result.stdout

    status_result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "status",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert "Status: completed" in status_result.stdout
    assert "Progress: fixed=0 failed=0 skipped=1" in status_result.stdout
    assert "Phase 1: Fix type-explicit-any findings in src/api/users.ts [completed]" in status_result.stdout
    assert "fixed=0 failed=0 skipped=1)" in status_result.stdout

    events_result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "events",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--limit",
            "3",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert events_result.returncode == 0
    assert f"Events: {campaign_id}" in events_result.stdout
    assert "phase.completed" in events_result.stdout
    assert "campaign.completed" in events_result.stdout
    assert "campaign.created" not in events_result.stdout

    pause_completed = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "pause",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert pause_completed.returncode == 1
    assert "cannot pause completed campaign" in pause_completed.stderr

    resume_completed = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "resume",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert resume_completed.returncode == 1
    assert "cannot resume completed campaign" in resume_completed.stderr

    abort_completed = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "abort",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert abort_completed.returncode == 1
    assert "cannot abort completed campaign" in abort_completed.stderr


def test_bluei_campaign_run_requires_dry_run_flag(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    root = Path(__file__).resolve().parent.parent

    result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "run",
            "camp-missing",
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "requires --dry-run" in result.stderr


def test_bluei_campaign_run_allow_mutate_requires_worktree(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    root = Path(__file__).resolve().parent.parent

    result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "run",
            "camp-missing",
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--allow-mutate",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "requires --worktree" in result.stderr


def test_bluei_campaign_run_allow_mutate_requires_git_worktree(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "findings.jsonl").write_text(
        json.dumps(
            {
                "finding_id": "f-1",
                "repo": "demo",
                "path": "src/app.py",
                "line": 1,
                "rule": "trailing-whitespace",
                "snippet": "value = 1  ",
                "confidence": 0.9,
                "quick_win": True,
                "safe_to_autofix": True,
            }
        )
        + "\n"
    )
    root = Path(__file__).resolve().parent.parent
    save_result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "plan",
            "--repo",
            "demo",
            "--rules",
            "trailing-whitespace",
            "--paths",
            "src/**",
            "--state-root",
            str(repos_dir),
            "--save",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    campaign_id = save_result.stdout.split("Saved:", 1)[1].splitlines()[0].strip()
    worktree = tmp_path / "plain-dir"
    worktree.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "run",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--allow-mutate",
            "--worktree",
            str(worktree),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "requires a clean git worktree" in result.stderr


def test_bluei_campaign_run_allow_mutate_applies_autofix_in_clean_worktree(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    src_dir = worktree / "src"
    src_dir.mkdir(parents=True)
    target = src_dir / "app.py"
    target.write_text("value = 1  \n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=worktree, text=True, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "bluei@example.invalid"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "bluei"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(["git", "add", "src/app.py"], cwd=worktree, text=True, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    (state_dir / "findings.jsonl").write_text(
        json.dumps(
            {
                "finding_id": "f-1",
                "repo": "demo",
                "path": "src/app.py",
                "line": 1,
                "rule": "trailing-whitespace",
                "snippet": "value = 1  ",
                "confidence": 0.9,
                "quick_win": True,
                "safe_to_autofix": True,
            }
        )
        + "\n"
    )
    root = Path(__file__).resolve().parent.parent
    save_result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "plan",
            "--repo",
            "demo",
            "--rules",
            "trailing-whitespace",
            "--paths",
            "src/**",
            "--state-root",
            str(repos_dir),
            "--save",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    campaign_id = save_result.stdout.split("Saved:", 1)[1].splitlines()[0].strip()

    result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "run",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--allow-mutate",
            "--worktree",
            str(worktree),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Run complete" in result.stdout
    assert "status=completed" in result.stdout
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    campaign = json.loads((state_dir / "campaigns" / campaign_id / "campaign.json").read_text())
    assert campaign["findings_fixed"] == 1


def test_bluei_campaign_pause_resume_and_abort(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "findings.jsonl").write_text(
        json.dumps(
            {
                "finding_id": "f-1",
                "repo": "demo",
                "path": "src/api/users.ts",
                "line": 10,
                "rule": "type-explicit-any",
                "snippet": "let value: any",
                "confidence": 0.9,
                "quick_win": True,
                "safe_to_autofix": True,
            }
        )
        + "\n"
    )
    root = Path(__file__).resolve().parent.parent

    save_result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "plan",
            "--repo",
            "demo",
            "--rules",
            "type-explicit-any",
            "--paths",
            "src/api/**",
            "--state-root",
            str(repos_dir),
            "--save",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    campaign_id = save_result.stdout.split("Saved:", 1)[1].splitlines()[0].strip()

    pause_result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "pause",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--reason",
            "operator check",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert pause_result.returncode == 0
    assert "Paused:" in pause_result.stdout

    paused_status = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "status",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert "Pause reason: operator check" in paused_status.stdout
    assert "Last event: campaign.paused" in paused_status.stdout

    resume_result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "resume",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert resume_result.returncode == 0
    assert "Resumed:" in resume_result.stdout

    abort_result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "abort",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--reason",
            "superseded",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert abort_result.returncode == 0
    assert "Aborted:" in abort_result.stdout

    resume_aborted = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "resume",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert resume_aborted.returncode == 1
    assert "cannot resume aborted campaign" in resume_aborted.stderr

    run_aborted = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "campaign",
            "run",
            campaign_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--dry-run",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert run_aborted.returncode == 1
    assert "campaign is aborted" in run_aborted.stderr

    campaign_file = state_dir / "campaigns" / campaign_id / "campaign.json"
    campaign = json.loads(campaign_file.read_text())
    assert campaign["status"] == "aborted"
    assert campaign["abort_reason"] == "superseded"

    events = [
        json.loads(line)
        for line in (state_dir / "campaigns" / campaign_id / "events.jsonl").read_text().splitlines()
    ]
    assert [event["event"] for event in events][-3:] == [
        "campaign.paused",
        "campaign.resumed",
        "campaign.aborted",
    ]
    assert events[-3]["payload"]["reason"] == "operator check"
    assert events[-1]["payload"]["reason"] == "superseded"
