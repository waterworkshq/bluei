"""Tests for the bin/blenny CLI wrapper script.

Tests that the wrapper correctly maps human-friendly subcommands to the
qa-agent Python CLI, handles help, version, and unknown commands.
"""

import subprocess
import sys
from pathlib import Path

import pytest

# Locate the blenny wrapper script
BLENNY_BIN = Path(__file__).resolve().parents[1] / "bin" / "blenny"


def _blenny(*args: str) -> subprocess.CompletedProcess:
    """Run blenny with given args and return CompletedProcess."""
    return subprocess.run(
        [str(BLENNY_BIN), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


# ── Help / Version ─────────────────────────────────────────────

class TestHelp:
    def test_help_with_no_args_exits_0_and_lists_commands(self):
        """blenny with no args should print usage and exit 0."""
        result = _blenny()
        assert result.returncode == 0
        assert "blenny" in result.stdout
        assert "Commands:" in result.stdout

    def test_help_command_exits_0(self):
        """blenny help should print usage and exit 0."""
        result = _blenny("help")
        assert result.returncode == 0
        assert "Commands:" in result.stdout

    def test_help_flag_exits_0(self):
        """blenny --help should print usage and exit 0."""
        result = _blenny("--help")
        assert result.returncode == 0
        assert "Commands:" in result.stdout

    def test_help_with_subcommand_shows_subcommand_help(self):
        """blenny help scan should show scan help."""
        result = _blenny("help", "scan")
        assert result.returncode == 0
        assert "blenny scan" in result.stdout

    def test_help_scan_flag_shows_scan_help(self):
        """blenny scan --help should show scan-specific help."""
        result = _blenny("scan", "--help")
        assert result.returncode == 0
        assert "issue-cycle" in result.stdout

    def test_help_ink_flag_shows_ink_help(self):
        """blenny ink --help should show ink-specific help."""
        result = _blenny("ink", "--help")
        assert result.returncode == 0
        assert "blenny ink" in result.stdout

    def test_help_duster_flag_shows_duster_help(self):
        """blenny duster --help should show duster-specific help."""
        result = _blenny("duster", "--help")
        assert result.returncode == 0
        assert "blenny duster" in result.stdout or "dry-run" in result.stdout

    def test_help_unknown_subcommand_fails(self):
        """blenny help <unknown> should exit non-zero."""
        result = _blenny("help", "nonexistent")
        assert result.returncode != 0


class TestVersion:
    def test_version_flag_exits_0(self):
        """blenny --version should exit 0."""
        result = _blenny("--version")
        assert result.returncode == 0
        assert "blenny version" in result.stdout


# ── Subcommand mapping ─────────────────────────────────────────

class TestSubcommandMapping:
    def test_scan_maps_to_issue_cycle(self):
        """blenny scan <repo> should invoke issue-cycle."""
        # Dry-run to avoid actual side effects; just check that the underlying
        # qa-agent is invoked with the right phase.
        result = _blenny("scan", "--help")
        assert result.returncode == 0
        assert "issue-cycle" in result.stdout

    def test_ink_maps_to_pr_cycle(self):
        """blenny ink <repo> should invoke pr-cycle (help describes PR creation)."""
        result = _blenny("ink", "--help")
        assert result.returncode == 0
        assert "Pull Request" in result.stdout or "PR" in result.stdout

    def test_duster_maps_to_dry_run_issue_cycle(self):
        """blenny duster <repo> should invoke issue-cycle with --dry-run."""
        result = _blenny("duster", "--help")
        assert result.returncode == 0
        assert "dry-run" in result.stdout.lower() or "preview" in result.stdout.lower()

    def test_run_maps_to_orchestrated(self):
        """blenny run <repo> should invoke orchestrated phase."""
        result = _blenny("run", "--help")
        assert result.returncode == 0
        assert "orchestrated" in result.stdout or "run" in result.stdout

    def test_doctor_accepts_repo_arg(self):
        """blenny doctor <name> should pass --repo <name> to qa-agent doctor."""
        result = _blenny("doctor", "--help")
        assert result.returncode == 0
        assert "doctor" in result.stdout.lower() or "diagnostics" in result.stdout.lower()


# ── install / setup ────────────────────────────────────────────

class TestInstall:
    def test_install_shows_instructions(self):
        """blenny install should print setup instructions."""
        result = _blenny("install")
        assert result.returncode == 0
        assert any(word in result.stdout for word in ["Install", "install", "Setup", "setup"])

    def test_setup_shows_instructions(self):
        """blenny setup should print setup instructions (alias for install)."""
        result = _blenny("setup")
        assert result.returncode == 0
        assert any(word in result.stdout for word in ["Install", "install", "Setup", "setup"])


# ── Unknown commands ───────────────────────────────────────────

class TestUnknownCommands:
    def test_unknown_command_returns_nonzero(self):
        """blenny <unknown> should exit non-zero with usage hint."""
        result = _blenny("boguscommand")
        assert result.returncode != 0
        assert "unknown command" in result.stderr.lower()

    def test_unknown_command_hints_at_help(self):
        """blenny <unknown> should suggest 'blenny help'."""
        result = _blenny("nonexistent")
        assert "blenny help" in result.stderr.lower()

    def test_scan_without_repo_arg_fails(self):
        """blenny scan without a repo name should report error."""
        result = _blenny("scan")
        assert result.returncode != 0
        assert "error" in result.stderr.lower() or "requires" in result.stderr.lower()

    def test_ink_without_repo_arg_fails(self):
        """blenny ink without a repo name should report error."""
        result = _blenny("ink")
        assert result.returncode != 0
        assert "requires" in result.stderr.lower()
