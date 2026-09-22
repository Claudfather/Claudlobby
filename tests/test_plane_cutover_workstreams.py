"""The workstream registry lives on the plane (cutover chunk A2 moved it
there behind a fourth door; the F18 closure's R1 made it the ONLY home).
`workstream-update.sh` works on a registry MATERIALIZED from the plane (the
same jq programs, one lock), the verb's plane event IS the write, no file is
ever written, and every reader (`claudlobby workstreams`, brief's section)
renders the registry from the plane — `plane-readers.workstream_registry`. No
flag gates this door any more, and since R2b no reader consults the
retirement fact or a file: a plane that cannot serve the registry, or an
emission the shim could not record, is a REFUSAL (rc 3 / rc 4), never a file.
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
from tests.plane_fixtures import F, REPO, _cli, _env, _scene, _stdlib_readers, ro as _ro

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


def _reg(paths):
    """Where the registry FILE used to live — asserted absent: nothing writes one."""
    return paths.fleet_state / "workstreams.json"


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


def test_the_plane_renders_every_verb_the_door_wrote(tmp_path):
    """The whole verb table through the real door, read back through the
    renderer the door itself materializes from — and no file anywhere."""
    root, paths, _, _ = _scene(tmp_path)
    reg = _reg(paths)
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
    pruned = _ws(root, "prune")
    assert pruned.returncode == 0 and "archived on the plane" in pruned.stdout
    assert not reg.exists()                                                     # the plane event IS the write
    assert not (root / "local" / F / "runtime" / "workstreams-archive.jsonl").exists()   # the archived event is the archive
    assert _await(root, "SELECT COUNT(*) FROM events WHERE kind = 'workstream' AND event = 'archived'", 1) == 1
    pr = _stdlib_readers()
    with _ro(root) as conn:
        plane_reg = pr.workstream_registry(conn, F, lease_days=14)
    assert set(plane_reg["workstreams"]) == {ws_a}                              # the pruned one is gone
    e = plane_reg["workstreams"][ws_a]
    assert set(SHARED) <= set(e) and set(e) >= {"renewals"}
    assert (e["id"], e["fleet"], e["title"], e["project"], e["owner_bot"]) == (ws_a, F, "Ship the widget", "alpha", "w1")
    assert e["status"] == "blocked" and e["next"] == "waiting on review"        # the block's note replaced the progress's next
    assert e["task_ids"] == [] and e["refs"] == {"issues": [], "prs": []}
    assert e["opened_ts"] <= e["last_progress_ts"]                              # the progress advanced (or held) the instant
    assert [r["note"] for r in e["renewals"]] == ["still on it"]
    assert e["lease_expires_ts"] > pr._plus_days(e["last_progress_ts"], 14)     # the renewal's own instant, not progress + the default lease
    assert plane_reg["updated"] >= e["last_progress_ts"]


def test_the_door_works_with_no_file_and_the_readers_serve_the_plane(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    reg = _reg(paths)
    a = _ws(root, "open", "Retired-era work", "--owner", "w2", "--next", "plan it")
    assert a.returncode == 0, a.stderr
    ws_a = a.stdout.strip()
    assert not reg.exists()                                                     # the plane event IS the write
    assert _await(root, "SELECT COUNT(*) FROM workstreams", 1) == 1
    listing = _ws_cli(root)                                                     # no flag, no fact: the plane
    assert listing.returncode == 0 and ws_a in listing.stdout and "w2" in listing.stdout, listing.stdout + listing.stderr
    shown = _ws_cli(root, "show", ws_a)
    assert shown.returncode == 0 and "Retired-era work" in shown.stdout and "plan it" in shown.stdout
    assert _ws(root, "progress", ws_a, "--next", "build it").returncode == 0
    assert _ws(root, "block", ws_a, "--note", "blocked on x").returncode == 0
    assert _await(root, "SELECT COUNT(*) FROM events WHERE kind = 'workstream'", 2) == 2
    assert not reg.exists()
    deg = []
    fleet, _ = load_fleet(root / "local" / F / "fleet.yaml")
    section = _workstream_section(fleet, paths, int(time.time()), deg)
    assert section == {"active": [], "stalled": []}                             # blocked: not active — served, not omitted
    assert not any(d.field == "workstreams" for d in deg)
    assert _ws(root, "close", ws_a, "--status", "done").returncode == 0
    pruned = _ws(root, "prune")
    assert pruned.returncode == 0 and "Pruned 1" in pruned.stdout
    assert not (root / "local" / F / "runtime" / "workstreams-archive.jsonl").exists()   # the archived event is the archive
    assert _await(root, "SELECT COUNT(*) FROM events WHERE kind = 'workstream' AND event = 'archived'", 1) == 1
    after = _ws_cli(root)
    assert after.returncode == 0 and "No workstreams." in after.stdout
    # the plane gone: the reader REFUSES (rc 3) — unreachable is not "No workstreams." (F18 R2b)
    for p in (root / "state" / "plane").glob("plane.db*"):
        p.unlink()
    unknown = _ws_cli(root)
    assert unknown.returncode == 3 and unknown.stdout == "" and "UNREACHABLE" in unknown.stderr


def test_an_unrecorded_verb_refuses_and_changes_nothing(tmp_path):
    """An emission the shim could not record is a REFUSAL (rc 4): the verb did
    not happen, the plane is unchanged, and no file appears — there is
    nothing to land it in any more."""
    root, paths, _, _ = _scene(tmp_path)
    reg = _reg(paths)
    a = _ws(root, "open", "Lost in the post", PLANE_EMIT_CLI="/usr/bin/false")
    assert a.returncode == 4, a.stdout + a.stderr
    assert "did not record this verb" in a.stderr and "nothing changed" in a.stderr
    assert "landed at" not in a.stderr
    assert not reg.exists()
    assert _await(root, "SELECT COUNT(*) FROM workstreams", 0, timeout=2) == 0


def test_the_lookup_and_the_reader_refuse_an_unknown_fleet(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    r = subprocess.run([sys.executable, str(LIB / "plane-lookup.py"), "--root", str(root), "--workstreams", "--fleet", "ghost"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 3 and r.stdout == "" and "no identity for fleet" in r.stderr
    ok = subprocess.run([sys.executable, str(LIB / "plane-lookup.py"), "--root", str(root), "--workstreams", "--fleet", F],
                        capture_output=True, text=True, timeout=120)
    assert ok.returncode == 0 and json.loads(ok.stdout) == {"updated": "", "workstreams": {}}
    # the WRITER's question (--or-empty): an unknown fleet is the empty registry,
    # because its first open is exactly the call that must work
    first = subprocess.run([sys.executable, str(LIB / "plane-lookup.py"), "--root", str(root), "--workstreams",
                            "--or-empty", "--fleet", "ghost"], capture_output=True, text=True, timeout=120)
    assert first.returncode == 0 and json.loads(first.stdout) == {"updated": "1970-01-01T00:00:00Z", "workstreams": {}, "archived": []}   # the writer's render carries the archived ids


# --- #1635: the one-shot importer for a pre-cutover workstreams.json ---------


def _write_residual(paths, doc):
    paths.fleet_state.mkdir(parents=True, exist_ok=True)
    (paths.fleet_state / "workstreams.json").write_text(json.dumps(doc))


def _stale_file(*, lease_days_ago: int = 16):
    """A single row whose lease expired `lease_days_ago` days ago — the
    shape #1635 is about: a naive import would re-lease it from the import
    instant and silence the stall flag for another fortnight."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    opened = now - timedelta(days=30)
    progressed = now - timedelta(
        days=lease_days_ago + 14
    )  # so lease = progressed+14 is `lease_days_ago` in the past
    lease = progressed + timedelta(days=14)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "updated": fmt(progressed),
        "workstreams": {
            "ws-stale-one": {
                "id": "ws-stale-one",
                "fleet": F,
                "title": "A row from before the cutover",
                "project": None,
                "status": "active",
                "owner_bot": "w1",
                "next": "still the last known next step",
                "task_ids": [],
                "refs": {"issues": [], "prs": []},
                "opened_ts": fmt(opened),
                "last_progress_ts": fmt(progressed),
                "lease_expires_ts": fmt(lease),
                "renewals": [],
            }
        },
    }, fmt(lease)


def test_import_preserves_the_original_lease_and_progress_instants(tmp_path):
    """The acceptance test #1635 names as the one that would have caught the
    naive version: brief flags the imported row stalled ON THE DAY OF THE
    IMPORT, which is only possible if the lease came from the file's own
    instants and not from the import's wall-clock time."""
    root, paths, _, _ = _scene(tmp_path)
    doc, expected_lease = _stale_file()
    _write_residual(paths, doc)

    r = _cli(root, "import-workstreams")
    assert r.returncode == 0, r.stdout + r.stderr

    pr = _stdlib_readers()
    with _ro(root) as conn:
        reg = pr.workstream_registry(conn, F, lease_days=14)
    e = reg["workstreams"]["ws-stale-one"]
    assert e["lease_expires_ts"] == expected_lease, (
        e["lease_expires_ts"],
        expected_lease,
    )
    assert e["opened_ts"] == doc["workstreams"]["ws-stale-one"]["opened_ts"]
    assert (
        e["last_progress_ts"] == doc["workstreams"]["ws-stale-one"]["last_progress_ts"]
    )

    fleet, _md = load_fleet(root / "local" / F / "fleet.yaml")
    section = _workstream_section(fleet, paths, int(time.time()), [])
    stalled_ids = {w["id"] for w in section["stalled"]}
    assert "ws-stale-one" in stalled_ids, section


def test_constructs_only_import_would_re_lease(tmp_path):
    """Negative control: proves the acceptance test above is not vacuous by
    reproducing the exact "wrong turn" #1635's own spec calls out by name —
    "emitting the constructs and letting the reader fill in the rest...
    every lease reads fresh... converting a stale stream into a freshly-
    leased one." That failure is specifically about the construct's OWN
    occurred_at, not the verb events: a naive importer that forgets to carry
    original instants at all defaults to `_finalize`'s "now" (omitting
    occurred_at entirely, mirroring emit_api.py:168's own fallback), which
    is what this constructs it that way — never `row["opened_ts"]`, which
    would just be a smaller, different bug (a missing progressed event)."""
    root, paths, _, _ = _scene(tmp_path)
    doc, expected_lease = _stale_file()
    row = doc["workstreams"]["ws-stale-one"]

    from claudlobby.plane.emit_api import emit_batch

    naive = [
        {
            "event_type": "workstream",
            "emitter": "test-naive-import",
            "fleet": F,
            # occurred_at DELIBERATELY omitted: the naive bug this test
            # pins is exactly the absence of an original instant.
            "origin": "legacy",
            "payload": {
                "workstream_id": "ws-stale-one-naive",
                "title": row["title"],
                "opened_by": f"bot:{F}/w1",
                "owner": f"bot:{F}/w1",
            },
        }
    ]
    emit_batch(root, naive)

    pr = _stdlib_readers()
    with _ro(root) as conn:
        reg = pr.workstream_registry(conn, F, lease_days=14)
    e = reg["workstreams"]["ws-stale-one-naive"]
    # the naive lease is opened_ts + 14d, NOT the file's stored (much older) lease
    assert e["lease_expires_ts"] != expected_lease
    assert e["lease_expires_ts"] > expected_lease, (
        "the naive import's lease must be LATER than the correct one — "
        "proving it under-reports staleness, not just differs"
    )


def test_import_is_idempotent_on_a_second_run(tmp_path):
    """Proven by running it twice and diffing the plane, not by reasoning
    about the code — dara's own bar for this acceptance criterion."""
    root, paths, _, _ = _scene(tmp_path)
    doc, _ = _stale_file()
    _write_residual(paths, doc)

    first = _cli(root, "import-workstreams")
    assert first.returncode == 0, first.stdout + first.stderr

    def _counts():
        with _ro(root) as conn:
            return (
                conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0],
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='workstream'"
                ).fetchone()[0],
            )

    before = _counts()
    assert before[0] == 1

    second = _cli(root, "import-workstreams")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "skipped ws-stale-one" in second.stderr
    after = _counts()
    assert after == before, (before, after)


def test_dedup_skips_an_id_already_live_or_archived(tmp_path):
    """R1 gauntlet hazard 1, reproduced: a construct id is unique per fleet
    FOREVER, live or archived — the importer must not re-mint either."""
    root, paths, _, _ = _scene(tmp_path)
    # a REAL open through the writer, using the SAME id the file will carry
    opened = subprocess.run(
        [
            "bash",
            str(LIB / "workstream-update.sh"),
            "open",
            "Pre-existing",
            "--id",
            "ws-collides",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env={
            **_env(root),
            "FLEET_NAME": F,
            "BOT_NAME": "mgr",
            "PLANE_EMIT_ENABLED": "1",
            "PLANE_EMIT_CLI": str(CLI),
            "PLANE_SOCKET": str(root / "no-daemon.sock"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "WORKSTREAM_LEASE_DAYS": "14",
        },
    )
    assert opened.returncode == 0, opened.stdout + opened.stderr

    doc, _ = _stale_file()
    doc["workstreams"]["ws-collides"] = doc["workstreams"].pop("ws-stale-one")
    doc["workstreams"]["ws-collides"]["id"] = "ws-collides"
    _write_residual(paths, doc)

    r = _cli(root, "import-workstreams")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skipped ws-collides" in r.stderr

    with _ro(root) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM workstreams WHERE workstream_id='ws-collides'"
        ).fetchone()[0]
    assert n == 1  # the live one from the writer, never a second mint


def test_renewals_reconstruct_their_own_lease_target(tmp_path):
    """A renewals[] entry only ever stored {ts, note} at the file layer — its
    renewed_until is derivable as ts + lease_days, the SAME formula both
    `progress` and `renew` use in the shell writer (`_lease_expiry_iso`)."""
    from datetime import datetime, timedelta, timezone

    root, paths, _, _ = _scene(tmp_path)
    now = datetime.now(timezone.utc)
    opened = now - timedelta(days=20)
    renewed_at = now - timedelta(days=10)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = {
        "updated": fmt(renewed_at),
        "workstreams": {
            "ws-renewed-one": {
                "id": "ws-renewed-one",
                "fleet": F,
                "title": "Renewed without progress",
                "project": None,
                "status": "active",
                "owner_bot": "w1",
                "next": "still working it",
                "task_ids": [],
                "refs": {"issues": [], "prs": []},
                "opened_ts": fmt(opened),
                "last_progress_ts": fmt(opened),
                "lease_expires_ts": fmt(renewed_at + timedelta(days=14)),
                "renewals": [
                    {
                        "ts": fmt(renewed_at),
                        "note": "still relevant, no code change yet",
                    }
                ],
            }
        },
    }
    _write_residual(paths, doc)

    r = _cli(root, "import-workstreams")
    assert r.returncode == 0, r.stdout + r.stderr

    pr = _stdlib_readers()
    with _ro(root) as conn:
        reg = pr.workstream_registry(conn, F, lease_days=14)
    e = reg["workstreams"]["ws-renewed-one"]
    assert e["lease_expires_ts"] == fmt(renewed_at + timedelta(days=14))
    assert e["last_progress_ts"] == fmt(opened)  # renew never advances progress
    assert [r_["note"] for r_ in e["renewals"]] == [
        "still relevant, no code change yet"
    ]


def test_dry_run_touches_neither_file_nor_plane(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    doc, _ = _stale_file()
    _write_residual(paths, doc)
    before = (paths.fleet_state / "workstreams.json").read_text()

    r = _cli(root, "import-workstreams", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"event_id"' in r.stdout  # the full envelope plan, not a summary

    assert (paths.fleet_state / "workstreams.json").read_text() == before
    with _ro(root) as conn:
        n = conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0]
    assert n == 0


def test_absent_file_exits_0_and_unreadable_file_exits_3(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    absent = _cli(root, "import-workstreams")
    assert absent.returncode == 0 and "nothing to import" in absent.stderr

    resid = paths.fleet_state / "workstreams.json"
    resid.parent.mkdir(parents=True, exist_ok=True)
    resid.write_text("{}")
    resid.chmod(0o000)
    try:
        unreadable = _cli(root, "import-workstreams")
        assert unreadable.returncode == 3, unreadable.stdout + unreadable.stderr
    finally:
        resid.chmod(0o644)  # restore so tmp_path teardown can remove it


def test_archive_renames_and_a_second_run_finds_nothing(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    doc, _ = _stale_file()
    _write_residual(paths, doc)

    r = _cli(root, "import-workstreams", "--archive")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "archived" in r.stdout
    assert not (paths.fleet_state / "workstreams.json").exists()
    archived = list(paths.fleet_state.glob("workstreams.json.imported-*"))
    assert len(archived) == 1

    again = _cli(root, "import-workstreams")
    assert again.returncode == 0 and "nothing to import" in again.stderr


def test_capture_mode_metadata_warns_and_goal_survives_the_strip(tmp_path):
    """The issue's capture-policy interaction: note/next_step are CONTENT and
    get stripped under metadata mode, but the construct's goal is not
    content-classified and is what `next` falls back to."""
    root, paths, _, _ = _scene(tmp_path)
    (root / "state" / "plane" / "capture.json").write_text(
        json.dumps({"*": "metadata"})
    )
    doc, _ = _stale_file()
    _write_residual(paths, doc)

    r = _cli(root, "import-workstreams")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "capture mode is 'metadata'" in r.stderr
    assert "STRIPPED" in r.stderr

    pr = _stdlib_readers()
    with _ro(root) as conn:
        reg = pr.workstream_registry(conn, F, lease_days=14)
    e = reg["workstreams"]["ws-stale-one"]
    assert e["next"] == doc["workstreams"]["ws-stale-one"]["next"]  # survived via goal
