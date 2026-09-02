"""data-sweep.sh — the weekly ephemeral purge, driven for real on a throwaway
root in --purge mode. The pin the rc-relay F7 fold claimed: the Stop hook's
0-byte .plane-rc-relay-<uuid> dedupe markers are vetted ephemeral and age
out at the window; a fresh marker and a durable neighbour are untouched.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_rc_relay_markers_age_out_and_durable_files_do_not(tmp_path):
    root = tmp_path / "root"
    data = root / "local" / "f" / "runtime" / "bots" / "erlich" / "data"
    data.mkdir(parents=True)
    (root / "lib").symlink_to(REPO / "lib")
    old = data / ".plane-rc-relay-aaaa"; old.write_text("")
    fresh = data / ".plane-rc-relay-bbbb"; fresh.write_text("")
    durable = data / "notes.md"; durable.write_text("keep")
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale)); os.utime(durable, (stale, stale))
    env = dict(os.environ, CLAUDLOBBY_ROOT=str(root), HOME=str(tmp_path),
               PATH="/usr/bin:/bin:/usr/sbin:/sbin")
    r = subprocess.run(["bash", str(REPO / "lib" / "data-sweep.sh"), "f",
                        "--purge", "--days", "30"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr + r.stdout
    assert not old.exists()            # the stale marker was swept
    assert fresh.exists()              # inside the window: kept
    assert durable.exists()            # durable, whatever its age: never swept
