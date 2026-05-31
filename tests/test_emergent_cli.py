import json
import subprocess
import sys
from pathlib import Path


def test_bluei_emergent_propose_list_and_show(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "findings.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "finding_id": finding_id,
                    "repo": "demo",
                    "path": path,
                    "line": 10,
                    "rule": "llm-typed-dict",
                    "snippet": "prefer TypedDict here",
                    "confidence": 0.85,
                    "quick_win": False,
                    "safe_to_autofix": False,
                }
            )
            for finding_id, path in [
                ("f-1", "src/api/users.py"),
                ("f-2", "src/api/orders.py"),
            ]
        )
        + "\n"
    )
    root = Path(__file__).resolve().parent.parent

    propose = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "emergent",
            "propose",
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--min-observations",
            "2",
            "--run-id",
            "run-test",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert propose.returncode == 0
    assert "created=1 updated=0 skipped=0" in propose.stdout
    rules_file = state_dir / "emergent_rules.json"
    payload = json.loads(rules_file.read_text())
    rule_id = payload["rules"][0]["rule_id"]

    listed = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "emergent",
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

    assert listed.returncode == 0
    assert rule_id in listed.stdout
    assert "proposed" in listed.stdout
    assert "llm-typed-dict" in listed.stdout

    shown = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "emergent",
            "show",
            rule_id,
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

    assert shown.returncode == 0
    assert f"Rule: {rule_id}" in shown.stdout
    assert "Status: proposed" in shown.stdout
    assert "Pattern: llm-typed-dict in src/api/**" in shown.stdout

    validated = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "emergent",
            "validate",
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

    assert validated.returncode == 0
    assert "candidate=1 rejected=0 unchanged=0" in validated.stdout

    relisted = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "emergent",
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

    assert "candidate" in relisted.stdout

    shadow = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "emergent",
            "shadow",
            rule_id,
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--matches",
            "4",
            "--false-positives",
            "0",
            "--min-shadow-runs",
            "1",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert shadow.returncode == 0
    assert "status=active" in shadow.stdout


def test_bluei_emergent_list_handles_empty_state(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    (repos_dir / "demo" / "state").mkdir(parents=True)
    root = Path(__file__).resolve().parent.parent

    result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "emergent",
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

    assert result.returncode == 0
    assert "No emergent rules found." in result.stdout


def test_bluei_emergent_propose_from_fix_patterns(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "fix_patterns.jsonl").write_text(
        json.dumps(
            {
                "pattern_id": "fp-test123456",
                "rule": "broad-except",
                "language": "python",
                "file_path": "src/api/users.py",
                "before_snippet": "except:",
                "after_snippet": "except Exception:",
                "diff_patch": "-except:\n+except Exception:",
                "confidence": 0.9,
                "success_count": 5,
                "failure_count": 0,
                "skip_count": 0,
                "source": "autofix",
                "created_at": "2026-05-18T00:00:00+00:00",
                "last_used_at": None,
                "last_verified_at": None,
                "source_finding_ids": ["f-1"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parent.parent

    result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "emergent",
            "propose",
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--from-patterns",
            "--min-success-count",
            "5",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "created=1 updated=0 skipped=0" in result.stdout
    payload = json.loads((state_dir / "emergent_rules.json").read_text())
    assert payload["rules"][0]["detection_pattern"]["search_pattern"] == "broad-except"


def test_bluei_emergent_scan_reports_shadow_matches(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    target = worktree / "src" / "api" / "users.py"
    target.parent.mkdir(parents=True)
    target.write_text("def handle():\n    llm-typed-dict\n", encoding="utf-8")
    (state_dir / "emergent_rules.json").write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {
                        "rule_id": "er-shadow",
                        "header": "Repeated typed dict",
                        "detection_pattern": {
                            "detection_type": "text_pattern",
                            "search_pattern": "llm-typed-dict",
                            "file_glob": "src/api/**",
                        },
                        "language": "python",
                        "category": "type",
                        "status": "candidate",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parent.parent

    result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "emergent",
            "scan",
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--worktree",
            str(worktree),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Emergent scan: matches=1" in result.stdout
    assert "er-shadow src/api/users.py:2 llm-typed-dict" in result.stdout


def test_bluei_emergent_reject_and_retire_update_rule_status(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "emergent_rules.json").write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {
                        "rule_id": "er-lifecycle",
                        "header": "Lifecycle rule",
                        "detection_pattern": {
                            "detection_type": "text_pattern",
                            "search_pattern": "llm-typed-dict",
                            "file_glob": "src/api/**",
                        },
                        "language": "python",
                        "category": "type",
                        "status": "candidate",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parent.parent

    rejected = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "emergent",
            "reject",
            "er-lifecycle",
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--reason",
            "too_noisy",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert rejected.returncode == 0
    assert "status=rejected" in rejected.stdout

    retired = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "emergent",
            "retire",
            "er-lifecycle",
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

    assert retired.returncode == 0
    assert "status=retired" in retired.stdout
    payload = json.loads((state_dir / "emergent_rules.json").read_text())
    assert payload["rules"][0]["status"] == "retired"
    assert payload["rules"][0]["rejected_reason"] == "superseded"


def test_bluei_emergent_discover_writes_active_rule_findings(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    target = worktree / "src" / "api" / "users.py"
    target.parent.mkdir(parents=True)
    target.write_text("def handle():\n    llm-typed-dict\n", encoding="utf-8")
    (state_dir / "emergent_rules.json").write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {
                        "rule_id": "er-active",
                        "header": "Repeated typed dict",
                        "detection_pattern": {
                            "detection_type": "text_pattern",
                            "search_pattern": "llm-typed-dict",
                            "file_glob": "src/api/**",
                        },
                        "language": "python",
                        "category": "type",
                        "status": "active",
                        "confidence": 0.82,
                        "severity_default": "low",
                    },
                    {
                        "rule_id": "er-candidate",
                        "header": "Candidate typed dict",
                        "detection_pattern": {
                            "detection_type": "text_pattern",
                            "search_pattern": "llm-typed-dict",
                            "file_glob": "src/api/**",
                        },
                        "language": "python",
                        "category": "type",
                        "status": "candidate",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parent.parent

    first = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "emergent",
            "discover",
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--worktree",
            str(worktree),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    second = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "emergent",
            "discover",
            "--repo",
            "demo",
            "--state-root",
            str(repos_dir),
            "--worktree",
            str(worktree),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert first.returncode == 0
    assert "Emergent discover: findings=1 written=1" in first.stdout
    assert second.returncode == 0
    assert "Emergent discover: findings=1 written=0" in second.stdout
    findings = [
        json.loads(line)
        for line in (state_dir / "findings.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(findings) == 1
    assert findings[0]["rule"] == "emergent:er-active"
    assert findings[0]["path"] == "src/api/users.py"
    assert findings[0]["category"] == "type"


def test_bluei_emergent_approve_activates_rule(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    import json
    rules_data = {
        "version": 1,
        "rules": [
            {
                "rule_id": "er-approve-test",
                "header": "Test rule",
                "detection_pattern": {"detection_type": "text_pattern", "search_pattern": "test-rule"},
                "language": "python",
                "category": "lint",
                "status": "candidate",
            }
        ],
    }
    (state_dir / "emergent_rules.json").write_text(json.dumps(rules_data), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable, str(Path(__file__).parent.parent / "bin" / "bluei.py"),
            "emergent", "approve", "er-approve-test",
            "--repo", "demo",
            "--state-root", str(repos_dir),
        ],
        cwd=str(Path(__file__).parent.parent),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    updated = json.loads((state_dir / "emergent_rules.json").read_text())
    rule = next(r for r in updated["rules"] if r["rule_id"] == "er-approve-test")
    assert rule["status"] == "active"


def test_bluei_emergent_gc_retires_stale(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    import json
    rules_data = {
        "version": 1,
        "rules": [
            {
                "rule_id": "er-old",
                "header": "Old rule",
                "detection_pattern": {"detection_type": "text_pattern", "search_pattern": "old"},
                "language": "python",
                "category": "lint",
                "status": "active",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "rule_id": "er-new",
                "header": "New rule",
                "detection_pattern": {"detection_type": "text_pattern", "search_pattern": "new"},
                "language": "python",
                "category": "lint",
                "status": "active",
                "updated_at": "2026-05-19T00:00:00+00:00",
            },
        ],
    }
    (state_dir / "emergent_rules.json").write_text(json.dumps(rules_data), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable, str(Path(__file__).parent.parent / "bin" / "bluei.py"),
            "emergent", "gc",
            "--repo", "demo",
            "--state-root", str(repos_dir),
        ],
        cwd=str(Path(__file__).parent.parent),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    updated = json.loads((state_dir / "emergent_rules.json").read_text())
    old_rule = next(r for r in updated["rules"] if r["rule_id"] == "er-old")
    new_rule = next(r for r in updated["rules"] if r["rule_id"] == "er-new")
    assert old_rule["status"] == "retired"
    assert new_rule["status"] == "active"
