"""#1402 battery: the Telegram carrier hooks — the operator in the stream.

Every pin drives the REAL scripts with hook-shaped stdin against a real
emit root (the cold CLI path end-to-end). The load-bearing laws: the
inbound hook's STDOUT IS EMPTY on every path (UserPromptSubmit stdout is
added to the model's context — leakage would reshape turns fleet-wide);
both hooks are dormant without arming; every path exits 0; an ordinary
prompt or foreign tool call writes nothing.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "lib" / "plane-telegram-out.sh"
IN = REPO / "lib" / "plane-telegram-in.sh"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "emitroot"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    return root


def _env(root: Path, **extra) -> dict:
    import os
    env = {
        # the repo venv leads PATH so plane-emit's cold rung resolves the
        # real `claudlobby` CLI against the FIXTURE root (--root $ROOT)
        "PATH": f"{REPO}/.venv/bin:" + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "CLAUDLOBBY_ROOT": str(root),
        "FLEET_NAME": "test-fleet",
        "BOT_ID": "erlich",
        "PLANE_EMIT_ENABLED": "1",
    }
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


def _run(script: Path, stdin: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)], input=stdin, capture_output=True,
        text=True, env=env, timeout=60)


def _rows(root: Path, sql: str):
    db = root / "state" / "plane" / "plane.db"
    if not db.is_file():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _out_payload(**tool_input) -> str:
    return json.dumps({
        "tool_name": "mcp__plugin_telegram_telegram__reply",
        "tool_input": {"chat_id": "-100999", "text": "the answer",
                       **tool_input},
        "tool_response": {"result": {"message_id": 4242}},
    })


def _channel_prompt(body: str = "please give me a /status update",
                    **attrs) -> str:
    a = {"source": "telegram", "chat_id": "-100999", "message_id": "77",
         "user": "chris", "ts": "2026-09-01T10:00:00Z", **attrs}
    attr_s = " ".join(f'{k}="{v}"' for k, v in a.items())
    return json.dumps({"prompt": f"<channel {attr_s}>{body}</channel>"})


# --- outbound ---------------------------------------------------------------

def test_outbound_records_the_reply_end_to_end(tmp_path):
    root = _root(tmp_path)
    r = _run(OUT, _out_payload(), _env(root))
    assert r.returncode == 0
    comms = _rows(root, "SELECT sender_alias, recipient_raw, body,"
                        " message_class FROM communications")
    assert len(comms) == 1
    assert comms[0]["sender_alias"] == "bot:test-fleet/erlich"
    assert comms[0]["recipient_raw"] == "-100999"
    assert comms[0]["body"] == "the answer"
    tx = _rows(root, "SELECT event, carrier, carrier_ref FROM events"
                     " WHERE kind='transmission'")
    assert len(tx) == 1
    assert tx[0]["event"] == "carrier_accepted"
    assert tx[0]["carrier"] == "telegram-bridge"
    assert tx[0]["carrier_ref"] == "tg:4242"


def test_outbound_ignores_foreign_tools(tmp_path):
    root = _root(tmp_path)
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "ls"}})
    r = _run(OUT, payload, _env(root))
    assert r.returncode == 0
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_outbound_dormant_without_arming(tmp_path):
    root = _root(tmp_path)
    r = _run(OUT, _out_payload(), _env(root, PLANE_EMIT_ENABLED=None))
    assert r.returncode == 0
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_outbound_disabled_exemption_wins(tmp_path):
    root = _root(tmp_path)
    r = _run(OUT, _out_payload(), _env(root, PLANE_EMIT_DISABLED="1"))
    assert r.returncode == 0
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_outbound_survives_broken_stdin(tmp_path):
    root = _root(tmp_path)
    for garbage in ("", "not json", '{"tool_name":'):
        r = _run(OUT, garbage, _env(root))
        assert r.returncode == 0


# --- inbound ----------------------------------------------------------------

def test_inbound_records_the_operator_as_a_first_class_sender(tmp_path):
    root = _root(tmp_path)
    r = _run(IN, _channel_prompt(), _env(root))
    assert r.returncode == 0
    assert r.stdout == ""                    # THE law: stdout feeds the model
    comms = _rows(root, "SELECT sender_alias, recipient_alias, body"
                        " FROM communications")
    assert len(comms) == 1
    assert comms[0]["sender_alias"] == "human:chris"
    assert comms[0]["recipient_alias"] == "bot:test-fleet/erlich"
    assert "status update" in comms[0]["body"]
    tx = _rows(root, "SELECT event, carrier, carrier_ref FROM events"
                     " WHERE kind='transmission'")
    assert tx[0]["event"] == "recipient_acknowledged"
    assert tx[0]["carrier_ref"] == "tg:77"
    # the human is a real actor in the registry
    actors = _rows(root, "SELECT kind FROM identity_registry"
                         " WHERE alias='human:chris'")
    assert [a["kind"] for a in actors] == ["actor"]


def test_inbound_ignores_ordinary_prompts_with_empty_stdout(tmp_path):
    root = _root(tmp_path)
    for prompt in ("fix the bug in parser.py",
                   "channel source=telegram without the tag shape",
                   "<channel source=\"slack\" chat_id=\"1\">hi</channel>"):
        r = _run(IN, json.dumps({"prompt": prompt}), _env(root))
        assert r.returncode == 0
        assert r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_inbound_handles_attr_order_multiline_and_missing_user(tmp_path):
    root = _root(tmp_path)
    # shuffled attrs + multiline body
    p1 = json.dumps({"prompt": '<channel message_id="9" user="ops"'
                     ' source="telegram" chat_id="-5">line one\nline two'
                     '</channel>'})
    assert _run(IN, p1, _env(root)).returncode == 0
    # no user attr -> human:telegram
    p2 = json.dumps({"prompt": '<channel source="telegram" chat_id="-5"'
                     ' message_id="10">hello</channel>'})
    assert _run(IN, p2, _env(root)).returncode == 0
    senders = sorted(r["sender_alias"] for r in _rows(
        root, "SELECT sender_alias FROM communications"))
    assert senders == ["human:ops", "human:telegram"]
    bodies = [r["body"] for r in _rows(
        root, "SELECT body FROM communications ORDER BY ingest_seq")]
    assert "line one\nline two" in bodies[0]


def test_inbound_dormant_and_broken_stdin(tmp_path):
    root = _root(tmp_path)
    r = _run(IN, _channel_prompt(), _env(root, PLANE_EMIT_ENABLED=None))
    assert r.returncode == 0 and r.stdout == ""
    for garbage in ("", "not json"):
        r = _run(IN, garbage, _env(root))
        assert r.returncode == 0 and r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()


# ---------------------------------------------------------------------------
# Gauntlet round-1 fix pins
# ---------------------------------------------------------------------------

def test_outbound_failed_send_records_failed_never_accepted(tmp_path):
    """The round's MAJOR (probed): carrier_accepted is a CARRIER-API FACT,
    and the hook recorded it for sends the carrier refused. Error-shaped
    responses now record `failed` + the error text."""
    for resp in ({"error": "Bad Request: chat not found"},
                 {"isError": True,
                  "content": [{"type": "text",
                               "text": "Error: Forbidden: bot was blocked"}]}):
        root = _root(tmp_path / resp.get("error", "e2")[:8].replace(" ", "_"))
        payload = json.dumps({
            "tool_name": "mcp__plugin_telegram_telegram__reply",
            "tool_input": {"chat_id": "-100999", "text": "hi"},
            "tool_response": resp})
        assert _run(OUT, payload, _env(root)).returncode == 0
        tx = _rows(root, "SELECT event, detail FROM events"
                         " WHERE kind='transmission'")
        assert len(tx) == 1
        assert tx[0]["event"] == "failed"


def test_outbound_extracts_ref_from_array_shaped_response(tmp_path):
    """Spec F1 (measured): real MCP hook responses arrive ARRAY-shaped and
    the fixed-path jq hard-errored — the fallback was dead for exactly the
    shape it targeted. The recursive walk covers both."""
    root = _root(tmp_path)
    payload = json.dumps({
        "tool_name": "mcp__plugin_telegram_telegram__reply",
        "tool_input": {"chat_id": "-100999", "text": "hi"},
        "tool_response": [{"type": "text",
                           "text": "Sent. message_id: 5150"}]})
    assert _run(OUT, payload, _env(root)).returncode == 0
    tx = _rows(root, "SELECT event, carrier_ref FROM events"
                     " WHERE kind='transmission'")
    assert tx[0]["event"] == "carrier_accepted"
    assert tx[0]["carrier_ref"] == "tg:5150"


def test_inbound_records_every_batched_tag(tmp_path):
    """First-match-only silently dropped a second batched message; every
    tag is now recorded with its own msg id."""
    root = _root(tmp_path)
    prompt = ('<channel source="telegram" chat_id="-1" message_id="1"'
              ' user="chris">first ask</channel> and then '
              '<channel source="telegram" chat_id="-1" message_id="2"'
              ' user="chris">second ask</channel>')
    r = _run(IN, json.dumps({"prompt": prompt}), _env(root))
    assert r.returncode == 0 and r.stdout == ""
    bodies = sorted(x["body"] for x in _rows(
        root, "SELECT body FROM communications"))
    assert bodies == ["first ask", "second ask"]


def test_inbound_carries_the_carrier_ts_as_occurred_at(tmp_path):
    """§4 (spec F2): the telegram ts IS the occurrence instant — this hook
    is a relay. An unparseable ts is dropped, never fails the batch."""
    root = _root(tmp_path)
    r = _run(IN, _channel_prompt(ts="2026-09-01T10:00:00Z"), _env(root))
    assert r.returncode == 0
    rows = _rows(root, "SELECT occurred_at FROM communications")
    assert rows[0]["occurred_at"].startswith("2026-09-01T10:00:00")
    root2 = _root(tmp_path / "badts")
    r = _run(IN, _channel_prompt(ts="not-a-time"), _env(root2))
    assert r.returncode == 0
    assert len(_rows(root2, "SELECT 1 FROM communications")) == 1


def test_short_renders_the_human_by_name():
    from claudlobby.plane.view import _short

    assert _short("human:chris") == "chris"
    assert _short("bot:fleet/erlich") == "erlich"
    assert _short("human:") == "human:"     # degenerate stays whole


def test_fleet_rail_shows_participants_never_registry_entities(tmp_path):
    """Operator-flagged regression: the 2b scan minted an identity per
    keyframed entity and the rail rendered all 208 library items. The rail
    is PARTICIPANTS (fleet/actor) — entity identities belong to the
    registry surfaces."""
    from claudlobby.plane.emit_api import emit_batch as _eb

    root = _root(tmp_path)
    _eb(root, [
        {"event_type": "communication", "emitter": "t", "fleet": "f",
         "payload": {"msg_id": "msg_" + "a" * 32, "sender": "bot:f/erlich",
                     "recipient": "bot:f/dinesh", "message_class": "chat",
                     "body": "x"}},
        {"event_type": "registry_snapshot", "emitter": "t", "fleet": "f",
         "payload": {"entity_type": "library_item",
                     "entity_alias": "shared/skills/status",
                     "payload": {"category": "skills", "name": "status",
                                 "source_tier": "shared",
                                 "content_hash": "h", "declared_hash": "h",
                                 "schema_version": "1"},
                     "cause": "generate", "scan_id": "s"}}])
    import sys
    sys.path.insert(0, str(REPO))
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app
    kinds = {r["kind"] for r in TestClient(create_app(root)).get(
        "/api/identities").json()["data"]["identities"]}
    assert "library_item" not in kinds
    assert "actor" in kinds and "fleet" in kinds
