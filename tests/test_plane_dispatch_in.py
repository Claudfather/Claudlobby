"""chunk P (#1501): the receiver hook lib/plane-dispatch-in.sh, end to end
through the real cold emit root (the test_plane_telegram_hooks.py shape).

The load-bearing laws, all pinned here:
  * STDOUT IS EMPTY on every path (UserPromptSubmit stdout feeds the model).
  * THE STRIP BOUNDARY IS EXACT: the receiver's received_sha256/received_bytes
    equal the SENDER's body_sha256/body_bytes (contracts.cap_body over the
    message proper) — the crux. Proven with a REAL round-trip: the sender's
    hash, the arrived prompt built the way bot_tmux_send builds it, the hook's
    strip, asserted equal.
  * an un-tokened prompt records NOTHING and exits at the zero-fork prefilter.
  * PLANE_EMIT_DISABLED=1 silences; a missing FLEET_NAME/BOT_ID does not record.

THE FIXTURE IS GROUNDED IN THE LIVE ARRIVED-PROMPT SHAPE (the #1404/#1411 rule),
captured from a jian-yang dispatch on the Mini: `set +H; [BOTCOMMAND] <mgr> |
task | …`, i.e. lib/dispatch.sh's `set +H; ` prefix + the [BOTCOMMAND] body,
with the routing trailer bot_tmux_send appends on its own last line. Only the
identifiers are faked (public repo). It is NOT hand-written from the contract.
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

# a real minted-shape id (msg_ + 32 hex), value faked
MSGID = "msg_1f3c9a7b2e5d4068a1b2c3d4e5f60718"
# the message PROPER — what the sender hashed (no `set +H; `, no trailer).
# A [BOTCOMMAND] envelope, single-line/single-spaced (the dominant, and
# sanitize-STABLE, dispatch shape).
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
        "FLEET_NAME": "test-fleet",
        "BOT_ID": "erlich",
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


def _arrived_prompt(body: str = BODY, msgid: str = MSGID,
                    *, prefix: str = "set +H; ") -> str:
    """The prompt the RECEIVER'S session sees — the live shape: the (optional)
    `set +H; ` dispatch.sh prefix + the body, then the trailer on its OWN final
    line (a single '\\n' separator), the way bot_tmux_send lays it out."""
    return f"{prefix}{body}\n⟦plane:{msgid}⟧"


def _wire(prompt: str, *, ensure_ascii: bool) -> str:
    """The hook JSON on the wire. ensure_ascii toggles the two encodings the
    prefilter must survive: the bracket as `\\u27e6` (a JSON encoder that
    escapes non-ASCII — Python's default) vs the literal UTF-8 byte."""
    return json.dumps({"prompt": prompt}, ensure_ascii=ensure_ascii)


def _received_row(root: Path):
    # destination rides `detail` (a transmission has no destination column)
    return _rows(root, "SELECT event, carrier, msg_id, detail"
                       " FROM events WHERE kind='transmission'")


# --- the crux: the strip boundary makes received == body --------------------

@pytest.mark.parametrize("ensure_ascii", [True, False], ids=["escaped", "literal"])
def test_received_sha_and_bytes_equal_the_senders_body_hash(tmp_path, ensure_ascii):
    from claudlobby.plane import contracts as c
    sender = c.cap_body(BODY)          # what the sender records at ingest
    root = _root(tmp_path)
    r = _run(_wire(_arrived_prompt(), ensure_ascii=ensure_ascii), _env(root))
    assert r.returncode == 0
    assert r.stdout == ""              # THE law: stdout feeds the model
    rows = _received_row(root)
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "received" and row["carrier"] == "tmux"
    assert row["msg_id"] == MSGID
    d = json.loads(row["detail"])
    assert d["destination"] == "erlich"
    # THE crux: the receiver's proof equals the sender's body hash byte-for-byte
    assert d["received_sha256"] == sender.body_sha256
    assert d["received_bytes"] == sender.body_bytes


def test_a_slash_command_briefing_carries_no_prefix_and_still_round_trips(tmp_path):
    """report-back and a `/briefing` send go through bot_tmux_send directly (no
    `set +H; `), so the hook must round-trip a trailer-only prompt too."""
    from claudlobby.plane import contracts as c
    body = "/briefing morning"
    sender = c.cap_body(body)
    root = _root(tmp_path)
    r = _run(_wire(_arrived_prompt(body, prefix=""), ensure_ascii=False), _env(root))
    assert r.returncode == 0 and r.stdout == ""
    d = json.loads(_received_row(root)[0]["detail"])
    assert d["received_sha256"] == sender.body_sha256
    assert d["received_bytes"] == sender.body_bytes


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
        r = _run(_wire(prompt, ensure_ascii=False), _env(root))
        assert r.returncode == 0
        assert r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_a_marker_shaped_but_invalid_trailer_is_disclosed_never_recorded(tmp_path):
    root = _root(tmp_path)
    r = _run(_wire("do the thing\n⟦plane:msg_short⟧", ensure_ascii=False), _env(root))
    assert r.returncode == 0 and r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()
    assert "did not parse" in r.stderr or "not recorded" in r.stderr


def test_a_trailing_marker_wins_even_if_the_body_quotes_one(tmp_path):
    """The end anchor: a body that mentions a marker mid-text does not confuse
    the real trailer at the tail."""
    from claudlobby.plane import contracts as c
    body = "explain ⟦plane:msg_00000000000000000000000000000000⟧ to a new hire"
    sender = c.cap_body(body)
    root = _root(tmp_path)
    r = _run(_wire(_arrived_prompt(body), ensure_ascii=False), _env(root))
    assert r.returncode == 0 and r.stdout == ""
    row = _received_row(root)[0]
    assert row["msg_id"] == MSGID          # the TAIL trailer, not the quoted one
    d = json.loads(row["detail"])
    assert d["received_sha256"] == sender.body_sha256


# --- dormancy / arming ------------------------------------------------------

def test_disabled_exemption_silences_it(tmp_path):
    root = _root(tmp_path)
    r = _run(_wire(_arrived_prompt(), ensure_ascii=False),
             _env(root, PLANE_EMIT_DISABLED="1"))
    assert r.returncode == 0 and r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_records_with_no_plane_flag_at_all(tmp_path):
    """F18 R1 always-on: no PLANE_EMIT_* flag -> still recorded."""
    root = _root(tmp_path)
    r = _run(_wire(_arrived_prompt(), ensure_ascii=False), _env(root))
    assert r.returncode == 0 and r.stdout == ""
    assert len(_received_row(root)) == 1


def test_missing_identity_does_not_record(tmp_path):
    root = _root(tmp_path)
    env = _env(root)
    del env["BOT_ID"]
    r = _run(_wire(_arrived_prompt(), ensure_ascii=False), env)
    assert r.returncode == 0 and r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_broken_stdin_is_silent(tmp_path):
    root = _root(tmp_path)
    for garbage in ("", "not json", '{"prompt":'):
        r = _run(garbage, _env(root))
        assert r.returncode == 0 and r.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").exists()
