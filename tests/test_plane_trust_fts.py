"""Phase-4 final chunk battery: the trust/gaps surface + channel FTS.

Pins the chunk's laws: §11 — FTS indexes ONLY permitted content (a metadata
row's body never enters the index, structurally); hostile search input never
reads as a source error; snippets carry control-byte markers, never markup;
the trust surface counts what the recorder REFUSED (quarantine, with
reasons), what waits (spool), per-door freshness, per-fleet capture policy
with dormant-fleet disclosure, and unconfirmed identities.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from claudlobby.plane.emit_api import emit_batch  # noqa: E402
from claudlobby.plane.view import create_app  # noqa: E402


def _seed(root: Path) -> None:
    d = root / "state" / "plane"
    d.mkdir(parents=True, exist_ok=True)
    (d / "capture.json").write_text(
        '{"engineering": "full", "data": "metadata", "ghostfleet": "full"}')
    emit_batch(root, [
        {"event_type": "communication", "emitter": "dispatch-task",
         "fleet": "engineering",
         "payload": {"msg_id": "msg_" + "a" * 32,
                     "sender": "bot:engineering/erlich",
                     "recipient": "bot:engineering/dinesh",
                     "message_class": "task_request",
                     "body": "please review the huntress rebase carefully"}},
        {"event_type": "communication", "emitter": "report-back",
         "fleet": "data",
         "payload": {"msg_id": "msg_" + "b" * 32,
                     "sender": "bot:data/peter",
                     "recipient": "bot:data/lumbergh",
                     "message_class": "report",
                     "body": "the huntress secret payload must never index"}},
    ])


def test_search_finds_permitted_words(tmp_path):
    _seed(tmp_path)
    body = TestClient(create_app(tmp_path)).get(
        "/api/search?q=huntress rebase").json()
    assert body["state"] == "ok"
    hits = body["data"]["results"]
    assert len(hits) == 1
    assert hits[0]["sender_short"] == "erlich"
    snip = hits[0]["snip"]
    mo, mc = body["data"]["marker_open"], body["data"]["marker_close"]
    assert mo in snip and mc in snip              # markers, never markup
    assert len(mo) > 2 and len(mc) > 2            # random tokens, not bare bytes
    assert "<" not in snip.replace(mo, "").replace(mc, "")


def test_metadata_rows_never_enter_the_index(tmp_path):
    """§11 structurally: the data fleet is metadata-capture, so its body is
    NULL at ingest — the secret word is unfindable BY CONSTRUCTION."""
    _seed(tmp_path)
    body = TestClient(create_app(tmp_path)).get(
        "/api/search?q=secret").json()
    assert body["state"] == "ok"
    assert body["data"]["results"] == []


def test_search_scopes_to_the_room(tmp_path):
    _seed(tmp_path)
    client = TestClient(create_app(tmp_path))
    eng = client.get("/api/search?q=huntress&fleet=engineering").json()
    assert len(eng["data"]["results"]) == 1
    ghost = client.get("/api/search?q=huntress&fleet=ghostfleet").json()
    assert ghost["data"]["results"] == []


def test_hostile_query_is_never_a_source_error(tmp_path):
    """A human's unbalanced quote or FTS syntax must never render as
    'unreadable source' — the query is tokenized and quoted."""
    _seed(tmp_path)
    client = TestClient(create_app(tmp_path))
    for evil in ('"unbalanced', "NEAR(", "a AND OR NOT", "col:injection",
                 "*", '"" ""', "  ", "\x00", "hunt\x00ress", "\x00\x00"):
        body = client.get("/api/search", params={"q": evil}).json()
        assert body["state"] == "ok", (evil, body.get("remediation"))


def test_trust_counts_quarantine_with_reasons(tmp_path):
    _seed(tmp_path)
    q = tmp_path / "state" / "plane" / "spool" / "quarantine"
    q.mkdir(parents=True)
    (q / "ev_bad.json").write_text("{}")
    (q / "ev_bad.json.reason").write_text("schema violation: missing sender")
    (q / "ev_worse.json").write_text("{}")
    body = TestClient(create_app(tmp_path)).get("/api/trust").json()
    d = body["data"]
    assert d["quarantined"] == 2
    reasons = {r["event"]: r["reason"] for r in d["quarantine_reasons"]}
    assert reasons["ev_bad.json"].startswith("schema violation")
    assert reasons["ev_worse.json"] == "(no reason recorded)"


def test_trust_emitter_freshness_is_data_driven(tmp_path):
    _seed(tmp_path)
    body = TestClient(create_app(tmp_path)).get("/api/trust").json()
    emitters = {e["emitter"] for e in body["data"]["emitters"]}
    assert {"dispatch-task", "report-back"} <= emitters


def test_trust_discloses_dormant_fleet_with_policy(tmp_path):
    """A fleet with a declared capture policy and zero events ever is a
    DORMANT emitter — disclosed, never silently absent (§16)."""
    _seed(tmp_path)
    body = TestClient(create_app(tmp_path)).get("/api/trust").json()
    fleets = {f["fleet"]: f for f in body["data"]["fleets"]}
    assert "ghostfleet" in fleets
    assert fleets["ghostfleet"]["comms"] == 0
    assert "dormant" in fleets["ghostfleet"]["note"]
    assert fleets["engineering"]["capture"] == "full"
    assert fleets["data"]["capture"] == "metadata"


def test_trust_malformed_capture_is_typed_not_silent(tmp_path):
    _seed(tmp_path)
    (tmp_path / "state" / "plane" / "capture.json").write_text("{broken")
    body = TestClient(create_app(tmp_path)).get("/api/trust").json()
    assert body["state"] == "ok"
    assert body["data"]["capture_config"] == "malformed"


def test_trust_counts_provisional_identities(tmp_path):
    _seed(tmp_path)
    body = TestClient(create_app(tmp_path)).get("/api/trust").json()
    assert body["data"]["provisional_identities"] > 0  # lazily-minted seeds


def test_search_and_trust_are_read_only_routes(tmp_path):
    _seed(tmp_path)
    client = TestClient(create_app(tmp_path))
    for path in ("/api/search?q=x", "/api/trust"):
        assert client.post(path).status_code == 405


# ---------------------------------------------------------------------------
# Gauntlet round-1 fix pins (three-reviewer synthesis)
# ---------------------------------------------------------------------------

def test_markers_are_per_request_random(tmp_path):
    """A bot-authored body carrying literal marker bytes must not be able to
    forge a highlight — possible only if markers are unpredictable."""
    _seed(tmp_path)
    emit_batch(tmp_path, [{
        "event_type": "communication", "emitter": "t", "fleet": "engineering",
        "payload": {"msg_id": "msg_" + "c" * 32,
                    "sender": "bot:engineering/x",
                    "recipient": "bot:engineering/y",
                    "message_class": "chat",
                    "body": "smuggle \x01fake\x02 marker huntress"}}])
    client = TestClient(create_app(tmp_path))
    b1 = client.get("/api/search?q=smuggle").json()["data"]
    b2 = client.get("/api/search?q=smuggle").json()["data"]
    assert b1["marker_open"] != b2["marker_open"]     # fresh per request
    snip = b1["results"][0]["snip"]
    assert "\x01fake\x02" in snip                     # body bytes inert


def test_search_order_rides_the_fts_index_never_a_temp_btree(tmp_path):
    """Cleanup-measured 123ms -> 0.2ms at 100k rows: ORDER BY comms_fts.rowid
    uses FTS5's internal index; ORDER BY c.ingest_seq temp-B-tree-sorts every
    match. Pinned so the sort cannot silently return."""
    import sqlite3 as _sq

    _seed(tmp_path)
    conn = _sq.connect(tmp_path / "state" / "plane" / "plane.db")
    plan = " | ".join(r[-1] for r in conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT c.msg_id, snippet(comms_fts, 0, ?, ?, ' … ', 12)"
        " FROM comms_fts JOIN communications c ON c.ingest_seq = comms_fts.rowid"
        " WHERE comms_fts MATCH ? ORDER BY comms_fts.rowid DESC LIMIT 50",
        ("\x01", "\x02", '"huntress"')).fetchall())
    conn.close()
    assert "TEMP B-TREE" not in plan, plan


def test_fts_keyed_on_ingest_seq_not_implicit_rowid(tmp_path):
    """0005 keys content_rowid on ingest_seq (ledger-derived, never reused).
    The implicit rowid of a TEXT-PK table is reused by delete+insert —
    probed returning the WRONG message verbatim."""
    import sqlite3 as _sq

    _seed(tmp_path)
    conn = _sq.connect(tmp_path / "state" / "plane" / "plane.db")
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='comms_fts'"
                       ).fetchone()[0]
    conn.close()
    assert "content_rowid='ingest_seq'" in sql


def test_emitter_freshness_covers_every_construct_table(tmp_path):
    """All three reviewers: the hand-list missed assignments + workstreams,
    so a door whose only activity was workstream opens read as NEVER FIRED.
    The roster is now ingest._CONSTRUCT_TABLE — pinned with doors that emit
    ONLY into the two previously-missing tables."""
    _seed(tmp_path)
    emit_batch(tmp_path, [
        {"event_type": "assignment", "emitter": "assign-only-door",
         "fleet": "engineering",
         "payload": {"assignment_id": "asg_" + "d" * 32,
                     "work_item_id": "wi_" + "d" * 32,
                     "assignee": "bot:engineering/w",
                     "assigned_by": "bot:engineering/lead"}},
        {"event_type": "workstream", "emitter": "ws-only-door",
         "fleet": "engineering",
         "payload": {"workstream_id": "ws-pin", "title": "t",
                     "opened_by": "bot:engineering/lead"}}])
    body = TestClient(create_app(tmp_path)).get("/api/trust").json()
    emitters = {e["emitter"]: e for e in body["data"]["emitters"]}
    assert "assign-only-door" in emitters
    assert "ws-only-door" in emitters
    assert emitters["ws-only-door"]["last_at"] is not None


def test_unreadable_quarantine_dir_is_disclosed_never_zero(tmp_path):
    """The false all-clear this panel exists to kill: an unreadable
    quarantine dir must read as a DISCLOSED gap, never quarantined=0
    (macOS glob swallows PermissionError silently — probed)."""
    import os as _os

    if _os.geteuid() == 0:
        pytest.skip("root reads through chmod 000")
    _seed(tmp_path)
    q = tmp_path / "state" / "plane" / "spool" / "quarantine"
    q.mkdir(parents=True)
    (q / "ev_hidden.json").write_text("{}")
    q.chmod(0)
    try:
        body = TestClient(create_app(tmp_path)).get("/api/trust").json()
    finally:
        q.chmod(0o700)
    assert body["state"] == "ok"                      # panel survives
    assert body["data"]["quarantine_state"] == "unreadable"
    assert body["data"]["quarantined"] == 0           # count withheld, flagged


def test_dangling_quarantine_entry_does_not_kill_the_panel(tmp_path):
    """Probed TOCTOU: the daemon reaps entries concurrently; one vanished
    (or dangling) entry must be skipped, never crash /api/trust whole."""
    _seed(tmp_path)
    q = tmp_path / "state" / "plane" / "spool" / "quarantine"
    q.mkdir(parents=True)
    (q / "dangling.json").symlink_to(q / "never-existed")
    (q / "real.json").write_text("{}")
    (q / "isdir.json").mkdir()                        # not an event either
    body = TestClient(create_app(tmp_path)).get("/api/trust").json()
    assert body["state"] == "ok"
    assert body["data"]["quarantined"] == 1           # only the real file


def test_quarantine_reason_read_through_the_real_door(tmp_path):
    """Consume-by-contract: the sidecar naming is exercised through
    spool.quarantine_entry itself — a rename there must break THIS test,
    not silently break the trust panel."""
    from claudlobby.plane import spool

    _seed(tmp_path)
    sp = spool.spool_dir(tmp_path)
    f = sp / "ev_door.json"
    f.write_text('{"spooled_at": "2026-01-01T00:00:00"}')
    spool.quarantine_entry(tmp_path, f, "pin: through the real door")
    body = TestClient(create_app(tmp_path)).get("/api/trust").json()
    reasons = {r["event"]: r["reason"]
               for r in body["data"]["quarantine_reasons"]}
    assert reasons["ev_door.json"] == "pin: through the real door"


def test_search_discloses_unsearchable_metadata_rows(tmp_path):
    """§11 completeness clause: a metadata-capture room answering
    'no matches' alone is a FALSE IDLE — the panel must state what was
    never indexed."""
    _seed(tmp_path)
    client = TestClient(create_app(tmp_path))
    data_room = client.get("/api/search?q=anything&fleet=data").json()
    assert data_room["data"]["results"] == []
    assert data_room["data"]["unsearchable"] == 1     # the metadata row
    eng = client.get("/api/search?q=huntress&fleet=engineering").json()
    assert eng["data"]["unsearchable"] == 0


def test_search_matches_the_recipient_side_of_a_room(tmp_path):
    """The room axis is sender OR recipient; the recipient arm gets its own
    pin (the channel had one, search did not)."""
    _seed(tmp_path)
    emit_batch(tmp_path, [{
        "event_type": "communication", "emitter": "t", "fleet": "ghostfleet",
        "payload": {"msg_id": "msg_" + "e" * 32,
                    "sender": "bot:ghostfleet/scout",
                    "recipient": "bot:engineering/erlich",
                    "message_class": "report",
                    "body": "crossfleet zebra finding"}}])
    # sender fleet is FULL capture (a metadata sender's body is nulled at
    # the emit door and structurally unfindable — §11, its own pin)
    hits = TestClient(create_app(tmp_path)).get(
        "/api/search?q=zebra&fleet=engineering").json()["data"]["results"]
    assert len(hits) == 1                             # recipient-arm match


# ---------------------------------------------------------------------------
# External-review (Codex, PR #1395 review 5068783751) fix pins
# ---------------------------------------------------------------------------

def test_unreadable_spool_PARENT_is_disclosed_never_a_green_zero(tmp_path):
    """THE Blocker: an unreadable spool ancestor read as spool 0 +
    quarantine ok — Python 3.13+ made is_dir() swallow every OSError, the
    old probe_dir pin died into the red baseline, and the defect resurfaced
    here. probe_dir now reads errnos from os.scandir at call time; both
    counters must disclose, never zero."""
    import os as _os

    if _os.geteuid() == 0:
        pytest.skip("root reads through the mode bits")
    _seed(tmp_path)
    spool = tmp_path / "state" / "plane" / "spool"
    (spool / "quarantine").mkdir(parents=True)
    (spool / "ev_pending.json").write_text(
        '{"spooled_at": "2026-01-01T00:00:00"}')
    spool.chmod(0)                                    # the PARENT, not a leaf
    try:
        body = TestClient(create_app(tmp_path)).get("/api/trust").json()
    finally:
        spool.chmod(0o700)
    assert body["state"] == "ok"                      # panel survives
    assert body["data"]["spool_state"] == "unreadable"
    assert body["data"]["spool_pending"] == 0         # withheld, flagged
    assert body["data"]["quarantine_state"] == "unreadable"


def test_truncated_body_is_disclosed_as_partially_indexed(tmp_path):
    """§11's other half — 'redacted OR TRUNCATED': a term past the capture
    cap is unfindable while the row still looks searchable. The count must
    say so instead of a clean 'no matches'."""
    _seed(tmp_path)
    emit_batch(tmp_path, [{
        "event_type": "communication", "emitter": "t", "fleet": "engineering",
        "payload": {"msg_id": "msg_" + "f" * 32,
                    "sender": "bot:engineering/x",
                    "recipient": "bot:engineering/y",
                    "message_class": "report",
                    "body": ("x" * 16390) + " tailzebra"}}])
    body = TestClient(create_app(tmp_path)).get(
        "/api/search?q=tailzebra&fleet=engineering").json()
    assert body["data"]["results"] == []              # past the cap: unfindable
    assert body["data"]["partially_indexed"] == 1     # …and DISCLOSED


def test_fleet_liveness_never_trusts_producer_clocks(tmp_path):
    """A 2099-dated producer row pinned a fleet at '0s ago' forever under
    MAX(occurred_at). Liveness now keys on ingest_seq -> LEDGER time, the
    same rule the emitter panel follows."""
    _seed(tmp_path)
    emit_batch(tmp_path, [{
        "event_type": "communication", "emitter": "t", "fleet": "engineering",
        "occurred_at": "2099-01-01T00:00:00+00:00",
        "payload": {"msg_id": "msg_" + "9" * 32,
                    "sender": "bot:engineering/skewed",
                    "recipient": "bot:engineering/y",
                    "message_class": "chat", "body": "future-stamped"}}])
    body = TestClient(create_app(tmp_path)).get("/api/trust").json()
    fleets = {f["fleet"]: f for f in body["data"]["fleets"]}
    at = fleets["engineering"]["last_comm_at"]
    assert at is not None and not at.startswith("2099")


def test_trust_panel_polls_while_visible():
    """External review: the trust panel was a one-shot snapshot — green
    forever while events quarantined behind it. Structural pin (no JS
    harness): a bounded poll exists, and visibilitychange pauses it."""
    app_js = (Path(__file__).resolve().parent.parent / "claudlobby"
              / "plane" / "ui" / "app.js").read_text()
    assert "setInterval(pollTrust" in app_js
    assert "clearInterval(trustTimer)" in app_js
    vis = app_js[app_js.index("visibilitychange"):]
    assert "trustTimer" in vis[:600], "visibilitychange must pause trust poll"
