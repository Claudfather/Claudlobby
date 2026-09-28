"""Plane-only workstream import and registry reconstruction checks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claudlobby.brief import _workstream_section
from claudlobby.config import load_fleet
from tests.plane_fixtures import F, REPO, _cli, _scene, _stdlib_readers, ro as _ro

LIB = REPO / "lib"


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
    def fmt(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
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
    # A pre-existing construct uses the same ID the residual file carries.
    from claudlobby.plane.emit_api import emit_batch
    opened = emit_batch(root, [{"event_type": "workstream", "emitter": "test",
        "fleet": F, "payload": {"workstream_id": "ws-collides", "title": "Pre-existing",
                              "opened_by": f"bot:{F}/mgr"}}], require_commit=True)
    assert opened[0].status == "committed"

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
    def fmt(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
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


def test_dry_run_against_a_fresh_root_creates_no_plane_db(tmp_path):
    """#1748 review: --dry-run promises to touch neither the file nor the
    plane. Against a root with NO plane db at all, the old connect() had
    the side effect of creating one (measured: a 288 KB migrated file) --
    the dedup read must be read-only and non-creating."""
    from tests.plane_fixtures import plane_root, _paths

    root = plane_root(tmp_path)
    paths = _paths(root)
    doc, _ = _stale_file()
    _write_residual(paths, doc)
    assert not (root / "state" / "plane" / "plane.db").exists()

    r = _cli(root, "import-workstreams", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"event_id"' in r.stdout

    assert not (root / "state" / "plane" / "plane.db").exists(), (
        "a dry run against a plane-less root must not create one"
    )


def test_the_kernel_lock_excludes_a_shell_writer_when_flock_binary_is_unavailable(
    tmp_path
):
    """Shell's no-flock-binary fallback and the importer must lock the same
    file. A stale directory from the retired spinlock must be inert.

    Force the shell's no-binary path on every host, including Linux CI."""
    import select
    import subprocess as sp
    import time

    from claudlobby.plane.workstream_import import registry_lock

    lockfile = tmp_path / "workstreams.lock"
    lockdir = tmp_path / "workstreams.lock.d"
    lockdir.mkdir()
    holder = sp.Popen(
        [
            "bash",
            "-c",
            f'. "{LIB}/lib-common.sh"; _FLOCK_BIN=; WITH_LOCK_WAIT_S=2; with_lock "{lockfile}" '
            f'bash -c "echo acquired; sleep 1.2; echo released"',
        ],
        stdout=sp.PIPE,
        stderr=sp.STDOUT,
        text=True,
        env=os.environ.copy(),
    )
    try:
        readable, _, _ = select.select([holder.stdout], [], [], 5)
        assert readable, "the shell holder did not report acquiring the lock"
        assert holder.stdout.readline().strip() == "acquired"
        assert lockfile.is_file(), "the shell holder must lock the shared file"

        t0 = time.monotonic()
        with registry_lock(lockfile, wait_s=5):
            elapsed = time.monotonic() - t0
        assert holder.wait(timeout=5) == 0
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)
    assert lockdir.is_dir(), "a stale spinlock directory is inert and left untouched"
    assert elapsed > 0.5, (
        f"acquired after only {elapsed:.2f}s -- the shell holder's lock did not exclude this"
    )
