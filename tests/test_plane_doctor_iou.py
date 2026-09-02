"""Doctor IOUs + provisional-signal hygiene (chunk: the two rungs chunk B
deferred, plus the live-flagged provisional fixes).

- The reconcile rung surfaces RECONCILIATION_SQL (informational — the tmux
  carrier yields no ack by design, so nonzero is expected, never a fault).
- The composed-hash-drift rung runs verify_current when a fleet resolves,
  points at --verify when not.
- provisional_actors EXCLUDES humans (never-confirmable, not a typo
  suspect); the rail excludes the `_host` sentinel and does not badge
  humans provisional.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.identity import provisional_actors

REPO = Path(__file__).resolve().parent.parent


def _root(tmp_path):
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    return root


def _cli(root, *argv):
    return subprocess.run(
        [sys.executable, "-m", "claudlobby", "--root", str(root),
         "plane", *argv], capture_output=True, text=True, timeout=120)


# --- provisional hygiene ---------------------------------------------------

def test_humans_are_not_provisional_suspects(tmp_path):
    """A human minted from a real message is never in a roster to confirm —
    provisional_actors (the typo-suspect list the doctor rung + trust panel
    read) must not include it, or it cries wolf on the operator's name."""
    root = _root(tmp_path)
    emit_batch(root, [
        {"event_type": "communication", "emitter": "t", "fleet": "f",
         "payload": {"msg_id": "msg_" + "a" * 32,
                     "sender": "human:chris", "recipient": "bot:f/erlich",
                     "message_class": "chat", "body": "hi"}}])
    conn = connect(db_path(root))
    try:
        # the human IS provisional in the db (never confirmed)...
        raw = conn.execute(
            "SELECT provisional FROM identity_registry"
            " WHERE alias='human:chris'").fetchone()[0]
        assert raw == 1
        # ...but is NOT in the typo-suspect list
        suspects = [a["alias"] for a in provisional_actors(conn)]
        assert "human:chris" not in suspects
    finally:
        conn.close()


def test_rail_excludes_host_sentinel_and_unbadges_humans(tmp_path):
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app

    root = _root(tmp_path)
    emit_batch(root, [
        {"event_type": "communication", "emitter": "t", "fleet": "f",
         "payload": {"msg_id": "msg_" + "b" * 32,
                     "sender": "human:chris", "recipient": "bot:f/erlich",
                     "message_class": "chat", "body": "hi"}},
        # a host-scoped metric_sample under the _host sentinel fleet
        {"event_type": "metric_sample", "emitter": "host-probe",
         "fleet": "_host",
         "payload": {"subject_kind": "host", "subject": "myhost",
                     "metric": "host.job_ran", "value": 1}}])
    body = TestClient(create_app(root)).get("/api/identities").json()
    ids = {i["alias"]: i for i in body["data"]["identities"]}
    assert "_host" not in ids                 # sentinel fleet excluded
    assert ids["human:chris"]["provisional"] == 0   # human not badged
    assert ids["human:chris"]["short"] == "chris"


# --- doctor rungs ----------------------------------------------------------

def test_doctor_reconcile_rung_is_informational(tmp_path):
    """A submitted-not-acked transmission (the tmux steady state) surfaces
    as an OK reconcile rung with the count, never ATTENTION."""
    root = _root(tmp_path)
    emit_batch(root, [
        {"event_type": "transmission", "emitter": "t", "fleet": "f",
         "payload": {"msg_id": "msg_" + "c" * 32, "attempt_no": 1,
                     "carrier": "tmux", "destination": "erlich",
                     "state": "pane_submitted"}}])
    r = _cli(root, "doctor")
    assert "reconcile (submitted-not-acked)" in r.stdout
    # the reconcile line is OK, not ATTENTION (informational)
    line = next(ln for ln in r.stdout.splitlines() if "reconcile" in ln)
    assert line.startswith("[ok]")
    assert " 1 " in line                       # the count is surfaced


def test_doctor_drift_rung_points_at_verify_without_a_fleet(tmp_path):
    """With no fleet context (root has no fleet.yaml), the drift rung
    surfaces the capability by pointing at the --verify door rather than
    silently skipping."""
    root = _root(tmp_path)
    emit_batch(root, [
        {"event_type": "metric_sample", "emitter": "t", "fleet": "f",
         "payload": {"subject_kind": "host", "subject": "h",
                     "metric": "host.job_ran", "value": 1}}])
    r = _cli(root, "doctor")
    assert "composed-hash drift" in r.stdout
    drift = next(ln for ln in r.stdout.splitlines()
                 if "composed-hash drift" in ln)
    assert "registry --verify" in drift
