"""Tests for lib/workstream-update.sh — the single-writer registry mutator.

The registry is the PLANE (F18 closure R1): every verb materializes it from
the plane (`plane-lookup.py --workstreams --or-empty`), the verb's plane event
IS the write, and nothing is written to disk — no workstreams.json, no
archive file, no WORKSTREAMS_PATH override. The door REFUSES rather than
falls back: an unreachable plane (rc 3), a silenced plane (PLANE_EMIT_DISABLED=1,
rc 3), an emission the shim could not record (rc 4).

Drives the real bash helper against a throwaway plane root per test: the
shim's socket rung finds no daemon (disclosed on stderr) and the cold-CLI rung
does the real ingest, so every verb costs a python spawn or two. The registry
is read back through the SAME renderer the door materializes from, whose
shape is the file's old shape (id, fleet, title, project, status, owner_bot,
next, task_ids, refs, opened_ts, last_progress_ts, lease_expires_ts,
renewals[, closed_ts]) — with one model difference worth knowing: the lease is
DERIVED at read time (opened / last progress + the read-time lease days, or
the renewal's own renewed_until), never stored per verb.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.plane_fixtures import ro as _ro

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "lib" / "workstream-update.sh"
LOOKUP = REPO / "lib" / "plane-lookup.py"
CLI = Path(sys.executable).parent / "claudlobby"
FLEET = "f"

# The helper shells out to jq; skip cleanly on hosts without it rather than
# erroring the whole suite (matches the sibling bash-driving tests).
pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="workstream-update.sh requires jq"
)


def _root(tmp_path: Path) -> Path:
    """The throwaway plane root (tests.plane_fixtures.plane_root's shape):
    state/plane/capture.json only — the first emission creates the db."""
    root = tmp_path / "root"
    plane = root / "state" / "plane"
    if not plane.exists():
        plane.mkdir(parents=True)
        (plane / "capture.json").write_text('{"*": "full"}')
    (tmp_path / "home").mkdir(exist_ok=True)
    return root


def _env(tmp_path: Path, env_extra: dict | None = None) -> dict:
    root = _root(tmp_path)
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path / "home"),
        "CLAUDLOBBY_ROOT": str(root),
        "FLEET_NAME": FLEET,
        "BOT_NAME": "mgr",
        "PLANE_EMIT_CLI": str(CLI),
        # no daemon: the socket rung fails (disclosed) and the cold CLI ingests
        "PLANE_SOCKET": str(root / "no-daemon.sock"),
        "WORKSTREAM_LEASE_DAYS": "14",
    }
    if env_extra:
        env.update(env_extra)
    return env


def _run(tmp_path: Path, *args: str, env_extra: dict | None = None):
    """Run workstream-update.sh against the test's plane root. Returns CompletedProcess."""
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=_env(tmp_path, env_extra),
        capture_output=True,
        text=True,
        timeout=180,
    )


def _registry(tmp_path: Path, fleet: str = FLEET, lease_days: int = 14) -> dict:
    """The registry as the door materializes it: the plane, rendered."""
    r = subprocess.run(
        [sys.executable, str(LOOKUP), "--root", str(_root(tmp_path)), "--workstreams",
         "--or-empty", "--fleet", fleet, "--lease-days", str(lease_days)],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _archived(tmp_path: Path) -> list[str]:
    """The ids whose `archived` workstream event landed on the plane — the archive."""
    db = _root(tmp_path) / "state" / "plane" / "plane.db"
    if not db.exists():
        return []
    with _ro(_root(tmp_path)) as conn:
        return [r[0] for r in conn.execute(
            "SELECT workstream_id FROM events WHERE kind = 'workstream' AND event = 'archived'"
            " ORDER BY ingest_seq")]


def _open(tmp_path: Path, title: str, *extra: str, env_extra: dict | None = None) -> str:
    r = _run(tmp_path, "open", title, *extra, env_extra=env_extra)
    assert r.returncode == 0, f"open failed: {r.stderr}"
    return r.stdout.strip()


def _iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _no_files(tmp_path: Path) -> None:
    """Nothing but the plane under the root — the door writes no record file.
    Excluded: the plane's own home, the host uid it mints, and the LOCK
    artefacts (`workstreams.lock` under flock on Linux, `workstreams.lock.d`
    under the mkdir spinlock on macOS) — a lock is not a record."""
    root = tmp_path / "root"
    files = sorted(
        p for p in root.rglob("*") if p.is_file()
        and "state/plane" not in str(p.relative_to(root))
        and p.name != "host-uid"
        and not p.name.endswith(".lock") and ".lock.d" not in str(p)
    )
    assert files == [], f"the door wrote files: {files}"


class TestOpen:
    def test_open_creates_active_entry_with_full_schema(self, tmp_path: Path):
        ws_id = _open(
            tmp_path, "Ship the widget", "--owner", "alex", "--project", "acme", "--next", "spike"
        )
        assert ws_id == "ws-ship-the-widget"
        entry = _registry(tmp_path)["workstreams"][ws_id]
        assert entry["status"] == "active"
        assert entry["owner_bot"] == "alex"
        assert entry["project"] == "acme"
        assert entry["next"] == "spike"
        assert entry["task_ids"] == []
        assert entry["refs"] == {"issues": [], "prs": []}
        assert entry["renewals"] == []
        # opened/progress/lease timestamps all present
        for k in ("opened_ts", "last_progress_ts", "lease_expires_ts"):
            assert entry[k], f"missing {k}"
        _no_files(tmp_path)

    def test_slug_dedup_is_deterministic(self, tmp_path: Path):
        a = _open(tmp_path, "Same Title")
        b = _open(tmp_path, "Same Title")
        c = _open(tmp_path, "Same Title")
        assert [a, b, c] == ["ws-same-title", "ws-same-title-2", "ws-same-title-3"]

    def test_explicit_id_collision_fails(self, tmp_path: Path):
        _open(tmp_path, "First", "--id", "ws-custom")
        r = _run(tmp_path, "open", "Second", "--id", "ws-custom")
        assert r.returncode != 0
        assert "already exists" in r.stderr

    def test_fleet_name_stamps_entry(self, tmp_path: Path):
        ws_id = _open(tmp_path, "Fleet-stamped", env_extra={"FLEET_NAME": "eng-team"})
        assert _registry(tmp_path, fleet="eng-team")["workstreams"][ws_id]["fleet"] == "eng-team"
        # the other fleet's registry never saw it
        assert ws_id not in _registry(tmp_path)["workstreams"]

    def test_lease_is_days_after_open(self, tmp_path: Path):
        ws_id = _open(tmp_path, "Leased", env_extra={"WORKSTREAM_LEASE_DAYS": "14"})
        entry = _registry(tmp_path, lease_days=14)["workstreams"][ws_id]
        opened = _iso(entry["opened_ts"])
        expiry = _iso(entry["lease_expires_ts"])
        assert abs((expiry - opened).total_seconds() - 14 * 86400) < 120


class TestCap:
    def test_open_at_cap_fails_with_actionable_message(self, tmp_path: Path):
        env = {"WORKSTREAM_MAX_ACTIVE": "2"}
        _open(tmp_path, "one", env_extra=env)
        _open(tmp_path, "two", env_extra=env)
        r = _run(tmp_path, "open", "three", env_extra=env)
        assert r.returncode == 3
        assert "cap (2)" in r.stderr
        assert "max_active" in r.stderr  # names the knob
        assert "ws-one" in r.stderr  # names oldest active as a close candidate
        assert set(_registry(tmp_path)["workstreams"]) == {"ws-one", "ws-two"}

    def test_blocked_and_closed_free_a_cap_slot(self, tmp_path: Path):
        env = {"WORKSTREAM_MAX_ACTIVE": "2"}
        a = _open(tmp_path, "one", env_extra=env)
        _open(tmp_path, "two", env_extra=env)
        # Blocking one drops it out of the active count -> a third can open.
        assert _run(tmp_path, "block", a, env_extra=env).returncode == 0
        assert _run(tmp_path, "open", "three", env_extra=env).returncode == 0


class TestProgressRenew:
    def test_progress_advances_last_progress_and_rederives_the_lease(self, tmp_path: Path):
        ws_id = _open(tmp_path, "work")
        before = _registry(tmp_path)["workstreams"][ws_id]
        time.sleep(1.1)                     # the render is whole-second: cross the boundary
        r = _run(tmp_path, "progress", ws_id, "--next", "phase 2")
        assert r.returncode == 0, r.stderr
        after = _registry(tmp_path)["workstreams"][ws_id]
        assert after["next"] == "phase 2"
        assert after["last_progress_ts"] > before["last_progress_ts"]
        # the lease is derived from the last progress + the read-time lease days
        assert _iso(after["lease_expires_ts"]) == _iso(after["last_progress_ts"]) + timedelta(days=14)
        assert after["lease_expires_ts"] > before["lease_expires_ts"]

    def test_renew_requires_note(self, tmp_path: Path):
        ws_id = _open(tmp_path, "needs-note")
        r = _run(tmp_path, "renew", ws_id)
        assert r.returncode != 0
        assert "--note is required" in r.stderr

    def test_renew_loophole_is_visible(self, tmp_path: Path):
        """renew extends the lease but must NOT credit progress — so serial
        renew-without-progress stays detectable by the stall check."""
        ws_id = _open(tmp_path, "loophole")
        opened = _registry(tmp_path)["workstreams"][ws_id]
        r1 = _run(tmp_path, "renew", ws_id, "--note", "still waiting on review", env_extra={"WORKSTREAM_LEASE_DAYS": "30"})
        r2 = _run(tmp_path, "renew", ws_id, "--note", "still waiting again", env_extra={"WORKSTREAM_LEASE_DAYS": "30"})
        assert r1.returncode == 0 and r2.returncode == 0, r1.stderr + r2.stderr
        after = _registry(tmp_path)["workstreams"][ws_id]
        # the renewal's OWN instant (renewed_until, 30d) is the lease, beyond the
        # default 14d the render derives from the last progress
        assert after["lease_expires_ts"] > opened["lease_expires_ts"]
        assert _iso(after["lease_expires_ts"]) > _iso(after["last_progress_ts"]) + timedelta(days=14)
        # two renewals logged, with notes, but progress NOT credited
        assert [rn["note"] for rn in after["renewals"]] == ["still waiting on review", "still waiting again"]
        assert after["last_progress_ts"] == opened["last_progress_ts"]


class TestCloseBlockPrune:
    def test_close_marks_done_and_stamps_closed_ts(self, tmp_path: Path):
        ws_id = _open(tmp_path, "finish")
        assert _run(tmp_path, "close", ws_id).returncode == 0
        entry = _registry(tmp_path)["workstreams"][ws_id]
        assert entry["status"] == "done"
        assert entry["closed_ts"]

    def test_close_abandoned(self, tmp_path: Path):
        ws_id = _open(tmp_path, "drop")
        assert _run(tmp_path, "close", ws_id, "--status", "abandoned").returncode == 0
        assert _registry(tmp_path)["workstreams"][ws_id]["status"] == "abandoned"

    def test_close_rejects_bad_status(self, tmp_path: Path):
        ws_id = _open(tmp_path, "bad")
        r = _run(tmp_path, "close", ws_id, "--status", "finished")
        assert r.returncode != 0
        assert "done|abandoned" in r.stderr

    def test_block_drops_from_active_and_carries_its_note(self, tmp_path: Path):
        ws_id = _open(tmp_path, "stuck", "--next", "keep going")
        assert _run(tmp_path, "block", ws_id, "--note", "waiting on review").returncode == 0
        entry = _registry(tmp_path)["workstreams"][ws_id]
        assert entry["status"] == "blocked" and entry["next"] == "waiting on review"

    def test_prune_archives_terminal_on_the_plane_and_drops_from_registry(self, tmp_path: Path):
        keep = _open(tmp_path, "keep active")
        gone = _open(tmp_path, "will close")
        _run(tmp_path, "close", gone)
        r = _run(tmp_path, "prune")
        assert r.returncode == 0, r.stderr
        assert "Pruned 1 terminal workstream(s) -- archived on the plane" in r.stdout
        reg = _registry(tmp_path)["workstreams"]
        assert keep in reg and gone not in reg
        # the `archived` event IS the archive: one per pruned id, no file anywhere
        assert _archived(tmp_path) == [gone]
        _no_files(tmp_path)

    def test_prune_noop_when_nothing_terminal(self, tmp_path: Path):
        _open(tmp_path, "active only")
        r = _run(tmp_path, "prune")
        assert r.returncode == 0
        assert _archived(tmp_path) == []
        assert "Pruned" not in r.stdout


class TestArchivedIds:
    def test_a_pruned_title_reopens_under_a_fresh_id(self, tmp_path: Path):
        """A construct id is unique per fleet on the plane, so the slug dedup
        must see what was pruned: the writer's render carries the archived
        ids (found by the R1 gauntlet — a re-opened title re-minted the
        archived id and ingest refused it, rc 4)."""
        first = _open(tmp_path, "Same Title")
        assert _run(tmp_path, "close", first).returncode == 0
        assert _run(tmp_path, "prune").returncode == 0
        assert first not in _registry(tmp_path)["workstreams"]
        r = _run(tmp_path, "open", "Same Title")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == f"{first}-2"
        assert set(_registry(tmp_path)["workstreams"]) == {f"{first}-2"}


class TestErrors:
    @pytest.mark.parametrize("cmd", ["progress", "renew", "block", "close"])
    def test_missing_id_fails(self, tmp_path: Path, cmd: str):
        extra = ["--note", "x"] if cmd == "renew" else []
        r = _run(tmp_path, cmd, "ws-nonexistent", *extra)
        assert r.returncode == 1
        assert "no such workstream" in r.stderr

    def test_unknown_subcommand_fails(self, tmp_path: Path):
        r = _run(tmp_path, "frobnicate")
        assert r.returncode != 0
        assert "unknown subcommand" in r.stderr


class TestRefusals:
    """The door refuses rather than falls back: there is no file behind it."""

    def test_a_silenced_plane_refuses_with_nothing_to_work_on(self, tmp_path: Path):
        r = _run(tmp_path, "open", "quiet", env_extra={"PLANE_EMIT_DISABLED": "1"})
        assert r.returncode == 3
        assert "the plane is the only registry" in r.stderr
        assert not (_root(tmp_path) / "state" / "plane" / "plane.db").exists()
        _no_files(tmp_path)

    def test_a_root_that_does_not_exist_refuses(self, tmp_path: Path):
        # the writer trusts an EXISTING root exactly as far as its own emit
        # would (the first emission creates state/plane there); a root that is
        # not a directory is unreachable, never an empty registry
        missing = tmp_path / "missing"
        r = _run(tmp_path, "open", "nowhere", env_extra={"CLAUDLOBBY_ROOT": str(missing)})
        assert r.returncode == 3
        assert "could not serve the registry" in r.stderr
        assert not missing.exists()

    def test_an_unrecorded_verb_refuses_and_changes_nothing(self, tmp_path: Path):
        keeper = _open(tmp_path, "keeper")
        r = _run(tmp_path, "open", "lost in the post", env_extra={"PLANE_EMIT_CLI": "/usr/bin/false"})
        assert r.returncode == 4
        assert "did not record this verb" in r.stderr and "nothing changed" in r.stderr
        assert set(_registry(tmp_path)["workstreams"]) == {keeper}
        _no_files(tmp_path)


class TestConcurrency:
    def test_parallel_opens_mint_distinct_ids(self, tmp_path: Path):
        # N concurrent opens mint N distinct ids and never lose an update: each
        # open's plane event is its own row. Through the cold-CLI rung an open
        # costs ~1-2s inside the lock, so N is held to what the lock's 5s
        # spinlock budget serializes (n=20 lands opens past the budget).
        n = 6
        # Cap raised above n so this isolates id-minting, not the cap (the cap
        # holding under concurrency is covered by the sequential cap tests).
        hi_cap = {"WORKSTREAM_MAX_ACTIVE": "50"}
        _root(tmp_path)                                   # one root, created before the race
        with ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(
                lambda i: _run(tmp_path, "open", f"work item {i}", env_extra=hi_cap), range(n)
            ))
        assert all(r.returncode == 0 for r in results), [r.stderr for r in results if r.returncode]
        ids = [r.stdout.strip() for r in results]
        assert len(set(ids)) == n, f"id collision under concurrency: {sorted(ids)}"
        assert len(_registry(tmp_path)["workstreams"]) == n, "lost update: fewer entries than opens"

    @pytest.mark.parametrize("cmd,extra", [
        ("progress", []), ("renew", ["--note", "n"]), ("block", []), ("close", []),
    ])
    def test_mutator_never_autovivifies_a_missing_id(self, tmp_path: Path, cmd, extra):
        # B1 regression: the existence check runs INSIDE the lock, so a mutator
        # on an absent id errors and leaves the registry untouched — jq never
        # runs `.workstreams[$id].x = y` on a missing key (which would create a
        # partial zombie: no id/status/title).
        keeper = _open(tmp_path, "keeper")
        r = _run(tmp_path, cmd, "ws-ghost", *extra)
        assert r.returncode != 0
        workstreams = _registry(tmp_path)["workstreams"]
        assert "ws-ghost" not in workstreams, f"{cmd} auto-vivified a zombie entry"
        assert set(workstreams) == {keeper}, f"{cmd} disturbed the registry"

    def test_concurrent_mutate_and_prune_stay_wellformed(self, tmp_path: Path):
        # Race a mutator against prune on a terminal entry. Whatever the
        # ordering, every surviving entry must be a full record (its map key
        # equals its .id) — no status-only zombie. Five rounds (each verb now
        # costs a spawn or two through the cold-CLI rung), each with its OWN
        # title: an archived id leaves the render, so re-opening the same
        # title would re-mint the same id and the plane's UNIQUE workstream_id
        # refuses it — the door's slug dedup cannot see archived ids (reported).
        for i in range(5):
            ws = _open(tmp_path, f"racer {i}")
            _run(tmp_path, "close", ws)
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(_run, tmp_path, "prune")
                f2 = ex.submit(_run, tmp_path, "renew", ws, "--note", "race")
                f1.result(); f2.result()
            for wid, entry in _registry(tmp_path)["workstreams"].items():
                assert entry.get("id") == wid, f"zombie entry {wid!r}: {entry}"


class TestBadEnvBounds:
    # M2/M3: the single writer validates its own bounds; a bad env must _die
    # before any mutation, not silently disable the cap or write an empty lease.
    @pytest.mark.parametrize("var", ["WORKSTREAM_MAX_ACTIVE", "WORKSTREAM_LEASE_DAYS"])
    @pytest.mark.parametrize("bad", ["lots", "-3", "0"])
    def test_non_positive_int_bound_dies(self, tmp_path: Path, var: str, bad: str):
        r = _run(tmp_path, "open", "t", env_extra={var: bad})
        assert r.returncode != 0
        if var == "WORKSTREAM_LEASE_DAYS" and bad == "lots":
            # The materialization passes the lease to the lookup BEFORE the
            # bounds check runs, so a non-numeric lease is refused by the
            # lookup (rc 3) rather than by _require_pos_int — nothing mutated
            # either way; the ordering is the door's to tighten.
            assert "could not serve the registry" in r.stderr or "positive integer" in r.stderr
        else:
            assert "positive integer" in r.stderr or "must be >= 1" in r.stderr
        assert _registry(tmp_path)["workstreams"] == {}, "entry created despite bad bound"
        _no_files(tmp_path)
