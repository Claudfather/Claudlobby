"""chunk P (#1501): the delivery JOIN — queries.DELIVERY_STATUS_SQL (the ONE
definition) and the operator view's plain-language rendering of it.

Delivery is the JOIN of the SENDER's body hash and the RECEIVER's `received`
proof; these pin the four verdicts on a seeded plane and the view's story-first
phrasing (DELIVERED -> "confirmed by the receiver", TRUNCATED -> loud "ARRIVED
SHORT by N bytes", UNCONFIRMED -> "sent, not yet confirmed").
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from claudlobby.plane import queries as q
from claudlobby.plane.db import connect
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.migrations import migrate

BODY = "[BOTCOMMAND] erlich | task | Onboard a new ingest source: KAIT"


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mid(tag: str) -> str:
    return "msg_" + (tag * 32)[:32]


def _full_capture(root):
    d = root / "state" / "plane"
    d.mkdir(parents=True, exist_ok=True)
    (d / "capture.json").write_text('{"*": "full"}')


def _comm(mid, body):
    return {"event_type": "communication", "emitter": "t", "fleet": "f",
            "payload": {"msg_id": mid, "sender": "bot:f/erlich",
                        "recipient": "bot:f/ramanujan",
                        "message_class": "task_request", "command_type": "task",
                        "body": body}}


def _submitted(mid):
    return {"event_type": "transmission", "emitter": "t", "fleet": "f",
            "payload": {"msg_id": mid, "attempt_no": 1, "carrier": "tmux",
                        "destination": "ramanujan", "state": "pane_submitted"}}


def _received(mid, text):
    return {"event_type": "transmission", "emitter": "t", "fleet": "f",
            "payload": {"msg_id": mid, "attempt_no": 1, "carrier": "tmux",
                        "destination": "ramanujan", "state": "received",
                        "received_bytes": len(text.encode("utf-8")),
                        "received_sha256": _sha(text)}}


def _seed(root):
    """Five messages, one per delivery verdict the derivation must produce."""
    _full_capture(root)
    trunc = BODY[:-10]                       # a strict prefix -> shorter
    altered = BODY[:-1] + "Z"                # same length, different sha
    emit_batch(root, [
        _comm(_mid("1"), BODY), _submitted(_mid("1")), _received(_mid("1"), BODY),
        _comm(_mid("2"), BODY), _submitted(_mid("2")), _received(_mid("2"), trunc),
        _comm(_mid("3"), BODY), _submitted(_mid("3")),
        _comm(_mid("4"), BODY), _submitted(_mid("4")), _received(_mid("4"), altered),
        _comm(_mid("5"), BODY),                          # no submission at all
    ])


def _delivery(root):
    db = root / "state" / "plane" / "plane.db"
    conn = connect(str(db))
    migrate(conn)
    conn.row_factory = sqlite3.Row
    ids = [_mid(t) for t in "12345"]
    ph = ",".join("?" * len(ids))
    return {r["msg_id"]: dict(r)
            for r in conn.execute(q.DELIVERY_STATUS_SQL.format(ph=ph), ids)}


# --- the derivation (the one definition), on a seeded plane -----------------

def test_delivered_when_received_sha_equals_body_sha(tmp_path):
    _seed(tmp_path)
    assert _delivery(tmp_path)[_mid("1")]["delivery"] == "delivered"


def test_truncated_when_received_is_shorter(tmp_path):
    _seed(tmp_path)
    row = _delivery(tmp_path)[_mid("2")]
    assert row["delivery"] == "truncated"
    # the shortfall is readable off the row (body_bytes - received_bytes)
    assert row["body_bytes"] - row["received_bytes"] == 10


def test_unconfirmed_when_submitted_but_no_received(tmp_path):
    _seed(tmp_path)
    assert _delivery(tmp_path)[_mid("3")]["delivery"] == "unconfirmed"


def test_altered_when_received_differs_but_is_not_shorter(tmp_path):
    _seed(tmp_path)
    assert _delivery(tmp_path)[_mid("4")]["delivery"] == "altered"


def test_null_when_never_submitted_to_a_pane(tmp_path):
    _seed(tmp_path)
    assert _delivery(tmp_path)[_mid("5")]["delivery"] is None


def test_newest_received_wins(tmp_path):
    """A later `received` supersedes an earlier one for the same msg_id — the
    MAX(ingest_seq) rule (defensive; a re-send mints a fresh id in practice)."""
    _full_capture(tmp_path)
    emit_batch(tmp_path, [_comm(_mid("9"), BODY), _submitted(_mid("9")),
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
