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

from tests.plane_setup import initialize_plane

from tests.package_fixtures import source_package
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
    initialize_plane(root)
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
    initialize_plane(tmp_path)
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
    body = TestClient(create_app(root, package=source_package())).get("/api/channel").json()
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


# --- #3001: a send typed but not submitted keeps its wire proof -------------
# Native rc 3 means the text was typed into the box and not submitted, so the
# send stays `unknown`. Its wire proof rides that row, and when the message has
# no submission-class fact the JOIN reads it there, so a receipt recorded once
# the text is submitted later reads delivered, truncated or altered. Before
# #3001 the contract refused a wire proof on an `unknown` fact.

GLUED = "text the box already held " + BODY   # #2060: typed onto a held box


def _typed(mid, wire=None, *, attempt=1):
    """The `unknown` fact of a send typed but not submitted (native rc 3), with
    the wire proof the transport observed (`wire=None`: none was observed)."""
    row = _submitted(mid, wire, state="unknown")
    row["payload"]["attempt_no"] = attempt
    return row


def _seed_typed(root):
    _full_capture(root)
    initialize_plane(root)
    later = _submitted(_mid("e"), None)          # a later attempt, submitted without a proof
    later["payload"]["attempt_no"] = 2
    emit_batch(root, [
        # typed, then submitted later and received whole
        _comm(_mid("a"), BODY), _typed(_mid("a"), BODY), _received(_mid("a"), BODY),
        # typed onto text the box already held, and submitted with it
        _comm(_mid("b"), BODY), _typed(_mid("b"), BODY), _received(_mid("b"), GLUED),
        # typed, then only a prefix of it arrived
        _comm(_mid("c"), BODY), _typed(_mid("c"), BODY), _received(_mid("c"), BODY[:-10]),
        # typed and never submitted: no receipt
        _comm(_mid("d"), BODY), _typed(_mid("d"), BODY),
        # a submission-class fact is still the only proof read when there is one
        _comm(_mid("e"), BODY), _typed(_mid("e"), BODY), later, _received(_mid("e"), BODY),
        # a later attempt observed no proof: the newest typed fact WITH one is read
        _comm(_mid("f"), BODY), _typed(_mid("f"), BODY), _typed(_mid("f"), None, attempt=2),
        _received(_mid("f"), BODY),
    ])


def _typed_rows(root):
    db = root / "state" / "plane" / "plane.db"
    conn = connect(str(db))
    migrate(conn)
    conn.row_factory = sqlite3.Row
    ids = [_mid(t) for t in "abcdef"]
    delivery = {r["msg_id"]: dict(r) for r in conn.execute(
        q.DELIVERY_STATUS_SQL.format(ph=",".join("?" * len(ids))), ids)}
    proofs = {r[0]: r[1] for r in conn.execute(
        "SELECT msg_id, json_extract(detail, '$.wire_sha256') FROM events"
        " WHERE kind='transmission' AND event='unknown' AND attempt_no=1")}
    return delivery, proofs


def test_a_send_typed_then_submitted_later_reads_delivered(tmp_path):
    _seed_typed(tmp_path)
    delivery, proofs = _typed_rows(tmp_path)
    assert proofs[_mid("a")] == _sha(BODY)
    assert delivery[_mid("a")]["delivery"] == "delivered"


def test_a_typed_send_glued_to_text_the_box_held_reads_altered(tmp_path):
    _seed_typed(tmp_path)
    row = _typed_rows(tmp_path)[0][_mid("b")]
    assert row["delivery"] == "altered"
    assert row["received_bytes"] > row["wire_bytes"]   # longer, another hash: not truncated


def test_a_typed_send_cut_short_reads_truncated_against_its_own_wire_bytes(tmp_path):
    _seed_typed(tmp_path)
    row = _typed_rows(tmp_path)[0][_mid("c")]
    assert row["delivery"] == "truncated"
    assert row["wire_bytes"] - row["received_bytes"] == 10   # the view's shortfall


def test_a_typed_send_with_no_receipt_has_no_verdict(tmp_path):
    """The proof alone is never a verdict: with no receipt, a send whose
    submission is unproven reads NULL, as before, never 'unconfirmed'."""
    _seed_typed(tmp_path)
    delivery, proofs = _typed_rows(tmp_path)
    assert proofs[_mid("d")] == _sha(BODY)
    assert delivery[_mid("d")]["delivery"] is None


def test_a_submission_class_fact_is_still_the_only_proof_read_when_there_is_one(tmp_path):
    """The typed fact's proof is read only when the message has no
    submission-class fact. Here a later attempt was submitted without a proof,
    so the receipt has nothing to compare and reads 'unconfirmed', as before."""
    _seed_typed(tmp_path)
    delivery, proofs = _typed_rows(tmp_path)
    assert proofs[_mid("e")] == _sha(BODY)
    assert delivery[_mid("e")]["delivery"] == "unconfirmed"


def test_the_newest_typed_fact_that_carries_a_proof_is_read(tmp_path):
    _seed_typed(tmp_path)
    assert _typed_rows(tmp_path)[0][_mid("f")]["delivery"] == "delivered"


def test_the_view_says_a_typed_send_submitted_later_was_confirmed(tmp_path):
    _seed_typed(tmp_path)
    msgs = _channel(tmp_path)
    assert msgs[_mid("a")]["delivery_state"] == "confirmed by the receiver"
    assert msgs[_mid("b")]["delivery_state"] == "ARRIVED ALTERED"
    assert msgs[_mid("c")]["delivery_state"] == "ARRIVED SHORT by 10 bytes"
    assert msgs[_mid("d")]["delivery_state"] is None


def test_the_contract_takes_a_wire_proof_on_a_tmux_unknown_fact_and_no_other_uncertain_one():
    from claudlobby.plane.contracts import Transmission
    base = {"msg_id": _mid("a"), "attempt_no": 1, "destination": DEST}
    proof = {"wire_sha256": _sha(BODY), "wire_bytes": _nbytes(BODY)}
    typed = Transmission(carrier="tmux", state="unknown", **base, **proof)
    assert (typed.wire_sha256, typed.wire_bytes) == (_sha(BODY), _nbytes(BODY))
    for carrier, state in (("tmux", "failed"), ("tmux", "send_attempted"),
                           ("telegram-tgpost", "unknown"), ("telegram-bridge", "unknown")):
        with pytest.raises(ValueError, match="only valid on"):
            Transmission(carrier=carrier, state=state, **base, **proof)
    with pytest.raises(ValueError, match="together"):
        Transmission(carrier="tmux", state="unknown", **base, wire_sha256=_sha(BODY))


# --- #3003: what the join's reading of a typed fact leaves open ---------------
# A send typed again records another `unknown` fact with its own proof, and the
# join reads the newest; a `carrier_queued` fact is submission-class, so it
# makes a send with no receipt read `unconfirmed` and is the only proof read.

def _join(root, tags):
    conn = connect(str(root / "state" / "plane" / "plane.db"))
    migrate(conn)
    conn.row_factory = sqlite3.Row
    ids = [_mid(t) for t in tags]
    return {r["msg_id"]: dict(r) for r in conn.execute(
        q.DELIVERY_STATUS_SQL.format(ph=",".join("?" * len(ids))), ids)}


def test_the_newest_of_several_typed_facts_with_a_proof_is_the_one_read(tmp_path):
    _full_capture(tmp_path)
    initialize_plane(tmp_path)
    other = BODY[:-1] + "Z"                      # the same length, another hash
    emit_batch(tmp_path, [
        # the older fact names other bytes, the newest names what arrived
        _comm(_mid("a"), BODY), _typed(_mid("a"), other), _typed(_mid("a"), BODY, attempt=2),
        _received(_mid("a"), BODY),
        # the older fact names what arrived, the newest does not
        _comm(_mid("b"), BODY), _typed(_mid("b"), BODY), _typed(_mid("b"), other, attempt=2),
        _received(_mid("b"), BODY),
    ])
    rows = _join(tmp_path, "ab")
    assert rows[_mid("a")]["delivery"] == "delivered"
    assert rows[_mid("b")]["delivery"] == "altered"


def test_a_carrier_queued_fact_counts_as_submitted_and_is_the_only_proof_read(tmp_path):
    _full_capture(tmp_path)
    initialize_plane(tmp_path)
    queued = _submitted(_mid("d"), None, state="carrier_queued")
    queued["payload"]["attempt_no"] = 2
    emit_batch(tmp_path, [
        # queued and not yet received: submitted, so unconfirmed (an `unknown` fact alone reads NULL)
        _comm(_mid("c"), BODY), _submitted(_mid("c"), None, state="carrier_queued"),
        # queued after a typed fact: the typed fact's proof is not read, so the receipt has nothing to compare
        _comm(_mid("d"), BODY), _typed(_mid("d"), BODY), queued, _received(_mid("d"), BODY),
    ])
    rows = _join(tmp_path, "cd")
    assert rows[_mid("c")]["delivery"] == "unconfirmed"
    assert rows[_mid("d")]["delivery"] == "unconfirmed"
