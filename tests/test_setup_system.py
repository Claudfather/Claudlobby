"""Tests for lib/setup-system — cross-platform host bootstrap + host-job enrollment."""

import json
import os
import re
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "lib", "setup-system")


@pytest.fixture(scope="module")
def dry_run(setup_system_dry_run):
    """Delegates to the session-scoped fixture in conftest.

    Kept as a thin alias so this module's existing tests read unchanged, while
    the actual `--dry-run` runs once per session rather than once per module —
    a second module now asserts against the same output.
    """
    return setup_system_dry_run


def test_setup_system_exists():
    """Script exists and is executable."""
    assert os.path.isfile(SCRIPT)
    assert os.access(SCRIPT, os.X_OK)


def test_setup_system_sources_lib_common():
    """Script sources lib-common.sh for shared helpers."""
    with open(SCRIPT) as f:
        content = f.read()
    assert "lib-common.sh" in content


def test_setup_system_dry_run(dry_run):
    """--dry-run completes without error on current OS."""
    assert dry_run.returncode == 0, f"stderr: {dry_run.stderr}"
    assert "dry-run" in dry_run.stdout.lower() or "dry-run" in dry_run.stderr.lower()


def test_setup_system_dry_run_reaches_host_jobs_phase(dry_run):
    """The host-jobs phase runs in sequence (the 10-phase pipeline is intact)."""
    assert dry_run.returncode == 0, f"stderr: {dry_run.stderr}"
    assert "phase 9/10: host jobs" in dry_run.stdout
    assert "phase 10/10: summary" in dry_run.stdout


def test_setup_system_dry_run_reaches_managed_settings_phase(dry_run):
    """The managed-settings phase runs in sequence and would write the approvals.

    The approval list is inline in the script, so the phase always has
    something to write; --dry-run must report the target + count and touch
    nothing. The list's contents are checked by test_managed_channel_approvals.
    """
    assert dry_run.returncode == 0, f"stderr: {dry_run.stderr}"
    assert "phase 7/10: managed settings" in dry_run.stdout
    assert "would write" in dry_run.stdout
    assert "channel-plugin approvals" in dry_run.stdout


def test_managed_channel_approvals_lists_official_and_fork_telegram():
    """The inline allowedChannelPlugins list is valid JSON and — because setting
    it REPLACES claude-code's built-in ledger — must carry BOTH the official
    marketplace (else official telegram de-approves) and the fork whose inbound
    we are keeping alive."""
    with open(SCRIPT, encoding="utf-8") as fh:
        m = re.search(r"MANAGED_CHANNEL_APPROVALS='(\{.*\})'", fh.read())
    assert m, "MANAGED_CHANNEL_APPROVALS assignment not found in setup-system"
    payload = json.loads(m.group(1))
    markets = {
        e["marketplace"]
        for e in payload["allowedChannelPlugins"]
        if e["plugin"] == "telegram"
    }
    assert "claude-plugins-official" in markets
    assert "claudfather-plugins" in markets


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
