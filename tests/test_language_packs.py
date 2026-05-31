from pathlib import Path
import subprocess
import sys

from bluei.engine.orchestrator import discover_findings
from bluei.engine.plugin_loader import detect_repo_languages, run_plugin_discovery


def test_detect_repo_languages_includes_script_docs_and_container_files(tmp_path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "deploy.sh").write_text("echo $name\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM ubuntu\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Title\n", encoding="utf-8")

    languages = detect_repo_languages(tmp_path)

    assert "shell" in languages
    assert "dockerfile" in languages
    assert "markdown" in languages


def test_run_plugin_discovery_loads_shell_docker_and_markdown_packs(tmp_path) -> None:
    (tmp_path / "deploy.sh").write_text("echo $name\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM ubuntu\nUSER root\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Title  \n", encoding="utf-8")

    findings = run_plugin_discovery(tmp_path)
    rules = {finding.rule for finding in findings}

    assert "shell-SC2086" in rules
    assert "docker-DL3006" in rules
    assert "docker-DL3002" in rules
    assert "md-MD009" in rules


def test_discover_findings_merges_language_pack_findings(tmp_path) -> None:
    (tmp_path / "deploy.sh").write_text("echo $name\n", encoding="utf-8")

    findings = discover_findings(tmp_path, log_file=tmp_path / "bluei.log")

    assert any(finding.rule == "shell-SC2086" for finding in findings)


def test_bluei_languages_lists_language_packs() -> None:
    root = Path(__file__).resolve().parent.parent

    result = subprocess.run(
        [sys.executable, str(Path("bin/bluei.py")), "languages"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "plugin-rust" in result.stdout
    assert "clippy-unwrap-used" in result.stdout
    assert "plugin-shell" in result.stdout
    assert "shell-SC2086" in result.stdout
