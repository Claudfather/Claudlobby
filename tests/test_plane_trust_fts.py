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
    assert "\x01" in snip and "\x02" in snip     # markers, never markup
    assert "<" not in snip.replace("\x01", "").replace("\x02", "")


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
                 "*", '"" ""', "  "):
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
