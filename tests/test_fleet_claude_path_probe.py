"""Expected PATH misses stay quiet under the real inherited ERR trap.

The helper only resolves a file; it must not execute a binary or accept a shell
function as that file. Each recording case then causes an unrelated real error
and reads its actual scratch Plane receipt, proving the trap remains armed.
"""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess

import pytest

from tests.conftest import constructed_env, read_fleet_events

REPO = Path(__file__).resolve().parents[1]
LIB_COMMON = REPO / "lib/lib-common.sh"


def _probe(tmp_path: Path, shape: str, *, scratch_plane_env=None):
    root = tmp_path / "root"
    root.mkdir()
    (root / "lib").symlink_to(REPO / "lib", target_is_directory=True)
    home, temporary, primary, secondary = (tmp_path / name for name in
                                           ("home", "tmp", "primary", "secondary"))
    for directory in (home, temporary, primary, secondary):
        directory.mkdir()
    executed = tmp_path / "executed"

    def executable(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\nprintf executed > {shlex.quote(str(executed))}\nexit 0\n")
        path.chmod(0o755)
        return path

    pin = None
    expected = ""
    function = ""
    if shape in ("present", "function-and-file"):
        expected = str(executable(primary / "claude"))
        executable(secondary / "claude")  # Preserve PATH precedence too.
    elif shape == "bare-override":
        pin = "alternate"
        expected = str(executable(primary / pin))
        executable(primary / "claude")
    elif shape == "absolute-pin":
        pin = expected = str(executable(tmp_path / "pinned directory" / "claude"))
    elif shape == "missing-absolute-pin":
        # Path-shaped explicit pins are returned as-is, including absent paths.
        pin = expected = str(tmp_path / "absent" / "claude")
    elif shape == "staged-link":
        target = executable(root / "state" / "versions" / "claude")
        link = root / "state" / "bin" / "claude"
        link.parent.mkdir()
        link.symlink_to(target)
        expected = str(link)  # The helper must keep the launch link, not its target.
    if shape in ("function-only", "function-and-file"):
        function = f"claude() {{ printf function > {shlex.quote(str(executed))}; }}\n"

    script = tmp_path / "path-probe.sh"
    script.write_text(
        'set -euo pipefail\n. "$1"\ninstall_error_trap ""\n' + function +
        'resolved="$(fleet_claude_path "$LOOKUP_PATH")"\n'
        'printf "RESOLVED=<%s>\\n" "$resolved"\n'
        # This unrelated failure must still emit through the actual trap/shim.
        "/bin/sh -c 'exit 23'\n"
    )
    env = constructed_env(HOME=home, TMPDIR=temporary, CLAUDLOBBY_ROOT=root,
                          LOOKUP_PATH=f"{primary}:{secondary}",
                          FLEET_NAME="path-probe", FLEET_EVENT_EMIT_TIMEOUT_S="60")
    if pin is not None:
        env["CLAUDE_BIN"] = pin
    if scratch_plane_env is not None:
        env.update(scratch_plane_env(root))
    result = subprocess.run(["/bin/bash", str(script), str(LIB_COMMON)],
                            env=env, cwd=tmp_path, text=True, capture_output=True,
                            timeout=90)
    return result, root, expected, executed


@pytest.mark.parametrize("shape", [
    "missing", "present", "function-only", "function-and-file", "bare-override",
    "absolute-pin", "missing-absolute-pin", "staged-link",
])
def test_resolution_preserves_real_error_recording_without_false_lookup_receipts(
        tmp_path, scratch_plane_env, shape):
    result, root, expected, executed = _probe(tmp_path, shape, scratch_plane_env=scratch_plane_env)
    assert result.returncode == 23, (result.stdout, result.stderr)
    assert result.stdout == f"RESOLVED=<{expected}>\n"
    assert not executed.exists(), "path resolution executed a binary or shell function"
    rows = [json.loads(line) for line in read_fleet_events(root).splitlines()]
    errors = [row for row in rows if row["type"] == "script_error"]
    assert errors, f"the real unexpected-error receipt did not land: {result.stderr}"
    assert [row["data"]["exit_code"] for row in errors] == [23], (
        "expected PATH miss emitted a false script_error alongside the real failure", errors,
        result.stderr,
    )
    assert errors[0]["data"]["script"] == "path-probe.sh"


def test_constructed_default_stays_silent_even_with_an_armed_error_trap(tmp_path):
    result, root, expected, executed = _probe(tmp_path, "missing")
    assert result.returncode == 23, (result.stdout, result.stderr)
    assert expected == "" and result.stdout == "RESOLVED=<>\n"
    assert not executed.exists()
    assert not (root / "state" / "plane").exists(), "default-disabled emission created Plane state"
    assert read_fleet_events(root) == ""
