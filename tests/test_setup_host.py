"""Tests for lib/setup-host.sh cross-platform host bootstrap script."""
import subprocess
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "lib", "setup-host.sh")


def test_setup_host_exists():
    """Script exists and is executable."""
    assert os.path.isfile(SCRIPT)
    assert os.access(SCRIPT, os.X_OK)


def test_setup_host_sources_lib_common():
    """Script sources lib-common.sh for shared helpers."""
    with open(SCRIPT) as f:
        content = f.read()
    assert "lib-common.sh" in content


def test_setup_host_dry_run():
    """--dry-run completes without error on current OS."""
    result = subprocess.run(
        [SCRIPT, "--dry-run"],
        capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "dry-run" in result.stdout.lower() or "dry-run" in result.stderr.lower()


def test_setup_host_has_help():
    """--help shows usage information."""
    result = subprocess.run(
        [SCRIPT, "--help"],
        capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0


def test_setup_host_set_euo_pipefail():
    """Script uses strict bash mode."""
    with open(SCRIPT) as f:
        content = f.read()
    assert "set -euo pipefail" in content
