"""Verify lifecycle scripts agree on unit/session names for a mock bot.

Regression test for the class of bug where lifecycle scripts (keepalive-all,
reconcile-fleet, fleet-pulse) disagree on unit names after a service_prefix
rename. The invariant: BOT_SERVICE (from bot.conf) drives unit file lookups;
the directory basename drives tmux session names. These must differ when
service_prefix is set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap

import pytest


# Always resolve relative to this file's location so the test uses the
# checkout's lib-common.sh, not the shared install's.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB_DIR = os.path.join(_REPO_ROOT, "lib")


@pytest.fixture
def mock_bot_dir(tmp_path):
    """Create a minimal bot directory with bot.conf and a .service file."""
    bot_dir = tmp_path / "alpha"
    bot_dir.mkdir()
    (bot_dir / "logs").mkdir()
    (bot_dir / "data" / "events").mkdir(parents=True)

    bot_conf = textwrap.dedent("""\
        export CLAUDLOBBY_ROOT={root}
        export BOT_ID=alpha
        BOT_NAME=alpha
        BOT_SERVICE=com.test.eng.alpha
        BOT_LABEL=ALPHA
        BOT_DIR="{bot_dir}"
        FLEET_NAME=test-fleet
        SERVICE_PREFIX=com.test.eng
    """).format(root=tmp_path, bot_dir=bot_dir)
    (bot_dir / "bot.conf").write_text(bot_conf)

    # Create a dummy .service file matching BOT_SERVICE
    (bot_dir / "com.test.eng.alpha.service").write_text("[Unit]\nDescription=test\n")

    return bot_dir


def _run_bash(script, env=None):
    """Run a bash snippet, return stdout."""
    merged_env = {**os.environ, **(env or {})}
    r = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=merged_env,
        timeout=10,
    )
    return r.stdout.strip(), r.stderr.strip(), r.returncode


class TestLifecycleNameAgreement:
    """All lifecycle scripts must resolve the same unit name for a given bot."""

    def test_bot_conf_get_reads_bot_service(self, mock_bot_dir):
        stdout, _, rc = _run_bash(
            f'. "{LIB_DIR}/lib-common.sh"; bot_conf_get "{mock_bot_dir}" BOT_SERVICE fallback'
        )
        assert rc == 0
        assert stdout == "com.test.eng.alpha"

    def test_bot_conf_get_fallback(self, mock_bot_dir):
        stdout, _, rc = _run_bash(
            f'. "{LIB_DIR}/lib-common.sh"; bot_conf_get "{mock_bot_dir}" NONEXISTENT "myfallback"'
        )
        assert rc == 0
        assert stdout == "myfallback"

    def test_tmux_session_name_uses_directory_basename(self, mock_bot_dir):
        stdout, _, rc = _run_bash(
            f'. "{LIB_DIR}/lib-common.sh"; tmux_session_name "{mock_bot_dir}"'
        )
        assert rc == 0
        assert stdout == "alpha"

    def test_unit_name_differs_from_directory_name(self, mock_bot_dir):
        """The core invariant: BOT_SERVICE != directory basename when
        service_prefix is set. Scripts must use BOT_SERVICE for units
        and directory basename for tmux sessions."""
        svc, _, _ = _run_bash(
            f'. "{LIB_DIR}/lib-common.sh"; bot_conf_get "{mock_bot_dir}" BOT_SERVICE ""'
        )
        session, _, _ = _run_bash(
            f'. "{LIB_DIR}/lib-common.sh"; tmux_session_name "{mock_bot_dir}"'
        )

        assert svc == "com.test.eng.alpha", (
            "BOT_SERVICE should be the full prefixed name"
        )
        assert session == "alpha", "tmux session should be the directory basename"
        assert svc != session, (
            "unit name and session name must differ when prefix is set"
        )

    def test_service_file_matches_bot_service(self, mock_bot_dir):
        """The .service file in the bot dir must match BOT_SERVICE from bot.conf."""
        svc, _, _ = _run_bash(
            f'. "{LIB_DIR}/lib-common.sh"; bot_conf_get "{mock_bot_dir}" BOT_SERVICE ""'
        )
        expected_file = mock_bot_dir / f"{svc}.service"
        assert expected_file.exists(), f"Expected {expected_file} to exist"

    def test_stale_unit_not_matched(self, mock_bot_dir):
        """A stale .service file (old naming) should NOT be the one scripts pick up."""
        # Create a stale unit file with the old naming convention
        (mock_bot_dir / "alpha.service").write_text("[Unit]\nDescription=stale\n")

        svc, _, _ = _run_bash(
            f'. "{LIB_DIR}/lib-common.sh"; bot_conf_get "{mock_bot_dir}" BOT_SERVICE ""'
        )
        # The correct unit is com.test.eng.alpha.service, not alpha.service
        assert svc == "com.test.eng.alpha"
        assert (mock_bot_dir / f"{svc}.service").exists()

    def test_bot_conf_get_handles_quoted_values(self, mock_bot_dir):
        """Values with double quotes should be stripped."""
        stdout, _, rc = _run_bash(
            f'. "{LIB_DIR}/lib-common.sh"; bot_conf_get "{mock_bot_dir}" BOT_DIR ""'
        )
        assert rc == 0
        assert stdout == str(mock_bot_dir)

    def test_bot_conf_get_missing_conf_returns_default(self):
        """Missing bot.conf returns the default value."""
        stdout, _, rc = _run_bash(
            f'. "{LIB_DIR}/lib-common.sh"; bot_conf_get "/nonexistent/path" BOT_SERVICE "myfallback"'
        )
        assert rc == 0
        assert stdout == "myfallback"


class TestKeepaliveAllArgConvention:
    """The composed keepalive unit's ExecStart passes the fleet NAME
    (`keepalive-all.sh <fleet>`), matching every other fleet job. Before the
    Phase 6 fix keepalive-all treated $1 as a bots DIR, so the composed unit
    FATALed on every tick — caught by the migration's verification gate."""

    def _run(self, root, arg):
        # keepalive-all resolves its worker script from CLAUDLOBBY_ROOT — give
        # the tmp root a real lib/ so only the arg semantics are under test.
        libdir = os.path.join(root, "lib")
        os.makedirs(libdir, exist_ok=True)
        for script in ("keepalive.sh", "lib-common.sh"):
            shutil.copy2(os.path.join(LIB_DIR, script), os.path.join(libdir, script))
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDLOBBY_FLEET", "FLEET_NAME")}
        env["CLAUDLOBBY_ROOT"] = str(root)
        return subprocess.run(
            ["bash", os.path.join(LIB_DIR, "keepalive-all.sh"), arg],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_fleet_name_arg_resolves_overlay_bots_dir(self, tmp_path):
        bots = tmp_path / "local" / "f9" / "runtime" / "bots"
        bots.mkdir(parents=True)
        r = self._run(tmp_path, "f9")
        assert r.returncode == 0, r.stderr

    def test_absolute_dir_arg_still_honored(self, tmp_path):
        bots = tmp_path / "elsewhere" / "bots"
        bots.mkdir(parents=True)
        r = self._run(tmp_path, str(bots))
        assert r.returncode == 0, r.stderr

    def test_unknown_fleet_name_still_fatals(self, tmp_path):
        r = self._run(tmp_path, "ghost-fleet")
        assert r.returncode == 1
