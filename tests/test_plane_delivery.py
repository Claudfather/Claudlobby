"""chunk P (#1501) + fold F1/F3: the delivery JOIN — queries.DELIVERY_STATUS_SQL
(the ONE definition) and the operator view's plain-language rendering of it.

Delivery is the JOIN of the SENDER's WIRE proof (the exact bytes bot_tmux_send
put on the wire — wire_sha256 / wire_bytes on a submission-class transmission)
and the RECEIVER's `received` proof. fold F1 moved the basis OFF the raw body
hash: the receiver hashes what came off the wire, which sanitize_tmux_input
rewrote, so the raw-body comparison read every multi-line / tabbed dispatch as
ALTERED though fully delivered. These pin the four verdicts on a seeded plane,
the fold's guard (altered needs a wire proof) and destination match (F3), the
TRUNCATED-vs-ALTERED boundary (`<`, not `<=`/`!=`), and the view's story-first
phrasing (DELIVERED -> "confirmed by the receiver", TRUNCATED -> loud "ARRIVED
SHORT by N bytes", UNCONFIRMED -> "sent, not yet confirmed").
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from claudlobby.plane import queries as q
from claudlobby.plane.db import connect
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.migrations import migrate

BODY = "[BOTCOMMAND] erlich | task | Onboard a new ingest source: KAIT"
# A dispatch whose STORED body is multi-line but whose WIRE form (what sanitize
# put on the wire, what the receiver got) is the one-line collapse. body_sha256
# and wire_sha256 differ here, so a delivered verdict is only correct when the
# JOIN compares the WIRE hash — the whole point of the fold's F1.
MLINE_BODY = "do the first thing\n\nthen the second\tindented"
MLINE_WIRE = "do the first thing then the second indented"
DEST = "ramanujan"                       # recipient_raw AND the received destination


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _mid(tag: str) -> str:
    return "msg_" + (tag * 32)[:32]


def _full_capture(root):
    d = root / "state" / "plane"
    d.mkdir(parents=True, exist_ok=True)
    (d / "capture.json").write_text('{"*": "full"}')


def _comm(mid, body):
    # fold F3: recipient_raw is the short name the receiver records as its
    # `received` destination — the JOIN admits the proof only when they match.
    return {"event_type": "communication", "emitter": "t", "fleet": "f",
            "payload": {"msg_id": mid, "sender": "bot:f/erlich",
                        "recipient": f"bot:f/{DEST}", "recipient_raw": DEST,
                        "message_class": "task_request", "command_type": "task",
                        "body": body}}


def _submitted(mid, wire=None, *, state="pane_submitted"):
    """A submission-class transmission. fold F1: it carries the SENDER's wire
    proof (sha256 + byte length of the bytes put on the wire). `wire=None`
    models a sha-less host — a submission with no proof, which the JOIN's guard
    must keep OUT of 'altered'."""
    payload = {"msg_id": mid, "attempt_no": 1, "carrier": "tmux",
               "destination": DEST, "state": state}
    if wire is not None:
        payload["wire_sha256"] = _sha(wire)
        payload["wire_bytes"] = _nbytes(wire)
    return {"event_type": "transmission", "emitter": "t", "fleet": "f",
            "payload": payload}


def _received(mid, text, *, dest=DEST):
    return {"event_type": "transmission", "emitter": "t", "fleet": "f",
            "payload": {"msg_id": mid, "attempt_no": 1, "carrier": "tmux",
                        "destination": dest, "state": "received",
                        "received_bytes": _nbytes(text),
                        "received_sha256": _sha(text)}}


def _seed(root):
    """One message per delivery verdict the derivation must produce. The wire
    proof, not the body, is the reference — so the wire text passed to
    _submitted is what a received is judged against."""
    _full_capture(root)
    trunc = BODY[:-10]                       # a strict prefix -> shorter
    altered = BODY[:-1] + "Z"                # SAME length, different sha
    longer = BODY + " and then some more"    # LONGER, different sha
    emit_batch(root, [
        _comm(_mid("1"), BODY), _submitted(_mid("1"), BODY), _received(_mid("1"), BODY),
        _comm(_mid("2"), BODY), _submitted(_mid("2"), BODY), _received(_mid("2"), trunc),
        _comm(_mid("3"), BODY), _submitted(_mid("3"), BODY),
        _comm(_mid("4"), BODY), _submitted(_mid("4"), BODY), _received(_mid("4"), altered),
        _comm(_mid("5"), BODY),                          # no submission at all
        # fold: a received addressed to the WRONG destination is inert (F3)
        _comm(_mid("6"), BODY), _submitted(_mid("6"), BODY),
        _received(_mid("6"), BODY, dest="someone-else"),
        # the mutant boundary: a LONGER arrival with a differing sha is ALTERED,
        # never TRUNCATED (kills `received_bytes != wire_bytes`)
        _comm(_mid("7"), BODY), _submitted(_mid("7"), BODY), _received(_mid("7"), longer),
        # the guard: a received with NO wire proof to compare cannot be 'altered'
        _comm(_mid("8"), BODY), _submitted(_mid("8"), None), _received(_mid("8"), altered),
        # THE WIRE-NOT-BODY pin (kills the mutant that compares body_sha256): the
        # body the plane STORED differs from what went on the WIRE (a multi-line
        # dispatch sanitize collapses to one line). The received matches the WIRE,
        # so the message is DELIVERED — but only if the JOIN reads wire_sha256.
        # Comparing body_sha256 (the raw multi-line form) would read 'altered'.
        _comm(_mid("9"), MLINE_BODY), _submitted(_mid("9"), MLINE_WIRE),
        _received(_mid("9"), MLINE_WIRE),
    ])


def _delivery(root):
    db = root / "state" / "plane" / "plane.db"
    conn = connect(str(db))
    migrate(conn)
    conn.row_factory = sqlite3.Row
    ids = [_mid(t) for t in "123456789"]
    ph = ",".join("?" * len(ids))
    return {r["msg_id"]: dict(r)
            for r in conn.execute(q.DELIVERY_STATUS_SQL.format(ph=ph), ids)}


# --- the derivation (the one definition), on a seeded plane -----------------

def test_delivered_when_received_sha_equals_wire_sha(tmp_path):
    _seed(tmp_path)
    assert _delivery(tmp_path)[_mid("1")]["delivery"] == "delivered"


def test_delivered_compares_the_WIRE_not_the_body(tmp_path):
    # msg 9's stored body is multi-line; its wire form (what the receiver got)
    # is the one-line collapse, so body_sha256 != wire_sha256. A received that
    # matches the WIRE must read DELIVERED. If the JOIN compared body_sha256 it
    # would read 'altered' — this is the pin for the fold's whole reason.
    _seed(tmp_path)
    assert _delivery(tmp_path)[_mid("9")]["delivery"] == "delivered"


def test_truncated_when_received_is_shorter(tmp_path):
    _seed(tmp_path)
    row = _delivery(tmp_path)[_mid("2")]
    assert row["delivery"] == "truncated"
    # the shortfall is readable off the row (wire_bytes - received_bytes) — fold
    # F1 made the reference the WIRE size, not the raw body
    assert row["wire_bytes"] - row["received_bytes"] == 10


def test_unconfirmed_when_submitted_but_no_received(tmp_path):
    _seed(tmp_path)
    assert _delivery(tmp_path)[_mid("3")]["delivery"] == "unconfirmed"


def test_altered_when_received_differs_but_is_not_shorter(tmp_path):
    _seed(tmp_path)
    assert _delivery(tmp_path)[_mid("4")]["delivery"] == "altered"


def test_null_when_never_submitted_to_a_pane(tmp_path):
    _seed(tmp_path)
    assert _delivery(tmp_path)[_mid("5")]["delivery"] is None


def test_a_received_to_the_wrong_destination_yields_no_verdict(tmp_path):
    """fold F3: a `received` whose destination is not the communication's
    recipient (a misdirected receipt, or an untracked prompt quoting a real
    trailer) must NOT produce a delivery verdict. Here a submission exists, so
    the message reads UNCONFIRMED — never 'delivered' off the wrong bot's row."""
    _seed(tmp_path)
    assert _delivery(tmp_path)[_mid("6")]["delivery"] == "unconfirmed"


def test_a_longer_arrival_with_a_differing_sha_is_altered_not_truncated(tmp_path):
    """The TRUNCATED/ALTERED boundary is `received_bytes < wire_bytes`, never
    `!=` or `<=`: an arrival that is EQUAL-or-LONGER but hashes differently is
    ALTERED (transformed), not TRUNCATED (tail loss). mid 4 pins the equal-length
    half (`<=`); this pins the longer half (`!=`)."""
    _seed(tmp_path)
    row = _delivery(tmp_path)[_mid("7")]
    assert row["delivery"] == "altered"
    assert row["received_bytes"] > row["wire_bytes"]   # genuinely longer


def test_a_received_with_no_wire_proof_is_never_altered(tmp_path):
    """fold guard: 'altered' means "compared against the wire and differs" — you
    cannot call an arrival altered with nothing to compare it to. A submission
    that carried no wire proof (a sha-less host, a pre-fold row) plus a received
    reads UNCONFIRMED, never a false 'ARRIVED ALTERED' alarm."""
    _seed(tmp_path)
    assert _delivery(tmp_path)[_mid("8")]["delivery"] == "unconfirmed"


def test_newest_received_wins(tmp_path):
    """A later `received` supersedes an earlier one for the same msg_id — the
    MAX(ingest_seq) rule (defensive; a re-send mints a fresh id in practice)."""
    _full_capture(tmp_path)
    emit_batch(tmp_path, [_comm(_mid("9"), BODY), _submitted(_mid("9"), BODY),
                          _received(_mid("9"), BODY[:-5])])   # first: short
    emit_batch(tmp_path, [_received(_mid("9"), BODY)])        # then: whole
    db = tmp_path / "state" / "plane" / "plane.db"
    conn = connect(str(db)); migrate(conn); conn.row_factory = sqlite3.Row
    row = dict(conn.execute(q.DELIVERY_STATUS_SQL.format(ph="?"),
                            [_mid("9")]).fetchone())
    assert row["delivery"] == "delivered"


# --- the view: plain-language delivery, story-first -------------------------

def _channel(root):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from claudlobby.plane.view import create_app
    body = TestClient(create_app(root)).get("/api/channel").json()
    msgs = {}
    for t in body["data"]["threads"]:
        for m in t["messages"]:
            msgs[m["msg_id"]] = m
    return msgs


def test_view_renders_each_delivery_verdict_in_plain_language(tmp_path):
    _seed(tmp_path)
    msgs = _channel(tmp_path)
    assert msgs[_mid("1")]["delivery_state"] == "confirmed by the receiver"
    assert msgs[_mid("2")]["delivery_state"] == "ARRIVED SHORT by 10 bytes"
    assert msgs[_mid("3")]["delivery_state"] == "sent, not yet confirmed"
    assert msgs[_mid("4")]["delivery_state"] == "ARRIVED ALTERED"
    # a message never submitted to a pane carries no delivery line at all
    assert msgs[_mid("5")]["delivery"] is None
    assert msgs[_mid("5")]["delivery_state"] is None


def test_view_carries_the_machine_status_beside_the_phrase(tmp_path):
    _seed(tmp_path)
    msgs = _channel(tmp_path)
    assert msgs[_mid("1")]["delivery"] == "delivered"
    assert msgs[_mid("2")]["delivery"] == "truncated"


def test_view_excludes_the_received_row_from_the_display_tx(tmp_path):
    """fold F4 (view half): `received` is the receiver's proof, rendered ONLY as
    delivery_state. It must not enter the display tx list, or app.js's latest-tx
    would show the raw token "received" as the newest carrier state."""
    _seed(tmp_path)
    msgs = _channel(tmp_path)
    m1 = msgs[_mid("1")]
    assert m1["delivery_state"] == "confirmed by the receiver"
    assert [t["event"] for t in m1["tx"]] == ["pane_submitted"]  # no 'received'
    assert all(t["event"] != "received" for t in m1["tx"])
