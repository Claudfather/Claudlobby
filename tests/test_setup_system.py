"""Tests for lib/setup-system — cross-platform host bootstrap + host-job enrollment."""

import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "lib", "setup-system")


def test_setup_system_exists():
    """Script exists and is executable."""
    assert os.path.isfile(SCRIPT)
    assert os.access(SCRIPT, os.X_OK)


def test_setup_system_sources_lib_common():
    """Script sources lib-common.sh for shared helpers."""
    with open(SCRIPT) as f:
        content = f.read()
    assert "lib-common.sh" in content


def test_setup_system_dry_run():
    """--dry-run completes without error on current OS."""
    result = subprocess.run(
        [SCRIPT, "--dry-run"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "dry-run" in result.stdout.lower() or "dry-run" in result.stderr.lower()


def test_setup_system_dry_run_reaches_host_jobs_phase():
    """The host-jobs phase runs in sequence (the 9-phase pipeline is intact)."""
    result = subprocess.run(
        [SCRIPT, "--dry-run"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "phase 8/9: host jobs" in result.stdout
    assert "phase 9/9: summary" in result.stdout


def test_setup_system_has_help():
    """--help shows usage information."""
    result = subprocess.run(
        [SCRIPT, "--help"], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0
    assert "setup-system" in result.stdout


def test_setup_system_set_euo_pipefail():
    """Script uses strict bash mode."""
    with open(SCRIPT) as f:
        content = f.read()
    assert "set -euo pipefail" in content


def test_setup_system_enrolls_via_generic_enrollers():
    """Host jobs flow through the shared enrollment spine, not inline logic."""
    with open(SCRIPT) as f:
        content = f.read()
    assert "install_fleet_timer.sh" in content
    assert "install_fleet_timer_launchd.sh" in content
