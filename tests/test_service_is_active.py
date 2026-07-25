"""Tests for the `service_is_active` lib-common.sh helper (#742 / #710 Part B).

`service_is_active <bot_service>` is the cross-platform supervised-service
liveness probe behind fleet-pulse's `service_down` check: rc 0 iff the service
is active, non-zero when confirmed down. It OS-dispatches
`systemctl --user is-active` on Linux and `launchctl print gui/<uid>/<label>` on
macOS.

Both branches are driven here on a single Linux host by stubbing the probe
binary on PATH and forcing `_OS` after sourcing — so CI exercises the REAL macOS
launchd branch logic, not a mock of the helper. The cross-stub tests
(`*_dispatches_to_*`) additionally prove each OS branch calls the RIGHT binary:
stub the two probes to DISAGREE and assert the verdict follows the OS-correct
one.
"""

import os
import subprocess
from pathlib import Path


LIB_COMMON = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"


def _service_is_active(
    svc: str, force_os: str, tmp_path: Path, *, systemctl_rc=None, launchctl_rc=None
) -> int:
    """Source lib-common.sh, force `_OS`, and run `service_is_active <svc>` with
    the named probe binaries stubbed on PATH (each exits its given rc). A probe
    left as None is not stubbed. HOME is pinned away from the real ~/.env.
    Returns the helper's exit code.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, rc in (("systemctl", systemctl_rc), ("launchctl", launchctl_rc)):
        if rc is not None:
            stub = bindir / name
            stub.write_text(f"#!/bin/sh\nexit {rc}\n")
            stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; _OS="$3"; service_is_active "$2"',
            "_",
            str(LIB_COMMON),
            svc,
            force_os,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    return proc.returncode


# --- Linux branch (systemctl --user is-active) -------------------------------


def test_linux_active_is_rc0(tmp_path):
    rc = _service_is_active("com.x.b1", "Linux", tmp_path, systemctl_rc=0)
    assert rc == 0


def test_linux_inactive_is_down(tmp_path):
    # systemctl is-active exits 3 for an inactive/failed unit — any non-zero is down.
    rc = _service_is_active("com.x.b1", "Linux", tmp_path, systemctl_rc=3)
    assert rc != 0


# --- macOS branch (launchctl print gui/<uid>/<label>) ------------------------


def test_darwin_loaded_is_rc0(tmp_path):
    rc = _service_is_active("com.x.b1", "Darwin", tmp_path, launchctl_rc=0)
    assert rc == 0


def test_darwin_not_bootstrapped_is_down(tmp_path):
    # launchctl print exits non-zero when the Label is not bootstrapped → down.
    rc = _service_is_active("com.x.b1", "Darwin", tmp_path, launchctl_rc=1)
    assert rc != 0


# --- dispatch: each OS branch must call the RIGHT binary ----------------------


def test_darwin_dispatches_to_launchctl_not_systemctl(tmp_path):
    """Stubs disagree: systemctl says active (0), launchctl says down (1). On
    Darwin the verdict must follow launchctl → down. Proves the branch does not
    fall through to the Linux probe."""
    rc = _service_is_active(
        "com.x.b1", "Darwin", tmp_path, systemctl_rc=0, launchctl_rc=1
    )
    assert rc != 0


def test_linux_dispatches_to_systemctl_not_launchctl(tmp_path):
    """Mirror: systemctl says down (3), launchctl says active (0). On Linux the
    verdict must follow systemctl → down."""
    rc = _service_is_active(
        "com.x.b1", "Linux", tmp_path, systemctl_rc=3, launchctl_rc=0
    )
    assert rc != 0


# --- fail-safe: an unsupported OS cannot prove down --------------------------


def test_unknown_os_reports_active_failsafe(tmp_path):
    """_OS is Linux|Darwin by construction; an unrecognized OS must report active
    (rc 0) so the alert path never pages a host it cannot supervise — even with
    both probes stubbed to 'down'."""
    rc = _service_is_active(
        "com.x.b1", "SunOS", tmp_path, systemctl_rc=3, launchctl_rc=1
    )
    assert rc == 0
