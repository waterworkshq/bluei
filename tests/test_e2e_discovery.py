"""test_e2e_discovery.py — End-to-end tests for the finding discovery pipeline."""

import subprocess
from pathlib import Path

import pytest

from bluei.engine.constants import DETECTOR_CATALOG
from bluei.engine.models import Finding
from bluei.engine.orchestrator import (
    _ast_scan_python_files,
    discover_findings,
)


def _create_qa_sandbox_tree(tmp_path: Path) -> Path:
    repo = tmp_path / "sandbox"
    repo.mkdir()

    (repo / "price.py").write_text(
        "def calculate_discount(amount, discount):\n    return amount + discount\n",
        encoding="utf-8",
    )

    qa = repo / "src" / "qa_sandbox"
    qa.mkdir(parents=True)

    (qa / "__init__.py").write_text("", encoding="utf-8")
    (qa / "catalog.py").write_text(
        "def find_item(items, query):\n"
        "    for item in items:\n"
        "        if item == query:\n"
        "            return item\n"
        "    return None\n",
        encoding="utf-8",
    )

    (qa / "orders.py").write_text(
        "class Order:\n"
        "    def compute_tax(self):\n"
        "        try:\n"
        "            result = int(self.subtotal * self.tax_rate)\n"
        "        except Exception:\n"
        "            result = 0\n"
        "        return result\n",
        encoding="utf-8",
    )

    (qa / "notifications.py").write_text(
        "def normalize_email(value):\n    return value.lower()\n",
        encoding="utf-8",
    )

    inv_code = (
        "def reserve_stock(stock, sku, quantity):\n"
        "    if stock[sku] < quantity:\n"
        "        return False\n"
        "    pending = []\n"
        "    while pending:\n"
        "        item = pending.pop(0)\n"
        "        process(item)\n"
        "    stock[sku] -= quantity\n"
        "    return True\n"
    )
    (qa / "inventory.py").write_text(inv_code, encoding="utf-8")

    (qa / "analytics.py").write_text(
        "def count_unique(events):\n"
        "    seen = []\n"
        "    for e in events:\n"
        "        if e not in seen:\n"
        "            seen.append(e)\n"
        "    return len(seen)\n",
        encoding="utf-8",
    )

    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "report_health.py").write_text(
        "from pathlib import Path\nstate_file = Path('/tmp/qa_sandbox/state.json')\n",
        encoding="utf-8",
    )

    docs = repo / "docs"
    docs.mkdir()
    (docs / "ARCHITECTURE.md").write_text(
        "# Architecture\n\nSee legacy_pricer.py for pricing logic.\n",
        encoding="utf-8",
    )
    (docs / "OPERATIONS.md").write_text(
        "# Operations\n\n## Deployment\n\nDeploy via CI.\n",
        encoding="utf-8",
    )
    (docs / "TROUBLESHOOTING.md").write_text(
        "# Troubleshooting\n\nCheck legacy_pricer.py first.\n",
        encoding="utf-8",
    )

    (repo / "README.md").write_text(
        "# Project\n\nRun tests:\n\n    pytest -q\n",
        encoding="utf-8",
    )

    return repo


def _create_violations_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "violations"
    repo.mkdir()

    (repo / "broad.py").write_text(
        "try:\n    do_work()\nexcept Exception:\n    pass\n",
        encoding="utf-8",
    )

    (repo / "tmp_path.py").write_text(
        "from pathlib import Path\nout = Path('/tmp/output.log')\n",
        encoding="utf-8",
    )

    (repo / "perf.py").write_text(
        "items = [1, 2, 3]\nwhile items:\n    x = items.pop(0)\n    print(x)\n",
        encoding="utf-8",
    )

    (repo / "membership.py").write_text(
        "seen = []\nfor x in data:\n    if x not in seen:\n        seen.append(x)\n",
        encoding="utf-8",
    )

    return repo


class TestASTScanBroadExcept:
    def test_finds_broad_except(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "bug.py").write_text(
            "try:\n    pass\nexcept Exception:\n    pass\n",
            encoding="utf-8",
        )
        rule_meta = {e["rule"]: e for e in DETECTOR_CATALOG}
        findings = _ast_scan_python_files(repo, tmp_path / "log.txt", rule_meta)
        broad = [f for f in findings if "broad" in f.rule]
        assert len(broad) >= 1
        assert broad[0].path == "bug.py"


class TestASTScanHardcodedTmp:
    def test_finds_hardcoded_tmp(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "tmp_file.py").write_text(
            "from pathlib import Path\np = Path('/tmp/data.txt')\n",
            encoding="utf-8",
        )
        rule_meta = {e["rule"]: e for e in DETECTOR_CATALOG}
        findings = _ast_scan_python_files(repo, tmp_path / "log.txt", rule_meta)
        tmp_findings = [f for f in findings if "tmp" in f.rule]
        assert len(tmp_findings) >= 1


class TestASTScanPerfPopFront:
    def test_finds_perf_pop_front(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "slow.py").write_text(
            "q = [1, 2, 3]\nwhile q:\n    x = q.pop(0)\n    print(x)\n",
            encoding="utf-8",
        )
        rule_meta = {e["rule"]: e for e in DETECTOR_CATALOG}
        findings = _ast_scan_python_files(repo, tmp_path / "log.txt", rule_meta)
        pop_findings = [f for f in findings if "pop" in f.rule]
        assert len(pop_findings) >= 1
        assert pop_findings[0].path == "slow.py"


class TestDiscoverFindingsMultipleDetectors:
    def test_full_pipeline_multiple_findings(self, tmp_path, git_commit_all):
        repo = _create_qa_sandbox_tree(tmp_path)
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        git_commit_all(repo, "init")
        log = tmp_path / "run.log"
        findings = discover_findings(repo, log_file=log)
        assert len(findings) >= 3
        rules = {f.rule for f in findings}
        assert "broad-except" in rules or "discount-math-sign" in rules
        for f in findings:
            assert isinstance(f, Finding)


class TestDeduplication:
    def test_no_duplicate_findings(self, tmp_path, git_commit_all):
        repo = _create_qa_sandbox_tree(tmp_path)
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        git_commit_all(repo, "init")
        log = tmp_path / "run.log"
        findings = discover_findings(repo, log_file=log)
        seen = set()
        for f in findings:
            key = (f.path, f.line, f.rule)
            assert key not in seen, f"duplicate finding: {key}"
            seen.add(key)


class TestEmptyRepo:
    def test_empty_repo_no_findings(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        (repo / "clean.py").write_text("x = 1\ny = 2\nz = x + y\n", encoding="utf-8")
        tests = repo / "tests"
        tests.mkdir()
        (tests / "test_notifications.py").write_text(
            "def test_placeholder(): pass\n", encoding="utf-8"
        )
        log = tmp_path / "run.log"
        findings = discover_findings(repo, log_file=log)
        assert len(findings) == 0


class TestFindingsHaveRequiredFields:
    def test_all_fields_present(self, tmp_path, git_commit_all):
        repo = _create_violations_repo(tmp_path)
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        git_commit_all(repo, "init")
        log = tmp_path / "run.log"
        findings = discover_findings(repo, log_file=log)
        if not findings:
            pytest.skip("no findings produced (ruff/AST may be unavailable)")
        for f in findings:
            assert f.finding_id, "missing finding_id"
            assert f.rule, "missing rule"
            assert f.path, "missing path"
            assert f.line > 0, f"invalid line: {f.line}"
            assert isinstance(f.snippet, str) and len(f.snippet) > 0, "missing snippet"
