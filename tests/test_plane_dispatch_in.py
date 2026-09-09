"""chunk P (#1501) + fold F1: the receiver hook lib/plane-dispatch-in.sh, end to
end through the real cold emit root (the test_plane_telegram_hooks.py shape).

The load-bearing laws, all pinned here:
  * STDOUT IS EMPTY on every path (UserPromptSubmit stdout feeds the model).
  * THE STRIP BOUNDARY IS EXACT (fold F1): the receiver's received_sha256 /
    received_bytes equal the SENDER's WIRE proof — sha256_prefixed(safe) and the
    byte length of `safe`, where `safe = sanitize_tmux_input(payload)` is the
    EXACT bytes bot_tmux_send put on the wire (the `set +H; ` prefix included,
    the trailer NOT). This is proven with a REAL round-trip that runs the ACTUAL
    bash send-path functions AND the ACTUAL hook — never a python re-derivation
    of either — so the tooling and the runtime are asserted to agree. And it is
    proven over SANITIZE-UNSTABLE shapes (multi-line, blank-line, tabbed,
    double-spaced, leading-control), which the pre-fold body-hash comparison
    read as ALTERED/TRUNCATED though fully delivered — the whole reason F1 exists.
  * an un-tokened prompt records NOTHING and exits at the zero-fork prefilter.
  * PLANE_EMIT_DISABLED=1 silences; a missing FLEET_NAME/BOT_ID does not record.

THE FIXTURE IS GROUNDED IN THE LIVE PIPELINE (the #1404/#1411 rule): the wire
form is produced by the REAL sanitize_tmux_input and the arrival is laid out the
way bot_tmux_send lays it (safe, then the trailer on its own last line). Only
the identifiers are faked (public repo). It is NOT hand-written from the contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "lib" / "plane-dispatch-in.sh"
LIB = REPO / "lib"

# a real minted-shape id (msg_ + 32 hex), value faked
MSGID = "msg_1f3c9a7b2e5d4068a1b2c3d4e5f60718"
BOT = "erlich"                              # BOT_ID; also the comm's recipient_raw
FLEET = "test-fleet"
# the message PROPER — a [BOTCOMMAND] envelope. dispatch.sh prepends `set +H; `.
BODY = "[BOTCOMMAND] erlich | task | Onboard a new ingest source: KAIT — see #1501"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "emitroot"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    return root


def _env(root: Path, **extra) -> dict:
    env = {
        "PATH": f"{REPO}/.venv/bin:" + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "CLAUDLOBBY_ROOT": str(root),
        "FLEET_NAME": FLEET,
        "BOT_ID": BOT,
    }
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


def _run(stdin: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)], input=stdin, capture_output=True, text=True,
        env=env, timeout=60)


def _rows(root: Path, sql: str):
    db = root / "state" / "plane" / "plane.db"
    if not db.is_file():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _received_row(root: Path):
    # destination rides `detail` (a transmission has no destination column)
    return _rows(root, "SELECT event, carrier, msg_id, detail"
                       " FROM events WHERE kind='transmission' AND event='received'")


# --- the SENDER's wire proof, from the REAL bash send-path functions ---------

def _wire_proof(payload: str) -> tuple[str, str, int]:
    """Run the ACTUAL bot_tmux_send building blocks — sanitize_tmux_input, then
    sha256_prefixed and `wc -c` over the result — so the test asserts the
    SENDER's tooling and the RECEIVER's hook agree, not a python model of either.
    Returns (wire_sha256, safe, wire_bytes). `safe` never contains a newline
    (sanitize collapses them), so the three fields print on three lines."""
    prog = (
        'set -eu; . "$1"/lib-common.sh; '
        'safe=$(sanitize_tmux_input "$2"); '
        'printf "sha=%s\\n" "$(sha256_prefixed "$safe")"; '
        'printf "bytes=%s\\n" "$(printf %s "$safe" | wc -c | tr -d " ")"; '
        'printf "safe=%s" "$safe"'
    )
    r = subprocess.run(["bash", "-c", prog, "_", str(LIB), payload],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    sha = safe = None
    nbytes = None
    for line in r.stdout.split("\n"):
        key, _, val = line.partition("=")
        if key == "sha":
            sha = val
        elif key == "bytes":
            nbytes = int(val)
        elif key == "safe":
            safe = val
    assert sha and safe is not None and nbytes is not None, r.stdout
    return sha, safe, nbytes


def _arrival(safe: str, msgid: str = MSGID) -> str:
    """What the RECEIVER'S session sees: the wire form, then the trailer on its
    OWN final line (a single '\\n' separator), the way bot_tmux_send lays it."""
    return f"{safe}\n⟦plane:{msgid}⟧"


def _hookjson(prompt: str, *, ensure_ascii: bool) -> str:
    """The hook JSON on the wire. ensure_ascii toggles the two encodings the
    prefilter must survive: the bracket as `\\u27e6` (Python's default) vs the
    literal UTF-8 byte."""
    return json.dumps({"prompt": prompt}, ensure_ascii=ensure_ascii)


# --- the crux: received == the sender's WIRE proof, for EVERY shape ----------

# Each shape is the MESSAGE PROPER; the wire is sanitize_tmux_input("set +H; " +
# body) for a prose dispatch. The pre-fold body-hash comparison read all but the
# first as ALTERED or TRUNCATED though fully delivered.
_SHAPES = {
    "single_line": BODY,
    "multi_line": "Do X.\nThen Y.\nThen Z.",
    "blank_line": "First paragraph.\n\nSecond paragraph.",
    "tab_indented": "steps:\n\tone\n\ttwo",
    "double_space": "fix  the  bug  now",
    "leading_control": "\x01\x02 urgent: ship it",
    "trailing_newline": "Fix the bug\n",
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("ensure_ascii", [True, False], ids=["escaped", "literal"])
def test_received_equals_the_wire_proof_for_every_shape(tmp_path, shape, ensure_ascii):
    body = _SHAPES[shape]
    payload = "set +H; " + body                # what dispatch.sh sends for prose
    sha, safe, nbytes = _wire_proof(payload)
    root = _root(tmp_path)
    r = _run(_hookjson(_arrival(safe), ensure_ascii=ensure_ascii), _env(root))
    assert r.returncode == 0
    assert r.stdout == ""                      # THE law: stdout feeds the model
    rows = _received_row(root)
    assert len(rows) == 1
    d = json.loads(rows[0]["detail"])
    assert d["destination"] == BOT
    # THE crux: the receiver's proof equals the sender's WIRE proof byte-for-byte
    assert d["received_sha256"] == sha
    assert d["received_bytes"] == nbytes


def test_a_slash_command_briefing_carries_no_prefix_and_still_round_trips(tmp_path):
    """report-back and a `/briefing` send go through bot_tmux_send directly (no
    `set +H; `), so the hook must round-trip a trailer-only wire form too."""
    sha, safe, nbytes = _wire_proof("/briefing morning")
    root = _root(tmp_path)
    r = _run(_hookjson(_arrival(safe), ensure_ascii=False), _env(root))
    assert r.returncode == 0 and r.stdout == ""
    d = json.loads(_received_row(root)[0]["detail"])
    assert d["received_sha256"] == sha
    assert d["received_bytes"] == nbytes


def test_a_whole_multiline_dispatch_reads_DELIVERED_through_the_join(tmp_path):
    """The acceptance test F1 exists for: a fully-delivered MULTI-LINE dispatch
    classifies DELIVERED, not ALTERED. Real sanitize + real hook land the
    `received`; a seeded pane_submitted carries the same wire proof; the JOIN
    agrees. Pre-fold this exact shape read ALTERED (body-hash comparison)."""
    from claudlobby.plane import queries as q
    from claudlobby.plane.db import connect
    from claudlobby.plane.emit_api import emit_batch
    from claudlobby.plane.migrations import migrate

    payload = "set +H; Do X.\n\nThen Y across\nseveral lines."
    sha, safe, nbytes = _wire_proof(payload)
    root = _root(tmp_path)
    emit_batch(root, [
        {"event_type": "communication", "emitter": "t", "fleet": FLEET,
         "payload": {"msg_id": MSGID, "sender": f"bot:{FLEET}/mgr",
                     "recipient": f"bot:{FLEET}/{BOT}", "recipient_raw": BOT,
                     "message_class": "task_request", "command_type": "task",
                     "body": payload}},
        {"event_type": "transmission", "emitter": "t", "fleet": FLEET,
         "payload": {"msg_id": MSGID, "attempt_no": 1, "carrier": "tmux",
                     "destination": BOT, "state": "pane_submitted",
                     "wire_sha256": sha, "wire_bytes": nbytes}},
    ])
    r = _run(_hookjson(_arrival(safe), ensure_ascii=False), _env(root))
    assert r.returncode == 0 and r.stdout == ""
    db = root / "state" / "plane" / "plane.db"
    conn = connect(str(db)); migrate(conn); conn.row_factory = sqlite3.Row
    row = dict(conn.execute(q.DELIVERY_STATUS_SQL.format(ph="?"), [MSGID]).fetchone())
    assert row["delivery"] == "delivered"


def test_a_genuinely_shortened_arrival_reads_TRUNCATED_through_the_join(tmp_path):
    """TRUNCATED is reserved for REAL tail loss: the wire proof is the whole
    message, the arrival is a strict prefix of it (the trailer still rode the
    last chunk), so received_bytes < wire_bytes and the JOIN reads truncated."""
    from claudlobby.plane import queries as q
    from claudlobby.plane.db import connect
    from claudlobby.plane.emit_api import emit_batch
    from claudlobby.plane.migrations import migrate

    payload = "set +H; " + BODY
    sha, safe, nbytes = _wire_proof(payload)
    root = _root(tmp_path)
    emit_batch(root, [
        {"event_type": "communication", "emitter": "t", "fleet": FLEET,
         "payload": {"msg_id": MSGID, "sender": f"bot:{FLEET}/mgr",
                     "recipient": f"bot:{FLEET}/{BOT}", "recipient_raw": BOT,
                     "message_class": "task_request", "command_type": "task",
                     "body": payload}},
        {"event_type": "transmission", "emitter": "t", "fleet": FLEET,
         "payload": {"msg_id": MSGID, "attempt_no": 1, "carrier": "tmux",
                     "destination": BOT, "state": "pane_submitted",
                     "wire_sha256": sha, "wire_bytes": nbytes}},
    ])
    # a shortened arrival: drop the tail of the wire form, keep the trailer
    r = _run(_hookjson(_arrival(safe[:-12]), ensure_ascii=False), _env(root))
    assert r.returncode == 0 and r.stdout == ""
    db = root / "state" / "plane" / "plane.db"
    conn = connect(str(db)); migrate(conn); conn.row_factory = sqlite3.Row
    row = dict(conn.execute(q.DELIVERY_STATUS_SQL.format(ph="?"), [MSGID]).fetchone())
    assert row["delivery"] == "truncated"


# --- the prefilter / recogniser ---------------------------------------------

def test_an_untokened_prompt_records_nothing_and_stdout_is_empty(tmp_path):
    root = _root(tmp_path)
    for prompt in (
        "fix the flaky test in tests/test_auth.py",
        "set +H; [BOTCOMMAND] erlich | task | no trailer here",
        "a discussion of the observable plane: design and its tradeoffs",
        # a marker-shaped tail that is NOT a minted trailer: recorded as nothing
        "do the thing\n⟦plane:not-a-real-id⟧",
    ):
        r = _run(_hookjson(prompt, ensure_ascii=False), _env(root))
        assert r.returncode == 0
        assert r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_a_marker_shaped_but_invalid_trailer_is_disclosed_never_recorded(tmp_path):
    root = _root(tmp_path)
    r = _run(_hookjson("do the thing\n⟦plane:msg_short⟧", ensure_ascii=False), _env(root))
    assert r.returncode == 0 and r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()
    assert "did not parse" in r.stderr or "not recorded" in r.stderr


def test_a_trailing_marker_wins_even_if_the_body_quotes_one(tmp_path):
    """The end anchor: a body that mentions a marker mid-text does not confuse
    the real trailer at the tail."""
    body = "explain ⟦plane:msg_00000000000000000000000000000000⟧ to a new hire"
    sha, safe, nbytes = _wire_proof(body)
    root = _root(tmp_path)
    r = _run(_hookjson(_arrival(safe), ensure_ascii=False), _env(root))
    assert r.returncode == 0 and r.stdout == ""
    row = _received_row(root)[0]
    assert row["msg_id"] == MSGID          # the TAIL trailer, not the quoted one
    d = json.loads(row["detail"])
    assert d["received_sha256"] == sha     # hashed the wire form, quoted marker and all


# --- dormancy / arming ------------------------------------------------------

def test_disabled_exemption_silences_it(tmp_path):
    _, safe, _ = _wire_proof("set +H; " + BODY)
    root = _root(tmp_path)
    r = _run(_hookjson(_arrival(safe), ensure_ascii=False),
             _env(root, PLANE_EMIT_DISABLED="1"))
    assert r.returncode == 0 and r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_records_with_no_plane_flag_at_all(tmp_path):
    """F18 R1 always-on: no PLANE_EMIT_* flag -> still recorded."""
    _, safe, _ = _wire_proof("set +H; " + BODY)
    root = _root(tmp_path)
    r = _run(_hookjson(_arrival(safe), ensure_ascii=False), _env(root))
    assert r.returncode == 0 and r.stdout == ""
    assert len(_received_row(root)) == 1


def test_missing_identity_does_not_record(tmp_path):
    _, safe, _ = _wire_proof("set +H; " + BODY)
    root = _root(tmp_path)
    env = _env(root)
    del env["BOT_ID"]
    r = _run(_hookjson(_arrival(safe), ensure_ascii=False), env)
    assert r.returncode == 0 and r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_broken_stdin_is_silent(tmp_path):
    root = _root(tmp_path)
    for garbage in ("", "not json", '{"prompt":'):
        r = _run(garbage, _env(root))
        assert r.returncode == 0 and r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()
