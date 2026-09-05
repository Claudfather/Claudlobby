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


def test_a_leftover_events_file_is_not_the_sweeps_to_delete(tmp_path):
    """F18 closure R2b-2: nothing writes `data/events/*.jsonl` and nothing reads
    it — the plane holds the events and `plane prune` ages its samples — so the
    sweep no longer names the pattern. A file left there is the operator's
    (the C0 archive list), never this sweep's; a stale one survives."""
    root = tmp_path / "root"
    data = root / "local" / "f" / "runtime" / "bots" / "erlich" / "data"
    (data / "events").mkdir(parents=True)
    (root / "lib").symlink_to(REPO / "lib")
    leftover = data / "events" / "fleet-2020-01-01.jsonl"; leftover.write_text('{"type":"x"}\n')
    marker = data / ".plane-rc-relay-cccc"; marker.write_text("")
    stale = time.time() - 40 * 86400
    os.utime(leftover, (stale, stale)); os.utime(marker, (stale, stale))
    env = dict(os.environ, CLAUDLOBBY_ROOT=str(root), HOME=str(tmp_path),
               PATH="/usr/bin:/bin:/usr/sbin:/sbin")
    r = subprocess.run(["bash", str(REPO / "lib" / "data-sweep.sh"), "f", "--purge", "--days", "30"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr + r.stdout
    assert leftover.exists()           # not the sweep's pattern any more
    assert not marker.exists()         # the sweep itself still ran
