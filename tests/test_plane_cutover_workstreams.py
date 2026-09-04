"""Cutover chunk A2 — the workstream registry, the last file-backed record.
`workstream-update.sh` dual-wrote its verbs to the plane since PR-B; now a
fourth door (`PLANE_LEGACY_WRITE_WORKSTREAMS`) retires the file: under the
retirement the door works on a registry MATERIALIZED from the plane (the same
jq programs, the same locks), the verb's plane event IS the write, and every
reader (`claudlobby workstreams`, brief's section) renders the registry from
the plane — `plane-readers.workstream_registry`, pinned equal to the file the
door writes under dual-write. An emission the shim could not record lands the
registry at the real path, disclosed, so a verb is never recorded nowhere.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from claudlobby.brief import _workstream_section
from claudlobby.config import load_fleet
from claudlobby.plane import shadow as sh
from claudlobby.workstreams import registry_path
from tests.plane_fixtures import ro as _ro
from tests.test_plane_cutover_flip import _cli, _declare, _env, _stdlib_readers
from tests.test_plane_shadow import F, REPO, _scene

LIB = REPO / "lib"
CLI = Path(sys.executable).parent / "claudlobby"


def _door_env(root, **extra):
    env = {"CLAUDLOBBY_ROOT": str(root), "HOME": str(root / "home"), "FLEET_NAME": F, "BOT_NAME": "mgr",
           "PLANE_EMIT_ENABLED": "1", "PLANE_EMIT_CLI": str(CLI),
           "PLANE_SOCKET": str(root / "no-daemon.sock"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "WORKSTREAM_LEASE_DAYS": "14"}
    env.update(extra)
    return env


def _ws(root, *args, **extra):
    r = subprocess.run(["bash", str(LIB / "workstream-update.sh"), *args], capture_output=True, text=True,
                       timeout=180, env=_door_env(root, **extra))
    return r


def _retire(root):
    for reader in sh.GATED:
        _declare(root, reader)
    assert _cli(root, "cutover", "--retire-writes").returncode == 0


def _await(root, sql, want, *, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        with _ro(root) as conn:
            got = conn.execute(sql).fetchone()[0]
        if got == want or time.monotonic() > deadline:
            return got
        time.sleep(0.25)


def _ws_cli(root, *args, **extra):
    return subprocess.run([sys.executable, "-m", "claudlobby", "--root", str(root), "--fleet", F,
                           "workstreams", *args], capture_output=True, text=True, timeout=180,
                          env=_env(root, **extra))


SHARED = ("id", "fleet", "title", "project", "status", "owner_bot", "next", "task_ids", "refs",
          "opened_ts", "last_progress_ts", "lease_expires_ts")


def test_the_plane_renders_the_registry_the_door_wrote_under_dual_write(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    reg = registry_path(paths)
    a = _ws(root, "open", "Ship the widget", "--owner", "w1", "--project", "alpha", "--next", "first cut")
    assert a.returncode == 0, a.stderr
    ws_a = a.stdout.strip()
    b = _ws(root, "open", "A second one"); ws_b = b.stdout.strip()
    assert _ws(root, "progress", ws_a, "--next", "second cut").returncode == 0
    # a DIFFERENT lease on the renew: the renewal's own instant (renewed_until) must
    # be what the plane renders, not the last progress plus the fleet's lease —
    # the two coincide to the second when both verbs run in one second (a
    # mutant dropping the renewal survived the first pin)
    assert _ws(root, "renew", ws_a, "--note", "still on it", WORKSTREAM_LEASE_DAYS="30").returncode == 0
    assert _ws(root, "block", ws_a, "--note", "waiting on review").returncode == 0
    assert _ws(root, "close", ws_b, "--status", "done").returncode == 0
    assert _ws(root, "prune").returncode == 0                                   # b archived and dropped from the file
    file_reg = json.loads(reg.read_text())
    assert set(file_reg["workstreams"]) == {ws_a}
    assert (root / "local" / F / "runtime" / "workstreams-archive.jsonl").exists()   # dual-write: the archive
    assert _await(root, "SELECT COUNT(*) FROM events WHERE kind = 'workstream' AND event = 'archived'", 1) == 1
    pr = _stdlib_readers()
    with _ro(root) as conn:
        plane_reg = pr.workstream_registry(conn, F, lease_days=14)
    assert set(plane_reg["workstreams"]) == {ws_a}                              # the pruned one is gone on both sides
    mine, theirs = file_reg["workstreams"][ws_a], plane_reg["workstreams"][ws_a]
    assert {k: mine[k] for k in SHARED} == {k: theirs[k] for k in SHARED}, (mine, theirs)
    assert mine["status"] == "blocked" and mine["next"] == "waiting on review"
    assert [r["note"] for r in theirs["renewals"]] == ["still on it"]
    assert theirs["lease_expires_ts"] == mine["lease_expires_ts"]                # the renewal's own instant
    assert theirs["lease_expires_ts"] > pr._plus_days(mine["last_progress_ts"], 14)   # and not progress + the default lease
    assert plane_reg["updated"] >= mine["last_progress_ts"]


def test_under_the_retirement_the_door_works_with_no_file_and_the_readers_serve_the_plane(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    reg = registry_path(paths)
    _retire(root)
    a = _ws(root, "open", "Retired-era work", "--owner", "w2", "--next", "plan it", PLANE_LEGACY_WRITE_WORKSTREAMS="0")
    assert a.returncode == 0, a.stderr
    ws_a = a.stdout.strip()
    assert not reg.exists()                                                     # the plane event IS the write
    assert _await(root, "SELECT COUNT(*) FROM workstreams", 1) == 1
    listing = _ws_cli(root, PLANE_READ_OPEN="1")                                # no flag needed: the retirement is the fact
    assert listing.returncode == 0 and ws_a in listing.stdout and "w2" in listing.stdout, listing.stdout + listing.stderr
    shown = _ws_cli(root, "show", ws_a)
    assert shown.returncode == 0 and "Retired-era work" in shown.stdout and "plan it" in shown.stdout
    assert _ws(root, "progress", ws_a, "--next", "build it", PLANE_LEGACY_WRITE_WORKSTREAMS="0").returncode == 0
    assert _ws(root, "block", ws_a, "--note", "blocked on x", PLANE_LEGACY_WRITE_WORKSTREAMS="0").returncode == 0
    assert _await(root, "SELECT COUNT(*) FROM events WHERE kind = 'workstream'", 2) == 2
    assert not reg.exists()
    deg = []
    fleet, _ = load_fleet(root / "local" / F / "fleet.yaml")
    section = _workstream_section(fleet, paths, int(time.time()), deg)
    assert section == {"active": [], "stalled": []}                             # blocked: not active — served, not omitted
    assert not any(d.field == "workstreams" for d in deg)
    assert _ws(root, "close", ws_a, "--status", "done", PLANE_LEGACY_WRITE_WORKSTREAMS="0").returncode == 0
    pruned = _ws(root, "prune", PLANE_LEGACY_WRITE_WORKSTREAMS="0")
    assert pruned.returncode == 0 and "Pruned 1" in pruned.stdout
    assert not (root / "local" / F / "runtime" / "workstreams-archive.jsonl").exists()   # the archived event is the archive
    assert _await(root, "SELECT COUNT(*) FROM events WHERE kind = 'workstream' AND event = 'archived'", 1) == 1
    after = _ws_cli(root)
    assert after.returncode == 0 and "No workstreams." in after.stdout
    # the fact unknown (the plane gone): the file serves, LABELED — and an absent file is then a refusal as before
    for p in (root / "state" / "plane").glob("plane.db*"):
        p.unlink()
    unknown = _ws_cli(root)
    assert unknown.returncode == 1 and "may be stale" in unknown.stderr


def test_an_unrecorded_verb_lands_the_registry_at_the_real_path(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    reg = registry_path(paths)
    _retire(root)
    a = _ws(root, "open", "Lost in the post", PLANE_LEGACY_WRITE_WORKSTREAMS="0", PLANE_EMIT_CLI="/usr/bin/false")
    assert a.returncode == 0, a.stderr
    assert "did not record this verb" in a.stderr and "landed at" in a.stderr
    assert reg.exists() and a.stdout.strip() in json.loads(reg.read_text())["workstreams"]


def test_the_lookup_and_the_reader_refuse_an_unknown_fleet(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    r = subprocess.run([sys.executable, str(LIB / "plane-lookup.py"), "--root", str(root), "--workstreams", "--fleet", "ghost"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 3 and r.stdout == "" and "no identity for fleet" in r.stderr
    ok = subprocess.run([sys.executable, str(LIB / "plane-lookup.py"), "--root", str(root), "--workstreams", "--fleet", F],
                        capture_output=True, text=True, timeout=120)
    assert ok.returncode == 0 and json.loads(ok.stdout) == {"updated": "", "workstreams": {}}
