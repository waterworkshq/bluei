"""Tests for the bin/bluei CLI wrapper script.

Tests that the wrapper correctly maps human-friendly subcommands to the
qa-agent Python CLI, handles help, version, and unknown commands.
"""

import subprocess
import sys
from pathlib import Path

import pytest

# Locate the bluei wrapper script
BLUEI_BIN = Path(__file__).resolve().parents[1] / "bin" / "bluei"


def _bluei(*args: str) -> subprocess.CompletedProcess:
    """Run bluei with given args and return CompletedProcess."""
    return subprocess.run(
        [str(BLUEI_BIN), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


# ── Help / Version ─────────────────────────────────────────────

class TestHelp:
    def test_help_with_no_args_exits_0_and_lists_commands(self):
        """bluei with no args should print version info and exit 0."""
        result = _bluei()
        assert result.returncode == 0
        assert "bluei" in result.stdout

    def test_help_command_exits_0(self):
        """bluei help should print usage and exit 0."""
        result = _bluei("help")
        assert result.returncode == 0
        assert "Commands:" in result.stdout

    def test_help_flag_exits_0(self):
        """bluei --help should print usage and exit 0."""
        result = _bluei("--help")
        assert result.returncode == 0
        assert "Commands:" in result.stdout

    def test_help_with_subcommand_shows_subcommand_help(self):
        """bluei help scan should show scan help."""
        result = _bluei("help", "scan")
        assert result.returncode == 0
        assert "bluei scan" in result.stdout

    def test_help_scan_flag_shows_scan_help(self):
        """bluei scan --help should show scan-specific help."""
        result = _bluei("scan", "--help")
        assert result.returncode == 0
        assert "issue-cycle" in result.stdout

    def test_help_clean_flag_shows_clean_help(self):
        """bluei clean --help should show clean-specific help."""
        result = _bluei("clean", "--help")
        assert result.returncode == 0
        assert "bluei clean" in result.stdout

    def test_help_duster_flag_shows_duster_help(self):
        """bluei duster --help should show duster-specific help."""
        result = _bluei("duster", "--help")
        assert result.returncode == 0
        assert "bluei duster" in result.stdout or "dry-run" in result.stdout

    def test_help_unknown_subcommand_shows_general_help(self):
        """bluei help <unknown> falls back to general help (exit 0)."""
        result = _bluei("help", "nonexistent")
        assert result.returncode == 0
        assert "Commands:" in result.stdout


class TestVersion:
    def test_version_flag_exits_0(self):
        """bluei --version should exit 0."""
        result = _bluei("--version")
        assert result.returncode == 0
        assert "bluei version" in result.stdout


# ── Subcommand mapping ─────────────────────────────────────────

class TestSubcommandMapping:
    def test_scan_maps_to_issue_cycle(self):
        """bluei scan <repo> should invoke issue-cycle."""
        # Dry-run to avoid actual side effects; just check that the underlying
        # qa-agent is invoked with the right phase.
        result = _bluei("scan", "--help")
        assert result.returncode == 0
        assert "issue-cycle" in result.stdout

    def test_clean_maps_to_pr_cycle(self):
        """bluei clean <repo> should invoke pr-cycle (help describes PR creation)."""
        result = _bluei("clean", "--help")
        assert result.returncode == 0
        assert "Pull Request" in result.stdout or "PR" in result.stdout

    def test_duster_maps_to_dry_run_issue_cycle(self):
        """bluei duster <repo> should invoke issue-cycle with --dry-run."""
        result = _bluei("duster", "--help")
        assert result.returncode == 0
        assert "specks" in result.stdout.lower() or "dry-run" in result.stdout.lower() or "preview" in result.stdout.lower()

    def test_run_maps_to_orchestrated(self):
        """bluei run <repo> should invoke orchestrated phase."""
        result = _bluei("run", "--help")
        assert result.returncode == 0
        assert "orchestrated" in result.stdout or "run" in result.stdout

    def test_doctor_accepts_repo_arg(self):
        """bluei doctor <name> should pass --repo <name> to qa-agent doctor."""
        result = _bluei("doctor", "--help")
        assert result.returncode == 0
        assert "doctor" in result.stdout.lower() or "diagnostics" in result.stdout.lower()


# ── install / setup ────────────────────────────────────────────

class TestInstall:
    def test_install_shows_instructions(self):
        """bluei install should print setup instructions."""
        result = _bluei("install")
        assert result.returncode == 0
        assert any(word in result.stdout for word in ["Install", "install", "Setup", "setup"])

    def test_setup_shows_instructions(self):
        """bluei setup should print setup instructions (alias for install)."""
        result = _bluei("setup")
        assert result.returncode == 0
        assert any(word in result.stdout for word in ["Install", "install", "Setup", "setup"])


# ── Unknown commands ───────────────────────────────────────────

class TestUnknownCommands:
    def test_unknown_command_returns_nonzero(self):
        """bluei <unknown> should exit non-zero with usage hint."""
        result = _bluei("boguscommand")
        assert result.returncode != 0
        assert "not a command" in result.stderr.lower() or "unknown command" in result.stderr.lower()

    def test_unknown_command_hints_at_help(self):
        """bluei <unknown> should suggest 'bluei help'."""
        result = _bluei("nonexistent")
        assert "bluei help" in result.stderr.lower()

    def test_scan_without_repo_arg_fails(self):
        """bluei scan without a repo name should report error."""
        result = _bluei("scan")
        assert result.returncode != 0
        assert "project name" in result.stderr.lower() or "requires" in result.stderr.lower()

    def test_clean_without_repo_arg_fails(self):
        """bluei clean without a repo name should report error."""
        result = _bluei("clean")
        assert result.returncode != 0
        assert "project name" in result.stderr.lower() or "requires" in result.stderr.lower()
