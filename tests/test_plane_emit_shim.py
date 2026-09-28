"""#1485 fold — pytest wrapper for tests/test_plane_emit.sh.

That bash suite is the ONLY pin on the ladder itself: which refusals fall
back, which pass through, and that a replayed batch carries the id the daemon
already saw. Nothing ran it. pytest collects `tests/test_*.py`, so a `.sh`
sibling is invisible to CI and to the local before/after recipe in CLAUDE.md —
the #1485 shim change shipped with two new cases in a file no gate opened.
Precedent for wrapping a shell gate this way: tests/test_debounce_recipient_harness.py.

The suite is hermetic (fake unix-socket daemon, `PLANE_EMIT_CLI` recorder
stub, no network, no claudlobby import), so this is a plain rc assertion plus
the suite's own completion marker — rc 0 alone would let an early `exit 0`
masquerade as a pass. The caller supplies the exact checkout's selected CLI;
the shell suite owns its data root, socket, and recorder override.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import constructed_env

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITE = REPO_ROOT / "tests" / "test_plane_emit.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not installed"
)


def test_the_shim_ladder_suite_passes(test_cli):
    env = constructed_env(CLAUDLOBBY_CLI=test_cli)
    run = subprocess.run(
        ["bash", str(SUITE)],
        capture_output=True, text=True, timeout=600,
        cwd=str(REPO_ROOT), env=env,
    )
    tail = "\n".join((run.stdout + run.stderr).splitlines()[-40:])
    assert run.returncode == 0, f"rc={run.returncode}\n{tail}"
    # The suite prints this only after its last case; a bare rc 0 could also
    # mean it exited early.
    assert "PASS: all plane-emit shim tests passed" in run.stdout, tail


@pytest.mark.parametrize("socket_rc,expected_rc", [(0, 0), (5, 127)])
def test_missing_cold_cli_is_checked_only_after_socket_failure(
    tmp_path, socket_rc, expected_rc
):
    """A functioning daemon must survive a broken selected CLI installation."""
    native = tmp_path / "native"
    native.mkdir()
    for name in ("plane-emit.sh", "cli-context.sh"):
        shutil.copyfile(REPO_ROOT / "lib" / name, native / name)
    # Replace only the transport peer: the test exercises the shim's branch
    # and validation order; the shell suite above tests real socket replies.
    (native / "plane-socket-client.py").write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[sys.argv.index('--finalize-to') + 1]).write_text('{}')\n"
        "print('SOCKET-ATTEMPT')\n"
        f"sys.exit({socket_rc})\n"
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").symlink_to(sys.executable)
    root = tmp_path / "data"
    (root / "state" / "plane").mkdir(parents=True)
    env = constructed_env(
        CLAUDLOBBY_ROOT=root,
        CLAUDLOBBY_CLI=tmp_path / "missing-cli",
        PLANE_EMIT_DISABLED="0",
    )
    env["PATH"] = f"{bindir}:{env['PATH']}"
    run = subprocess.run(
        ["/bin/bash", str(native / "plane-emit.sh")],
        input="{}", capture_output=True, text=True, timeout=10, env=env,
    )
    assert run.returncode == expected_rc, run.stderr
    assert run.stdout == "SOCKET-ATTEMPT\n"
    if socket_rc == 0:
        assert run.stderr == ""
    else:
        assert "CLAUDLOBBY_CLI" in run.stderr
        assert "cold CLI rung failed rc=127" in run.stderr


@pytest.mark.parametrize("root", [None, "relative/data"])
def test_shim_refuses_missing_or_relative_data_root(root):
    env = constructed_env(PLANE_EMIT_DISABLED="0")
    if root is not None:
        env["CLAUDLOBBY_ROOT"] = root
    run = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "lib" / "plane-emit.sh")],
        input="{}", capture_output=True, text=True, timeout=10, env=env,
    )
    assert run.returncode == 127
    assert "CLAUDLOBBY_ROOT" in run.stderr
    assert run.stdout == ""
