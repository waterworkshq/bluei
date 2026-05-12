"""Onboarding E2E test — walk the full flow on a temp repo.

Proves Ceph can:
1. Onboard a fresh repo (language detection, config generation)
2. Run an issue-cycle scan (linter discovery)
3. Produce findings, issues, and a health score
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CEPH_CLI = ROOT / "bin" / "ceph"
QA_AGENT = ROOT / "qa-agent"


def _run(*args, workdir: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run the ceph CLI or qa-agent directly and return the result."""
    # Use qa-agent directly for subcommand control in tests
    cmd = [sys.executable, str(QA_AGENT)] + list(args)
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=workdir or ROOT, timeout=timeout,
    )
    return result


def _create_minimal_python_repo(path: Path) -> None:
    """Create a minimal Python project with ruff-detectable issues."""
    path.mkdir(parents=True, exist_ok=True)

    # pyproject.toml with ruff config
    (path / "pyproject.toml").write_text("""\
[project]
name = "test-ceph-onboarding"
version = "0.1.0"
requires-python = ">=3.11"

[tool.ruff.lint]
select = ["E", "F", "B", "S", "I"]
""")

    # Python file with various issues
    (path / "main.py").write_text("""\
import os
import sys
import json


def compute(x):
    unused = 42
    return x * 2


def process(data):
    result = {}
    for item in data:
        if item == None:
            return None
        result[item] = compute(item)
        return result


class DataHandler:
    def __init__(self, config):
        self.config = config
        pass

    def handle(self):
        try:
            return self._do_work()
        except:
            return None

    def _do_work(self):
        # Bare except above is bad practice
        return {"status": "ok"}
""")

    # A second file for breadth
    (path / "utils.py").write_text("""\
import os
import sys
from pathlib import Path


def load_config(path):
    if not Path(path).exists():
        return {}
    content = open(path).read()
    return json.loads(content)


class DefaultHandler:
    def __init__(self, **kwargs):
        self.settings = kwargs
        self._init_db()

    def _init_db(self):
        pass

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def validate(self):
        for k, v in self.settings.items():
            if v is None:
                raise ValueError(f"{k} is None")
            try:
                int(v)
            except (TypeError, ValueError):
                pass

    def close(self):
        pass

    def flush(self):
        pass

    def rollback(self):
        pass
""")

    # Init git repo
    subprocess.run(["git", "init"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@ceph.dev"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Ceph Test"], cwd=path, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=path, capture_output=True)


# ── Tests ──


class TestOnboardingE2E:
    """Full onboarding flow on a generated temp Python repo."""

    def setup_method(self):
        self.test_dir = Path("/tmp/ceph-e2e-test")
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.repo_path = self.test_dir / "source-repo"
        _create_minimal_python_repo(self.repo_path)
        # Clean any leftover state from a previous run
        state_dir = ROOT / "repos" / "ceph-e2e-test"
        if state_dir.exists():
            shutil.rmtree(state_dir)

    def teardown_method(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)

    def _get_state_dir(self, repo_name: str = "ceph-e2e-test") -> Path:
        return ROOT / "repos" / repo_name / "state"

    def test_01_onboard_detects_language(self):
        """Onboarding should detect Python and generate config."""
        result = _run(
            "onboard", "--repo", str(self.repo_path),
            "--name", "ceph-e2e-test",
            "--mode", "observe",
            "--profile", "conservative",
        )
        assert result.returncode == 0, f"onboard failed: {result.stderr}"

        # Check config was generated
        config_path = ROOT / "repos" / "ceph-e2e-test" / "config.yaml"
        assert config_path.exists(), "config.yaml not generated"

        config_text = config_path.read_text()
        assert "language: python" in config_text, f"Python not detected in config:\n{config_text}"
        assert "mode: observe" in config_text, "observe mode not set"

    def test_02_scan_discovers_findings(self):
        """Issue-cycle should discover ruff findings."""
        # Onboard first
        _run(
            "onboard", "--repo", str(self.repo_path),
            "--name", "ceph-e2e-test",
            "--mode", "observe",
            "--profile", "conservative",
        )

        # Run issue-cycle (dry-run to avoid GitHub issues)
        result = _run(
            "run", "--repo", "ceph-e2e-test",
            "--phase", "issue-cycle", "--dry-run",
        )
        assert result.returncode == 0, f"issue-cycle failed: {result.stderr}"

        # Check output for findings
        assert "findings" in result.stdout.lower(), f"No findings produced:\n{result.stdout}"
        assert "Findings:" in result.stdout, f"Missing Findings count:\n{result.stdout}"

    def test_03_issues_file_created(self):
        """After scan, issues.json should exist with expected data."""
        _run(
            "onboard", "--repo", str(self.repo_path),
            "--name", "ceph-e2e-test",
            "--mode", "observe",
            "--profile", "conservative",
        )
        _run(
            "run", "--repo", "ceph-e2e-test",
            "--phase", "issue-cycle", "--dry-run",
        )

        issues_file = self._get_state_dir() / "issues.json"
        assert issues_file.exists(), "issues.json not found"

        data = json.loads(issues_file.read_text())
        assert "issues" in data, f"issues.json missing 'issues' key: {data}"
        issues = data["issues"]
        assert len(issues) > 0, f"No issues created: {data}"
        assert any(
            i.get("status") == "open" for i in issues
        ), "No open issues found"

    def test_04_health_score_written(self):
        """Status.json should contain health_score in current_counts."""
        _run(
            "onboard", "--repo", str(self.repo_path),
            "--name", "ceph-e2e-test",
            "--mode", "observe",
            "--profile", "conservative",
        )
        _run(
            "run", "--repo", "ceph-e2e-test",
            "--phase", "issue-cycle", "--dry-run",
        )

        status_file = self._get_state_dir() / "status.json"
        assert status_file.exists(), "status.json not found"

        data = json.loads(status_file.read_text())
        counts = data.get("current_counts", {})
        health = counts.get("health_score")
        assert health is not None, f"health_score missing in current_counts: {counts}"
        assert isinstance(health, (int, float)), f"health_score not numeric: {health}"
        assert 0 <= health <= 100, f"health_score out of bounds: {health}"

    def test_05_findings_jsonl_written(self):
        """findings.jsonl should contain detected findings."""
        _run(
            "onboard", "--repo", str(self.repo_path),
            "--name", "ceph-e2e-test",
            "--mode", "observe",
            "--profile", "conservative",
        )
        _run(
            "run", "--repo", "ceph-e2e-test",
            "--phase", "issue-cycle", "--dry-run",
        )

        findings_file = self._get_state_dir() / "findings.jsonl"
        assert findings_file.exists(), "findings.jsonl not found"
        lines = [l for l in findings_file.read_text().splitlines() if l.strip()]
        assert len(lines) > 0, "findings.jsonl is empty"
        # Verify each line is valid JSON
        for line in lines:
            record = json.loads(line)
            assert "finding_id" in record, f"Finding missing finding_id: {record}"
