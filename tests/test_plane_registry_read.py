"""Chunk B: the registry lane's READ half — F11's validation half enforced.

The four scan tests are MANDATED by the spec (§18 round-3 F11): matching
scan_id validates tombstones; an incomplete scan invalidates; a mismatched
scan_id invalidates; an empty-but-complete scan tombstones everything in
scope. Every test drives the REAL emit spine (emit_batch -> ingest -> the
Lane C SQL) — fixtures at the SQL layer would certify a join the ingest
path might not produce (the r4 lesson, applied to our own producer).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from claudlobby.plane import registry_read as rr
from claudlobby.plane.emit_api import emit_batch

REPO = Path(__file__).resolve().parent.parent

T1 = "2026-09-01T10:00:00+00:00"
T2 = "2026-09-01T11:00:00+00:00"
T3 = "2026-09-01T12:00:00+00:00"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "emitroot"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    return root


def _conn(root: Path) -> sqlite3.Connection:
    return sqlite3.connect(root / "state" / "plane" / "plane.db")


def _snap(alias: str, scan_id: str, occurred_at: str, payload: dict,
          etype: str = "bot") -> dict:
    return {"event_type": "registry_snapshot", "emitter": "t", "fleet": "f",
            "occurred_at": occurred_at,
            "payload": {"entity_type": etype, "entity_alias": alias,
                        "cause": "generate", "scan_id": scan_id,
                        "payload": payload}}


def _tomb(alias: str, scan_id: str, occurred_at: str,
          etype: str = "bot") -> dict:
    return {"event_type": "registry_snapshot", "emitter": "t", "fleet": "f",
            "occurred_at": occurred_at,
            "payload": {"entity_type": etype, "entity_alias": alias,
                        "cause": "generate", "scan_id": scan_id,
                        "tombstone": True}}


def _done(scan_id: str, occurred_at: str, complete: bool = True) -> dict:
    return {"event_type": "declaration", "emitter": "t", "fleet": "f",
            "occurred_at": occurred_at,
            "payload": {"event": "scan_completed", "subject_kind": "host",
                        "subject": "h1", "scan_id": scan_id,
                        "scope": "test", "counts": {},
                        "complete": complete}}


BOT = "bot:f/erlich"


def _bot(alias: str, model: str = "opus", skills=("status",)) -> dict:
    """A minimal CONTRACT-VALID BotPayload — the ingest path validates
    entity payloads against §9b, so under-shaped fixtures never reach the
    SQL these tests exist to exercise."""
    return {"alias": alias, "account": "acct", "service": "svc",
            "model": model, "equipment": {"skills": sorted(skills)},
            "posture": {"permissions_mode": "plan"},
            "composed_hashes": {}, "declared_hash": "d1",
            "schema_version": "1"}


P1 = _bot(BOT, "opus", ["status"])
P2 = _bot(BOT, "fable", ["status", "dispatch"])
LIB = {"category": "skills", "name": "status", "source_tier": "shared",
       "content_hash": "h", "declared_hash": "h", "schema_version": "1"}


# --- the four mandated F11 scan tests --------------------------------------

def test_matching_complete_scan_validates_the_tombstone(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1), _done("s1", T1),
                      _tomb(BOT, "s2", T2), _done("s2", T2)])
    conn = _conn(root)
    assert rr.current_entities(conn) == []          # deleted from current
    hist = rr.entity_history(conn, BOT)
    assert [h["tombstone"] for h in hist] == [0, 1]
    assert hist[0]["valid_from"].startswith("2026-09-01T10")
    assert hist[0]["valid_to"].startswith("2026-09-01T11")
    assert hist[1]["valid_to"] is None              # deletion still open
    assert rr.invalid_tombstones(conn) == []


def test_incomplete_scan_never_deletes(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1),
                      _tomb(BOT, "s2", T2), _done("s2", T2, complete=False)])
    conn = _conn(root)
    cur = rr.current_entities(conn)
    assert [c["entity_alias"] for c in cur] == [BOT]
    assert cur[0]["payload"]["model"] == "opus"     # snapshot survives
    inv = rr.invalid_tombstones(conn)
    assert [i["scan_id"] for i in inv] == ["s2"]    # surfaced, not honored


def test_mismatched_scan_id_never_deletes(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1),
                      _tomb(BOT, "s2", T2), _done("s3", T2)])
    conn = _conn(root)
    assert [c["entity_alias"] for c in rr.current_entities(conn)] == [BOT]
    assert [i["scan_id"] for i in rr.invalid_tombstones(conn)] == ["s2"]


def test_empty_but_complete_scan_tombstones_everything_in_scope(tmp_path):
    root = _root(tmp_path)
    other = "bot:f/dinesh"
    emit_batch(root, [
        _snap(BOT, "s1", T1, P1),
        _snap(other, "s1", T1, _bot(other)),
        _done("s1", T1),
        _tomb(BOT, "s2", T2), _tomb(other, "s2", T2), _done("s2", T2)])
    conn = _conn(root)
    assert rr.current_entities(conn) == []
    assert rr.invalid_tombstones(conn) == []


# --- SCD ordering, changes, trust doors ------------------------------------

def test_scd_ties_on_occurred_at_break_by_ingest_seq(tmp_path):
    """Spec line 145: ORDER BY (occurred_at, ingest_seq) — never bare ts,
    no dependence on timestamp uniqueness. Two rows at the same instant:
    ledger order decides which is current."""
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1)])
    emit_batch(root, [_snap(BOT, "s2", T1, P2)])    # same occurred_at
    conn = _conn(root)
    cur = rr.current_entities(conn)
    assert cur[0]["payload"]["model"] == "fable"
    hist = rr.entity_history(conn, BOT)
    assert [h["payload"]["model"] for h in hist] == ["opus", "fable"]


def test_changes_carry_field_level_diffs(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1)])
    emit_batch(root, [_snap(BOT, "s2", T2, P2)])
    conn = _conn(root)
    chg = rr.recent_changes(conn)
    assert [c["change"] for c in chg] == ["updated", "first_observed"]
    assert chg[0]["fields"]["model"] == ("opus", "fable")
    assert chg[0]["fields"]["equipment.skills"] == (
        ["status"], ["dispatch", "status"])   # _bot sorts, like the emitter
    assert chg[1]["fields"] == {}             # first sighting, no field storm


def test_changes_render_deletion_and_recreation(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1), _done("s1", T1),
                      _tomb(BOT, "s2", T2), _done("s2", T2)])
    emit_batch(root, [_snap(BOT, "s3", T3, P2)])
    conn = _conn(root)
    changes = {c["change"] for c in rr.recent_changes(conn)}
    assert changes == {"first_observed", "deleted", "recreated"}
    cur = rr.current_entities(conn)
    assert cur[0]["payload"]["model"] == "fable"    # recreation is current


def test_diff_fields_added_and_removed_keys():
    assert rr.diff_fields({"a": 1}, {"b": 2}) == {
        "a": (1, None), "b": (None, 2)}
    assert rr.diff_fields({"x": {"y": 1}}, {"x": {"y": 1}}) == {}
    # the public-door non-dict branch (unreachable from shipped callers,
    # kept for direct consumers): top-level scalars diff under "."
    assert rr.diff_fields(1, 2) == {".": (1, 2)}
    assert rr.diff_fields(1, 1) == {}


# --- round-2 pins: the write side asks the reader's question ---------------

def test_crashed_scan_tombstone_does_not_block_later_deletion(tmp_path):
    """Gauntlet SEV-1 (probed): a scan dying between its tombstone and its
    completion left an INVALID tombstone that suppressed every later valid
    deletion at both write layers — permanent false PRESENT in the exact
    case F11 exists for. Suppression now asks the reader's own question
    (effectively deleted?), so the later complete scan CAN delete."""
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1), _done("s1", T1),
                      _tomb(BOT, "s2", T2)])          # crashed: no completion
    conn = _conn(root)
    assert [c["entity_alias"] for c in rr.current_entities(conn)] == [BOT]
    emit_batch(root, [_tomb(BOT, "s3", T3), _done("s3", T3)])
    conn = _conn(root)
    assert rr.current_entities(conn) == []            # deletion finally lands
    assert [i["scan_id"] for i in rr.invalid_tombstones(conn)] == ["s2"]


def test_stale_clock_tombstone_heals_by_re_tombstoning(tmp_path):
    """A valid tombstone stamped with an OLDER occurred_at loses the SCD
    ordering (spec line 145 rules the ordering; pinned here) — the entity
    stays current. The heal: a later re-tombstone is NOT suppressed (the
    entity is still current) and lands with a fresh instant that wins."""
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T2, P1), _done("s1", T2),
                      _tomb(BOT, "s2", T1), _done("s2", T1)])  # older instant
    conn = _conn(root)
    assert [c["entity_alias"] for c in rr.current_entities(conn)] == [BOT]
    assert rr.invalid_tombstones(conn) == []          # valid, just losing
    emit_batch(root, [_tomb(BOT, "s4", T3), _done("s4", T3)])
    conn = _conn(root)
    assert rr.current_entities(conn) == []


def test_repeat_tombstone_of_an_effective_deletion_is_suppressed(tmp_path):
    """The dedup's legitimate half survives the fix: once a deletion is
    EFFECTIVE, re-tombstoning writes nothing (no row spam)."""
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1), _done("s1", T1),
                      _tomb(BOT, "s2", T2), _done("s2", T2),
                      _tomb(BOT, "s3", T3), _done("s3", T3)])
    conn = _conn(root)
    stones = conn.execute("SELECT COUNT(*) FROM registry_snapshots"
                          " WHERE tombstone=1").fetchone()[0]
    assert stones == 1                                # s3's was suppressed
    assert rr.current_entities(conn) == []


def test_verify_host_scoping_and_duplicate_assembly(tmp_path):
    """Two guards probed in review: a foreign host's partition must not
    read as phantom drift when host_uid is passed, and a duplicate alias
    in an injected assembly must not double-count or contradict itself."""
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1)])
    conn = _conn(root)
    host = conn.execute("SELECT host_uid FROM registry_snapshots"
                        " LIMIT 1").fetchone()[0]
    rep = rr.verify_current(conn, [("bot", BOT, P1)], fleet="f",
                            host_uid=host)
    assert rep.ok and rep.checked == 1
    rep2 = rr.verify_current(conn, [("bot", BOT, P1)], fleet="f",
                             host_uid="host_nonexistent")
    assert rep2.missing_from_db == [("bot", BOT)]     # scoped away, honest
    dup = rr.verify_current(
        conn, [("bot", BOT, P2), ("bot", BOT, P1)], fleet="f")
    assert dup.ok and dup.checked == 1                # last wins, once


def test_last_scan_reports_the_newest_completion(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1), _done("s1", T1),
                      _done("s2", T2, complete=False)])
    conn = _conn(root)
    ls = rr.last_scan(conn)
    assert ls["scan_id"] == "s2"
    assert ls["complete"] is False
    assert ls["occurred_at"].startswith("2026-09-01T11")


def test_reader_fleet_scope_uses_the_emitter_rules(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [
        _snap(BOT, "s1", T1, P1),
        _snap("bot:other/x", "s1", T1, _bot("bot:other/x")),
        _snap("shared/skills/status", "s1", T1, LIB, etype="library_item")])
    conn = _conn(root)
    aliases = {r["entity_alias"] for r in rr.current_entities(conn, fleet="f")}
    assert BOT in aliases
    assert "shared/skills/status" in aliases        # host-global
    assert "bot:other/x" not in aliases             # another fleet's


# --- verify (the injectable-assembly seam) ---------------------------------

def test_verify_matches_and_detects_each_drift_direction(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1)])
    conn = _conn(root)
    ok = rr.verify_current(conn, [("bot", BOT, P1)], fleet="f")
    assert ok.ok and ok.checked == 1
    drift = rr.verify_current(conn, [("bot", BOT, P2)], fleet="f")
    assert drift.drifted == [("bot", BOT)]
    extra = rr.verify_current(
        conn, [("bot", BOT, P1), ("bot", "bot:f/new", _bot("bot:f/new"))],
        fleet="f")
    assert extra.missing_from_db == [("bot", "bot:f/new")]
    gone = rr.verify_current(conn, [], fleet="f")
    assert gone.missing_from_estate == [("bot", BOT)]


def test_verify_ignores_vault_rev_like_the_hash_gate(tmp_path):
    """vault_rev is excluded from the gate's hashed view (a daily vault
    commit must not read as estate drift) — verify must share that view or
    every generate day manufactures phantom drift."""
    root = _root(tmp_path)
    p = {**P1, "vault_rev": "abc123"}
    emit_batch(root, [_snap(BOT, "s1", T1, p)])
    conn = _conn(root)
    rep = rr.verify_current(
        conn, [("bot", BOT, {**P1, "vault_rev": "zzz999"})], fleet="f")
    assert rep.ok


# --- CLI + doctor smoke (the real doors, subprocess) ------------------------

def _cli(root: Path, *argv: str) -> subprocess.CompletedProcess:
    # module form, not REPO/.venv/bin: CI has no .venv — the running
    # interpreter (venv locally, CI's env there) is the one true binary
    import sys
    return subprocess.run(
        [sys.executable, "-m", "claudlobby", "--root", str(root),
         "plane", *argv],
        capture_output=True, text=True, timeout=120)


def test_cli_registry_list_show_history_and_trust_line(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1), _done("s1", T1),
                      _tomb(BOT, "s2", T2), _done("s3", T2)])  # s2 invalid
    r = _cli(root, "registry")
    assert r.returncode == 0
    assert BOT in r.stdout
    assert "NOT honored" in r.stderr                # the trust line
    r2 = _cli(root, "registry", "--show", BOT)
    assert r2.returncode == 0
    assert '"model": "opus"' in r2.stdout
    # the INVALID tombstone is rightly absent from history (F11: excluded,
    # not merely demoted) — only its s1 window renders
    r3 = _cli(root, "registry", "--history", BOT)
    assert r3.returncode == 0
    assert "TOMBSTONE" not in r3.stdout
    assert "scan=s1" in r3.stdout
    r4 = _cli(root, "registry", "--show", "nope")
    assert r4.returncode == 1
    # a VALID tombstone renders in history as the deletion window
    root2 = _root(tmp_path / "valid")
    emit_batch(root2, [_snap(BOT, "s1", T1, P1), _done("s1", T1),
                       _tomb(BOT, "s2", T2), _done("s2", T2)])
    r5 = _cli(root2, "registry", "--history", BOT)
    assert r5.returncode == 0
    assert "TOMBSTONE" in r5.stdout
    # --scope is a DB fact, never an overlay requirement (the shared
    # --fleet dest collided with the global overlay selector — probed)
    r6 = _cli(root, "registry", "--scope", "f")
    assert r6.returncode == 0
    assert BOT in r6.stdout


def test_cli_trust_line_survives_an_empty_registry(tmp_path):
    """One unhonored tombstone deleting your only entity must not read as
    silence — the trust line precedes the empty early-return (probed: it
    used to vanish exactly when it mattered most)."""
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1), _done("s1", T1),
                      _tomb(BOT, "s2", T2), _done("s2", T2),   # valid delete
                      _tomb("bot:f/ghost", "s4", T3)])         # invalid, ghost
    r = _cli(root, "registry")
    assert r.returncode == 0
    assert "empty" in r.stderr
    assert "NOT honored" in r.stderr


def test_doctor_surfaces_invalid_tombstones_and_scan_health(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "s1", T1, P1),
                      _tomb(BOT, "s2", T2), _done("s2", T2, complete=False)])
    r = _cli(root, "doctor")
    assert r.returncode == 1
    assert "tombstone validity" in r.stdout
    assert "INCOMPLETE" in r.stdout
