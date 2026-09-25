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


def _env(root: Path, *, scratch_plane_env, **extra) -> dict:
    env = {
        "PATH": f"{REPO}/.venv/bin:" + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        **scratch_plane_env(root),
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
def test_received_equals_the_wire_proof_for_every_shape(tmp_path, shape, ensure_ascii, *, scratch_plane_env):
    body = _SHAPES[shape]
    payload = "set +H; " + body                # what dispatch.sh sends for prose
    sha, safe, nbytes = _wire_proof(payload)
    root = _root(tmp_path)
    r = _run(_hookjson(_arrival(safe), ensure_ascii=ensure_ascii), _env(root, scratch_plane_env=scratch_plane_env))
    assert r.returncode == 0
    assert r.stdout == ""                      # THE law: stdout feeds the model
    rows = _received_row(root)
    assert len(rows) == 1
    d = json.loads(rows[0]["detail"])
    assert d["destination"] == BOT
    # THE crux: the receiver's proof equals the sender's WIRE proof byte-for-byte
    assert d["received_sha256"] == sha
    assert d["received_bytes"] == nbytes


def test_a_slash_command_briefing_carries_no_prefix_and_still_round_trips(tmp_path, *, scratch_plane_env):
    """report-back and a `/briefing` send go through bot_tmux_send directly (no
    `set +H; `), so the hook must round-trip a trailer-only wire form too."""
    sha, safe, nbytes = _wire_proof("/briefing morning")
    root = _root(tmp_path)
    r = _run(_hookjson(_arrival(safe), ensure_ascii=False), _env(root, scratch_plane_env=scratch_plane_env))
    assert r.returncode == 0 and r.stdout == ""
    d = json.loads(_received_row(root)[0]["detail"])
    assert d["received_sha256"] == sha
    assert d["received_bytes"] == nbytes


def test_a_whole_multiline_dispatch_reads_DELIVERED_through_the_join(tmp_path, *, scratch_plane_env):
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
    r = _run(_hookjson(_arrival(safe), ensure_ascii=False), _env(root, scratch_plane_env=scratch_plane_env))
    assert r.returncode == 0 and r.stdout == ""
    db = root / "state" / "plane" / "plane.db"
    conn = connect(str(db)); migrate(conn); conn.row_factory = sqlite3.Row
    row = dict(conn.execute(q.DELIVERY_STATUS_SQL.format(ph="?"), [MSGID]).fetchone())
    assert row["delivery"] == "delivered"


def test_a_genuinely_shortened_arrival_reads_TRUNCATED_through_the_join(tmp_path, *, scratch_plane_env):
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
    r = _run(_hookjson(_arrival(safe[:-12]), ensure_ascii=False), _env(root, scratch_plane_env=scratch_plane_env))
    assert r.returncode == 0 and r.stdout == ""
    db = root / "state" / "plane" / "plane.db"
    conn = connect(str(db)); migrate(conn); conn.row_factory = sqlite3.Row
    row = dict(conn.execute(q.DELIVERY_STATUS_SQL.format(ph="?"), [MSGID]).fetchone())
    assert row["delivery"] == "truncated"


# --- the prefilter / recogniser ---------------------------------------------

def test_an_untokened_prompt_records_nothing_and_stdout_is_empty(tmp_path, *, scratch_plane_env):
    root = _root(tmp_path)
    for prompt in (
        "fix the flaky test in tests/test_auth.py",
        "set +H; [BOTCOMMAND] erlich | task | no trailer here",
        "a discussion of the observable plane: design and its tradeoffs",
        # a marker-shaped tail that is NOT a minted trailer: recorded as nothing
        "do the thing\n⟦plane:not-a-real-id⟧",
    ):
        r = _run(_hookjson(prompt, ensure_ascii=False), _env(root, scratch_plane_env=scratch_plane_env))
        assert r.returncode == 0
        assert r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_a_marker_shaped_but_invalid_trailer_is_disclosed_never_recorded(tmp_path, *, scratch_plane_env):
    root = _root(tmp_path)
    r = _run(_hookjson("do the thing\n⟦plane:msg_short⟧", ensure_ascii=False), _env(root, scratch_plane_env=scratch_plane_env))
    assert r.returncode == 0 and r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()
    assert "did not parse" in r.stderr or "not recorded" in r.stderr


def test_a_trailing_marker_wins_even_if_the_body_quotes_one(tmp_path, *, scratch_plane_env):
    """The end anchor: a body that mentions a marker mid-text does not confuse
    the real trailer at the tail."""
    body = "explain ⟦plane:msg_00000000000000000000000000000000⟧ to a new hire"
    sha, safe, nbytes = _wire_proof(body)
    root = _root(tmp_path)
    r = _run(_hookjson(_arrival(safe), ensure_ascii=False), _env(root, scratch_plane_env=scratch_plane_env))
    assert r.returncode == 0 and r.stdout == ""
    row = _received_row(root)[0]
    assert row["msg_id"] == MSGID          # the TAIL trailer, not the quoted one
    d = json.loads(row["detail"])
    assert d["received_sha256"] == sha     # hashed the wire form, quoted marker and all


# --- dormancy / arming ------------------------------------------------------

def test_disabled_exemption_silences_it(tmp_path, *, scratch_plane_env):
    _, safe, _ = _wire_proof("set +H; " + BODY)
    root = _root(tmp_path)
    r = _run(_hookjson(_arrival(safe), ensure_ascii=False),
             _env(root, PLANE_EMIT_DISABLED="1", scratch_plane_env=scratch_plane_env))
    assert r.returncode == 0 and r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_records_with_no_plane_flag_at_all(tmp_path, *, scratch_plane_env):
    """F18 R1 always-on: no PLANE_EMIT_* flag -> still recorded."""
    _, safe, _ = _wire_proof("set +H; " + BODY)
    root = _root(tmp_path)
    r = _run(_hookjson(_arrival(safe), ensure_ascii=False), _env(root, PLANE_EMIT_DISABLED=None, scratch_plane_env=scratch_plane_env))
    assert r.returncode == 0 and r.stdout == ""
    assert len(_received_row(root)) == 1


def test_missing_identity_does_not_record(tmp_path, *, scratch_plane_env):
    _, safe, _ = _wire_proof("set +H; " + BODY)
    root = _root(tmp_path)
    env = _env(root, scratch_plane_env=scratch_plane_env)
    del env["BOT_ID"]
    r = _run(_hookjson(_arrival(safe), ensure_ascii=False), env)
    assert r.returncode == 0 and r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_broken_stdin_is_silent(tmp_path, *, scratch_plane_env):
    root = _root(tmp_path)
    for garbage in ("", "not json", '{"prompt":'):
        r = _run(garbage, _env(root, scratch_plane_env=scratch_plane_env))
        assert r.returncode == 0 and r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_a_held_box_gets_one_more_enter_and_stays_loud_if_still_held(tmp_path, *, scratch_plane_env):
    """#1099/#1236, the failure that happened: a tracked dispatch sat in an idle
    recipient's box, its Enter turned into a newline, and the pane-reading
    verify called it a clean send. The dispatch door now asks the RECEIVER:
    pane_await_receipt waits for this hook's `received` row and gives a missing
    one ONE more Enter. Only the TUI is a stub -- the Enter it is handed runs
    the REAL hook on the held prompt, as UserPromptSubmit would -- and the REAL
    plane-lookup.py reads what that hook wrote."""
    root = _root(tmp_path)
    env = _env(root, FLEET_EVENT_EMIT_TIMEOUT_S="60", PANE_RECEIPT_WAIT_S="0.3", scratch_plane_env=scratch_plane_env)
    _, safe, _ = _wire_proof("set +H; " + BODY)
    # An earlier dispatch was received, so this recipient's hook is armed.
    assert _run(_hookjson(_arrival(safe), ensure_ascii=False), env).returncode == 0
    prog = ('. "$LIB/lib-common.sh"; set +e; '
            'bot_tmux() { case "$*" in *capture-pane*) return 0;; esac;'  # a read, not a key
            ' echo "$*"; [ "$TUI" = submits ] || return 0;'
            ' printf %s "$PROMPT" | bash "$LIB/plane-dispatch-in.sh"; }; '
            'pane_await_receipt sock "${DEST:-$BOT_ID}" "$MSG"')

    def gate(msgid, tui, dest=BOT):   # -> (rc, the keys the stub TUI was sent, stderr)
        r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=120,
                           env={**env, "LIB": str(LIB), "TUI": tui, "MSG": msgid, "DEST": dest,
                                "PROMPT": _hookjson(_arrival(safe, msgid), ensure_ascii=False)})
        return r.returncode, r.stdout.splitlines(), r.stderr

    held, stuck = "msg_" + "a" * 32, "msg_" + "b" * 32
    assert gate(MSGID, "submits")[:2] == (0, [])        # received already: no extra Enter
    assert gate(held, "submits")[:2] == (0, [f"sock send-keys -t {BOT} Enter"])
    # A prompt elsewhere that merely quotes the trailer files a receipt under
    # ANOTHER bot (fold F3): it must not read as this one's.
    assert _run(_hookjson(_arrival(safe, stuck), ensure_ascii=False), {**env, "BOT_ID": "gilfoyle"}).returncode == 0
    rc, keys, err = gate(stuck, "holds")
    assert (rc, len(keys)) == (1, 1) and f"no receipt from {BOT}" in err
    assert [tuple(r) for r in _rows(root, (
        "SELECT event, json_extract(detail, '$.data.msg_id') FROM events"
        " WHERE kind = 'system' AND event IN ('send_retry', 'send_miss') ORDER BY ingest_seq"))] \
        == [("send_retry", held), ("send_retry", stuck), ("send_miss", stuck)]
    # A recipient that never recorded a receipt has no hook armed: no verdict, nothing pressed.
    assert gate(stuck, "holds", dest="dinesh")[:2] == (0, [])


def test_a_queued_delivery_is_not_a_miss(tmp_path, *, scratch_plane_env):
    """#1099 review (vera, 8 of the 16 would-be misses): a recipient that starts a
    turn after the door's idle probe QUEUES the prompt, and its receipt lands only
    when that turn ends. That is not a held box, so a missing receipt from a busy
    recipient gets no Enter and no send_miss -- checked before the Enter, and again
    before the verdict, since the turn may start while the gate waits."""
    root = _root(tmp_path)
    env = _env(root, FLEET_EVENT_EMIT_TIMEOUT_S="60", PANE_RECEIPT_WAIT_S="0.3", scratch_plane_env=scratch_plane_env)
    _, safe, _ = _wire_proof("set +H; " + BODY)
    assert _run(_hookjson(_arrival(safe), ensure_ascii=False), env).returncode == 0  # armed
    flag = tmp_path / "turn-started"
    prog = ('. "$LIB/lib-common.sh"; set +e; '
            'bot_tmux() { case "$*" in'
            ' *capture-pane*) [ -f "$FLAG" ] && echo "esc to interrupt"; return 0;;'
            ' *send-keys*) [ "$TURN" = after-enter ] && : > "$FLAG";; esac; echo "$*"; }; '
            'pane_await_receipt sock "$BOT_ID" "$MSG"')

    def gate(msgid, turn):   # -> (rc, the keys the stub TUI was sent)
        if turn == "running":
            flag.touch()
        r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=120,
                           env={**env, "LIB": str(LIB), "MSG": msgid, "TURN": turn, "FLAG": str(flag)})
        flag.unlink(missing_ok=True)
        return r.returncode, r.stdout.splitlines()

    queued, late = "msg_" + "c" * 32, "msg_" + "e" * 32
    assert gate(queued, "running") == (0, [])                               # nothing pressed
    assert gate(late, "after-enter") == (0, [f"sock send-keys -t {BOT} Enter"])
    assert [tuple(r) for r in _rows(root, (
        "SELECT event, json_extract(detail, '$.data.msg_id') FROM events"
        " WHERE kind = 'system' AND event IN ('send_retry', 'send_miss') ORDER BY ingest_seq"))] \
        == [("send_retry", late)]                                            # no verdict for either


def test_a_zero_wait_in_any_spelling_turns_the_gate_off(tmp_path, *, scratch_plane_env):
    """PANE_RECEIPT_WAIT_S=0 is the off switch; `0.0` must not read as on (#1099 review)."""
    root = _root(tmp_path)
    _, safe, _ = _wire_proof("set +H; " + BODY)
    assert _run(_hookjson(_arrival(safe), ensure_ascii=False), _env(root, scratch_plane_env=scratch_plane_env)).returncode == 0  # armed
    for zero in ("0", "0.0"):
        env = _env(root, FLEET_EVENT_EMIT_TIMEOUT_S="60", PANE_RECEIPT_WAIT_S=zero, scratch_plane_env=scratch_plane_env)
        r = subprocess.run(["bash", "-c", '. "$LIB/lib-common.sh"; set +e; bot_tmux() { echo "$*"; }; '
                            'pane_await_receipt sock "$BOT_ID" msg_' + "f" * 32],
                           capture_output=True, text=True, timeout=60, env={**env, "LIB": str(LIB)})
        assert (r.returncode, r.stdout) == (0, ""), zero                   # nothing pressed
    assert _rows(root, "SELECT event FROM events WHERE kind = 'system'"
                       " AND event IN ('send_retry', 'send_miss')") == []


def _pasted(text: str, at: int) -> str:
    """Claude Code's wrapper for a pasted run -- SHAPE from live transcripts
    (#1099; the id faked): the first `at` characters arrived as one paste,
    the rest was typed after it. The boundary is a tmux chunk boundary, so it
    can fall inside the trailer itself."""
    return f'\n\n<pasted_content id="0f3a">\n{text[:at]}\n</pasted_content id="0f3a">\n\n{text[at:]}'


@pytest.mark.parametrize("where", ["splits-the-trailer", "before-the-trailer"])
def test_a_pasted_arrival_is_received_as_the_wire_form(tmp_path, where, *, scratch_plane_env):
    """#1099: the TUI wraps a pasted run in <pasted_content> tags. Where the
    boundary split the trailer, the hook recorded NO receipt for a prompt that
    WAS submitted (9 tracked prompts, 2026-09-20..24); where it fell before the
    trailer, the tags were hashed in and a whole delivery read ALTERED (50).
    The wrapper is the TUI's, not the sender's: without it, the arrival is
    the wire form again."""
    sha, safe, nbytes = _wire_proof("set +H; " + BODY)
    arrival = _arrival(safe)
    at = len(arrival) - 10 if where == "splits-the-trailer" else len(safe)
    root = _root(tmp_path)
    r = _run(_hookjson(_pasted(arrival, at), ensure_ascii=False), _env(root, scratch_plane_env=scratch_plane_env))
    assert r.returncode == 0 and r.stdout == ""
    rows = _received_row(root)
    assert len(rows) == 1 and rows[0]["msg_id"] == MSGID
    detail = json.loads(rows[0]["detail"])
    assert (detail["received_sha256"], detail["received_bytes"]) == (sha, nbytes)
