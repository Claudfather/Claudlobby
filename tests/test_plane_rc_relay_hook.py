"""RC-relay outbound door (#1412) — a Stop hook recording the final text of
a Telegram-initiated turn the reply-tool hook cannot see.

Fixtures are SHAPE-verbatim from a live capture (a lumbergh transcript,
2026-09-02; ids faked per the PII rule): the plugin's channel prompt entry
carries `isMeta: true` and a structured `origin {kind: channel, server:
plugin:telegram:telegram}`; the final answer is an assistant entry with
`stop_reason: end_turn`; an API-error final has `isApiErrorMessage`/
`error` and `stop_reason: stop_sequence`. Laws: record only a genuine
final answer (end_turn, text, not an API error, no reply-tool send in the
turn); carrier state `unknown` (honest); dedupe on the entry uuid; stdout
empty; always on (only PLANE_EMIT_DISABLED=1 silences it); exit 0 every
path.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "lib" / "plane-rc-relay-out.sh"
CLI = Path(sys.executable).parent / "claudlobby"
CHAT = "-1001234567890"


def _root(tmp_path):
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    (tmp_path / "bot" / "data").mkdir(parents=True)
    return root


def _env(tmp_path, root, *, armed=True, disabled=False):
    # `armed` keeps the legacy PLANE_EMIT_ENABLED=1 in the environment of most
    # pins — it is IGNORED now (F18 closure R1); `disabled` is the one switch.
    env = {"CLAUDLOBBY_ROOT": str(root), "HOME": str(tmp_path),
           "PLANE_EMIT_CLI": str(CLI), "PLANE_SOCKET": str(tmp_path / "no.sock"),
           "FLEET_NAME": "f", "BOT_ID": "erlich", "BOT_DIR": str(tmp_path / "bot"),
           "PATH": "/usr/bin:/bin"}
    if armed:
        env["PLANE_EMIT_ENABLED"] = "1"
    if disabled:
        env["PLANE_EMIT_DISABLED"] = "1"
    return env


def _channel_user(uuid="u1"):
    return {"type": "user", "isMeta": True, "uuid": uuid, "sessionId": "s1",
            "parentUuid": None, "timestamp": "2026-09-02T12:00:00.000Z",
            "origin": {"kind": "channel", "server": "plugin:telegram:telegram"},
            "message": {"role": "user", "content":
                        f'<channel source="plugin:telegram:telegram" chat_id="{CHAT}"'
                        ' message_id="42" user="operator" user_id="8888888"'
                        ' ts="2026-09-02T12:00:00Z">\nchecking in\n</channel>'}}


def _assistant(text, *, stop="end_turn", uuid="a1", tool=None, api_error=False):
    content = []
    if tool:
        content.append({"type": "tool_use", "id": "t1", "name": tool, "input": {}})
    content.append({"type": "text", "text": text})
    e = {"type": "assistant", "uuid": uuid, "sessionId": "s1", "parentUuid": "u1",
         "timestamp": "2026-09-02T12:00:05.000Z",
         "message": {"role": "assistant", "stop_reason": stop, "content": content}}
    if api_error:
        e.update({"isApiErrorMessage": True, "error": "rate_limit",
                  "apiErrorStatus": 429})
    return e


def _transcript(tmp_path, entries):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return p


def _run(tmp_path, root, entries, *, armed=True, disabled=False, payload=None):
    tp = _transcript(tmp_path, entries)
    stdin = json.dumps(payload if payload is not None else
                       {"session_id": "s1", "transcript_path": str(tp),
                        "hook_event_name": "Stop", "stop_hook_active": False})
    return subprocess.run(["bash", str(HOOK)], input=stdin, capture_output=True,
                          text=True, env=_env(tmp_path, root, armed=armed, disabled=disabled),
                          timeout=120)


def _rows(root, sql):
    db = root / "state" / "plane" / "plane.db"
    if not db.is_file():
        return []
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_genuine_rc_relayed_final_answer_is_recorded_honestly(tmp_path):
    root = _root(tmp_path)
    r = _run(tmp_path, root, [_channel_user(), _assistant("All quiet, migration on track.")])
    assert r.returncode == 0 and r.stdout == ""
    comms = _rows(root, "SELECT sender_uid, recipient_raw, body FROM communications")
    assert len(comms) == 1
    assert comms[0]["recipient_raw"] == CHAT
    assert comms[0]["body"] == "All quiet, migration on track."
    # destination rides in `detail` (spec: classified sensitive, never a
    # column) — the chat is pinned via the communication's recipient_raw
    tx = _rows(root, "SELECT carrier, event FROM events WHERE kind='transmission'")
    assert len(tx) == 1
    assert tx[0]["carrier"] == "telegram-bridge"
    assert tx[0]["event"] == "unknown"          # delivery unobserved — never fabricated


def test_reply_tool_turn_is_the_other_hooks_and_not_double_recorded(tmp_path):
    root = _root(tmp_path)
    r = _run(tmp_path, root, [_channel_user(),
                              _assistant("sent", tool="mcp__plugin_telegram_telegram__reply", uuid="a0"),
                              _assistant("Done.", uuid="a1")])
    assert r.returncode == 0 and r.stdout == ""
    assert _rows(root, "SELECT 1 FROM communications") == []


def test_api_error_final_is_never_recorded_as_the_bots_reply(tmp_path):
    """The live capture's trap: 26 of 685 channel turns ended in Claude's
    own quota/rate error rendered as an assistant entry. Recording it would
    put 'You've hit your weekly limit' in the operator's conversation as
    the bot speaking."""
    root = _root(tmp_path)
    r = _run(tmp_path, root, [_channel_user(),
                              _assistant("You've hit your weekly limit · resets Sep 9",
                                         stop="stop_sequence", api_error=True)])
    assert r.returncode == 0 and r.stdout == ""
    assert _rows(root, "SELECT 1 FROM communications") == []


def test_interstitial_text_before_a_tool_call_is_not_a_final_answer(tmp_path):
    root = _root(tmp_path)
    r = _run(tmp_path, root, [_channel_user(),
                              _assistant("Reading the screenshot.", stop="tool_use",
                                         tool="Bash")])
    assert r.returncode == 0 and r.stdout == ""
    assert _rows(root, "SELECT 1 FROM communications") == []


def test_non_channel_turn_is_ignored(tmp_path):
    root = _root(tmp_path)
    plain = {"type": "user", "uuid": "u9", "sessionId": "s1",
             "message": {"role": "user", "content": "run the tests"}}
    r = _run(tmp_path, root, [plain, _assistant("Tests pass.")])
    assert r.returncode == 0 and r.stdout == ""
    assert _rows(root, "SELECT 1 FROM communications") == []


def test_records_without_any_flag_and_disabled_silences_it(tmp_path):
    """The always-on contract (F18 closure R1): no plane flag at all → the
    answer is recorded; PLANE_EMIT_DISABLED=1 → nothing, exit 0."""
    root = _root(tmp_path)
    r = _run(tmp_path, root, [_channel_user(), _assistant("hi")], armed=False)
    assert r.returncode == 0 and r.stdout == ""
    assert len(_rows(root, "SELECT 1 FROM communications")) == 1
    root2 = tmp_path / "root2"
    (root2 / "state" / "plane").mkdir(parents=True)
    (root2 / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    r = _run(tmp_path, root2, [_channel_user(), _assistant("hi")], disabled=True)
    assert r.returncode == 0 and r.stdout == ""
    assert not (root2 / "state" / "plane" / "plane.db").is_file()


def test_missing_transcript_is_silent_exit_zero(tmp_path):
    root = _root(tmp_path)
    r2 = _run(tmp_path, root, [_channel_user(), _assistant("hi")],
              payload={"session_id": "s1", "hook_event_name": "Stop"})   # no path
    assert r2.returncode == 0 and r2.stdout == ""
    assert not (root / "state" / "plane" / "plane.db").is_file()


def test_a_refired_stop_never_double_records(tmp_path):
    root = _root(tmp_path)
    ents = [_channel_user(), _assistant("All quiet.")]
    assert _run(tmp_path, root, ents).returncode == 0
    assert _run(tmp_path, root, ents).returncode == 0
    assert len(_rows(root, "SELECT 1 FROM communications")) == 1


def test_stop_hook_is_composed_fleet_wide():
    import yaml
    hooks = yaml.safe_load((REPO / "claudlobby" / "system.yaml").read_text())[
        "defaults"]["hooks"]
    assert any("plane-rc-relay-out.sh" in h["command"] for h in hooks["Stop"])
    assert (REPO / "system.yaml.example").read_text().split(
        "# --- verbatim copy of the package tier below ---\n", 1)[1] == (
        REPO / "claudlobby" / "system.yaml").read_text()


def test_big_transcript_is_read_bounded_and_fast(tmp_path):
    """Gauntlet F2 fold: the turn is at the END, so a bounded tail read
    finds it without parsing a 60 MB history (422 MB RSS per turn end on
    the Pi). A 6 MB transcript with the channel turn last: recorded, and
    the hook finishes quickly."""
    import time
    root = _root(tmp_path)
    filler = [{"type": "assistant", "uuid": f"f{i}", "sessionId": "s0",
               "message": {"role": "assistant", "stop_reason": "end_turn",
                           "content": [{"type": "text", "text": "x" * 2000}]}}
              for i in range(3000)]                        # ~6 MB
    t0 = time.monotonic()
    r = _run(tmp_path, root, filler + [_channel_user(), _assistant("Final answer.")])
    assert r.returncode == 0 and r.stdout == ""
    assert time.monotonic() - t0 < 10
    assert len(_rows(root, "SELECT 1 FROM communications")) == 1


def test_bot_dir_unset_refuses_and_writes_nothing_into_cwd(tmp_path):
    """Gauntlet F3 fold (#874 class): with BOT_DIR unset the hook must
    refuse — never default the marker dir to cwd, which is a bot's project
    checkout."""
    root = _root(tmp_path)
    tp = _transcript(tmp_path, [_channel_user(), _assistant("hi")])
    env = _env(tmp_path, root); env.pop("BOT_DIR")
    cwd = tmp_path / "checkout"; cwd.mkdir()
    r = subprocess.run(["bash", str(HOOK)], input=json.dumps(
        {"session_id": "s1", "transcript_path": str(tp), "hook_event_name": "Stop"}),
        capture_output=True, text=True, env=env, cwd=cwd, timeout=120)
    assert r.returncode == 0 and r.stdout == ""
    assert "BOT_DIR unset" in r.stderr
    assert not (cwd / "data").exists()
    assert _rows(root, "SELECT 1 FROM communications") == []


def test_unwritable_marker_dir_skips_rather_than_double_records(tmp_path):
    """Gauntlet F4 fold: no dedupe possible → no record (disclosed), so a
    re-fired Stop can never double-record."""
    import os, stat
    if os.geteuid() == 0:
        return                                          # root ignores modes
    root = _root(tmp_path)
    data = tmp_path / "bot" / "data"
    data.chmod(0o500)
    try:
        ents = [_channel_user(), _assistant("hi")]
        r1 = _run(tmp_path, root, ents); r2 = _run(tmp_path, root, ents)
        assert r1.returncode == 0 and r2.returncode == 0
        assert "marker unwritable" in r1.stderr
        assert _rows(root, "SELECT 1 FROM communications") == []
    finally:
        data.chmod(0o700)


def test_python_errors_are_not_swallowed():
    """Gauntlet F5 fold: the python invocation must not hide tracebacks
    behind 2>/dev/null — a shape error must be distinguishable from
    'not this door's turn'."""
    body = HOOK.read_text()
    line = next(l for l in body.splitlines() if 'python3 -S -E -c "$PYPROG"' in l)
    assert "2>/dev/null" not in line
