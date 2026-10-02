"""#1693 — the plane socket deadline follows the caller's class.

One 1.0 s total deadline served every caller of the plane socket, and on the
Pi's SD card an ordinary commit can take longer (2026-09-28 trace: 14 of the
29 clients that missed were waiting through a plain commit). Every miss arms
the host-wide wedge marker. A caller now names who waits on its emission —
``PLANE_EMIT_CLASS`` is ``hook``, ``background`` or ``door`` — and that class's
knob sets its deadline. Every knob defaults to today's 1.0 s.

The shim's own suite (tests/test_plane_emit.sh) pins the knobs and the arm
record. This file pins the WIRING, which the shim cannot see: a class is a
plain, un-exported shell variable in its caller, so it reaches the shim only
because the helpers forward it. Each end-to-end case runs a REAL caller
against a daemon that answers in 1.5 s, and each has a control that the wrong
class would pass.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "claudlobby/_runtime_scripts"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not installed"
)


class _SlowDaemon:
    """A unix-socket listener that answers each request `delay` seconds after
    reading it, `ok` for every event, and keeps what it read."""

    def __init__(self, delay: float):
        self.dir = Path(tempfile.mkdtemp(prefix="pec", dir="/tmp"))  # sun_path limit
        self.path = self.dir / "s"
        self.delay = delay
        self.seen: list[str] = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(self.path))
        self._srv.listen(16)
        self._srv.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            if not buf:
                return  # a liveness probe
            self.seen.append(buf.decode())
            time.sleep(self.delay)
            events = json.loads(buf)["events"]
            reply = {
                "ok": True,
                "results": [
                    {"event_id": e["event_id"], "status": "committed"} for e in events
                ],
            }
            conn.sendall(json.dumps(reply).encode() + b"\n")
        except (OSError, ValueError):
            pass  # the client gave up first: that is the miss under test
        finally:
            conn.close()

    def stop(self) -> None:
        self._stop.set()
        self._srv.close()
        self._thread.join(timeout=3)
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture()
def world(tmp_path: Path):
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    bot = tmp_path / "bots" / "b"
    (bot / "data").mkdir(parents=True)
    cold = tmp_path / "cold.sh"
    cold.write_text('#!/bin/bash\necho "$@" >> "$COLD_LOG"\nexit 0\n')
    cold.chmod(0o755)
    daemon = _SlowDaemon(delay=1.5)
    try:
        yield {
            "root": root,
            "bot": bot,
            "cold": cold,
            "daemon": daemon,
            "cold_log": tmp_path / "cold.log",
        }
    finally:
        daemon.stop()


def _env(w: dict, **extra) -> dict:
    """Built from nothing, not from os.environ: a bot session exports its own
    identity, and a canary bot a deadline knob, and either would leak in."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "USER": os.environ.get("USER", "nobody"),
        "LANG": "C.UTF-8",
        "CLAUDLOBBY_ROOT": str(w["root"]),
        "PLANE_SOCKET": str(w["daemon"].path),
        "PLANE_EMIT_CLI": str(w["cold"]),
        "COLD_LOG": str(w["cold_log"]),
        "FLEET_NAME": "f",
        "BOT_ID": "b",
        "BOT_DIR": str(w["bot"]),
    }
    env.update(extra)
    return env


def _marker(w: dict) -> Path:
    return w["root"] / "state" / "plane" / ".socket-wedged"


def _answered(w: dict, needle: str) -> None:
    """The daemon's answer was waited for: it saw the event, nothing fell back
    to the cold rung, and nothing armed the host-wide marker."""
    assert any(needle in r for r in w["daemon"].seen), "the daemon never saw the event"
    assert not w["cold_log"].exists(), (
        "the cold rung ran: the class deadline was not applied"
    )
    assert not _marker(w).exists(), (
        "the marker armed: the class deadline was not applied"
    )


def _missed(w: dict) -> None:
    assert _marker(w).exists(), "no miss: the knob reached a caller of another class"


def _run(argv: list, stdin: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, input=stdin, capture_output=True, text=True, env=env, timeout=60
    )


# --- the hot hook path: bot-vitals, twice per tool call ---------------------

VITALS_PAYLOAD = json.dumps(
    {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s-1693"}
)


def test_bot_vitals_waits_as_a_hook(world):
    r = _run(
        ["bash", str(LIB / "bot-vitals.sh")],
        VITALS_PAYLOAD,
        _env(world, PLANE_SOCKET_DEADLINE_HOOK_S="4"),
    )
    assert r.returncode == 0, r.stderr
    _answered(world, "tool_call")


def test_bot_vitals_is_not_background(world):
    """The control. bot-vitals rides plane_emit_bounded, whose own default is
    background; were its hook class lost there, this knob would rescue it."""
    r = _run(
        ["bash", str(LIB / "bot-vitals.sh")],
        VITALS_PAYLOAD,
        _env(world, PLANE_SOCKET_DEADLINE_BACKGROUND_S="4"),
    )
    assert r.returncode == 0, r.stderr
    _missed(world)


# --- fire-and-forget: every emit_fleet_event outside a hook -----------------

FLEET_EVENT = (
    f'. "{LIB}/lib-common.sh"; emit_fleet_event probe_1693 test "{{}}" "" fleet'
)


def test_a_fleet_event_outside_a_hook_is_background(world):
    r = _run(
        ["bash", "-c", FLEET_EVENT],
        "",
        _env(world, PLANE_SOCKET_DEADLINE_BACKGROUND_S="4"),
    )
    assert r.returncode == 0, r.stderr
    _answered(world, "probe_1693")


def test_a_fleet_event_outside_a_hook_is_not_a_hook(world):
    r = _run(
        ["bash", "-c", FLEET_EVENT], "", _env(world, PLANE_SOCKET_DEADLINE_HOOK_S="4")
    )
    assert r.returncode == 0, r.stderr
    _missed(world)


# --- a door: the class is an un-exported variable, forwarded by the helper --

DOOR_BATCH = json.dumps(
    {
        "events": [
            {
                "event_type": "system",
                "emitter": "t",
                "fleet": "f",
                "payload": {"event": "door_1693"},
            }
        ]
    }
)


def test_plane_emit_events_forwards_an_unexported_class(world):
    script = (
        f'. "{LIB}/lib-common.sh"; PLANE_EMIT_CLASS=door; plane_emit_events t <<<"$B"'
    )
    r = _run(
        ["bash", "-c", script],
        "",
        _env(world, B=DOOR_BATCH, PLANE_SOCKET_DEADLINE_DOOR_S="4"),
    )
    assert r.returncode == 0, r.stderr
    _answered(world, "door_1693")


def test_an_unclassified_emit_keeps_the_default(world):
    script = f'. "{LIB}/lib-common.sh"; plane_emit_events t <<<"$B"'
    r = _run(
        ["bash", "-c", script],
        "",
        _env(world, B=DOOR_BATCH, PLANE_SOCKET_DEADLINE_DOOR_S="4"),
    )
    assert r.returncode == 0, r.stderr
    _missed(world)


# --- a hook that calls the shim directly --------------------------------------

CHANNEL = json.dumps(
    {
        "prompt": '<channel source="plugin:telegram:telegram" '
        'chat_id="-100999" message_id="77" user="op" '
        'user_id="70001" ts="2026-09-28T10:00:00Z">'
        "status please</channel>"
    }
)


def test_telegram_in_waits_as_a_hook(world):
    r = _run(
        ["bash", str(LIB / "plane-telegram-in.sh")],
        CHANNEL,
        _env(world, PLANE_SOCKET_DEADLINE_HOOK_S="4"),
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == "", "UserPromptSubmit stdout reaches the model"
    _answered(world, "status please")


def test_telegram_in_is_not_a_door(world):
    r = _run(
        ["bash", str(LIB / "plane-telegram-in.sh")],
        CHANNEL,
        _env(world, PLANE_SOCKET_DEADLINE_DOOR_S="4"),
    )
    assert r.returncode == 0, r.stderr
    _missed(world)


# --- the arm record names its cause -----------------------------------------


def _client_against(reply_fn, tmp_path: Path, timeout: str = "1.0") -> tuple:
    """Run the socket client alone against a listener that answers with
    `reply_fn(conn)`; return (rc, the arm log's rows)."""
    d = Path(tempfile.mkdtemp(prefix="pec", dir="/tmp"))
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(d / "s"))
    srv.listen(1)
    srv.settimeout(3)  # a client that never connects must not hang the thread

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return  # never connected: the deadline was spent first
        try:
            conn.recv(65536)
            reply_fn(conn)
        except OSError:
            pass
        finally:
            conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    arms = tmp_path / "arms"
    try:
        r = subprocess.run(
            ["python3", "-S", "-E", str(LIB / "plane-socket-client.py"), "--socket", str(d / "s"),
             "--finalize-to", str(tmp_path / "fin"), "--arm-log", str(arms), "--timeout", timeout],
            input='{"events": [{"event_type": "x"}]}', capture_output=True, text=True,
            timeout=30, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    finally:
        srv.close()
        t.join(timeout=5)
        shutil.rmtree(d, ignore_errors=True)
    rows = [l.split("\t") for l in arms.read_text().splitlines()] if arms.exists() else []
    return r.returncode, rows


def test_the_clients_own_deadline_check_is_a_timeout(tmp_path):
    """The client also checks its deadline itself between operations, and
    that check raised a bare OSError, which the arm record would name
    `OSError`. A socket op cannot reach it on demand -- each runs under the
    time left, so a late byte is a socket timeout instead -- so the deadline
    is spent before the first check: 1 us, less than creating the socket."""
    rc, rows = _client_against(lambda conn: None, tmp_path, timeout="0.000001")
    assert rc == 5
    assert [r[5] for r in rows] == ["timeout"], rows


def test_a_refusal_the_cold_rung_can_answer_is_named_by_its_code(tmp_path):
    def forbidden(conn):
        conn.sendall(b'{"ok": false, "code": "forbidden", "error": "no"}\n')

    rc, rows = _client_against(forbidden, tmp_path)
    assert rc == 5
    assert [r[5] for r in rows] == ["code:forbidden"], rows


def test_a_corrupt_arm_log_never_changes_the_exit(tmp_path):
    """The record is best-effort. A log it cannot parse -- a torn write, a
    foreign file -- must leave the exit at 5: a crash is exit 1, which the
    shim reads as neither a verdict nor a transport failure."""
    arms = tmp_path / "arms"
    arms.write_bytes(b"\xff\xfe not a row\n")
    r = subprocess.run(
        ["python3", "-S", "-E", str(LIB / "plane-socket-client.py"),
         "--socket", str(tmp_path / "absent"), "--finalize-to", str(tmp_path / "fin"),
         "--arm-log", str(arms)],
        input='{"events": [{"event_type": "x"}]}', capture_output=True, text=True,
        timeout=30, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    assert r.returncode == 5, r.stderr
    last = arms.read_bytes().splitlines()[-1].decode().split("\t")
    assert len(last) == 6 and last[5] == "unreachable", last
    # and the torn row ages out, or a rotation that cannot read it never runs
    assert b"not a row" not in arms.read_bytes()


def test_a_reply_the_log_cannot_encode_never_changes_the_exit(tmp_path):
    """Not only I/O can fail the append: a garbage or squatting daemon's code
    may be a lone surrogate, which valid JSON can carry and UTF-8 cannot
    encode. The record is lost, and the exit stays 5."""
    def surrogate(conn):
        conn.sendall(b'{"ok": false, "code": "\\ud800", "error": "x"}\n')

    rc, _rows = _client_against(surrogate, tmp_path)
    assert rc == 5


def test_a_verdict_is_not_an_arm(tmp_path):
    """A contract violation passes through as 2 and arms nothing."""
    def refuse(conn):
        conn.sendall(b'{"ok": false, "code": "contract_violation", "error": "no"}\n')

    rc, rows = _client_against(refuse, tmp_path)
    assert rc == 2
    assert rows == []


# --- the classification, pinned ------------------------------------------------
# Who waits on each caller's emission, so who can afford what deadline:
#   hook       a live Claude Code turn: every tool call, prompt, reply, turn end
#   background nothing reads the result (and plane_emit_bounded, i.e. every
#              emit_fleet_event, defaults to it)
#   door       the default for explicit shim calls; task/check-in/workstream
#              mutations now commit through their Python operation owners
CLASSES = {
    "bot-vitals.sh": "hook",
    "plane-telegram-in.sh": "hook",
    "plane-telegram-out.sh": "hook",
    "plane-dispatch-in.sh": "hook",
    "plane-rc-relay-out.sh": "hook",
    "vault-git-guard.sh": "hook",
    "credential-echo-guard.sh": "hook",
    "transcript-digest.sh": "background",  # a hook, but at SessionEnd no turn waits
    "keepalive.sh": "background",
    "plane-host-probe.sh": "background",
    "tg-post.sh": "background",
    "briefing-trigger.sh": "background",
    "vault-sync.sh": "background",
}
# Callers that deliberately keep the default: the shim itself, the helpers'
# home, and two harnesses that measure the default.
UNCLASSIFIED = {
    "plane-emit.sh",
    "lib-common.sh",
    "validate-bot-change.sh",
    "send-size-probe.sh",
}
ASSIGN = re.compile(r"^PLANE_EMIT_CLASS=(\w+)", re.M)
EMITS = re.compile(r"plane_emit_events\b|plane-emit\.sh\"|emit_fleet_event\b")


def _code(path: Path) -> str:
    return "\n".join(
        l for l in path.read_text().splitlines() if not l.lstrip().startswith("#")
    )


@pytest.mark.parametrize("script,cls", sorted(CLASSES.items()))
def test_each_caller_declares_its_class_once(script, cls):
    assert ASSIGN.findall((LIB / script).read_text()) == [cls]


def test_every_emitting_caller_is_classified():
    """A new caller must choose: silence would leave it on the default."""
    direct = re.compile(r"plane_emit_events\b|plane-emit\.sh\"")
    callers = {p.name for p in LIB.glob("*.sh") if direct.search(_code(p))}
    assert callers - set(CLASSES) - UNCLASSIFIED == set()


def test_a_direct_shim_call_forwards_the_class():
    """An un-exported variable does not reach a child on its own."""
    for script in CLASSES:
        for line in _code(LIB / script).splitlines():
            if 'plane-emit.sh"' in line:
                assert 'PLANE_EMIT_CLASS="$PLANE_EMIT_CLASS"' in line, (script, line)


def test_every_composed_turn_hook_that_emits_is_a_hook():
    """Derived from the compose config, not a hand list: a hook added to
    system.yaml that records on the plane blocks a turn, so it is a hook."""
    hooks = yaml.safe_load((REPO / "claudlobby" / "system.yaml").read_text())[
        "defaults"
    ]["hooks"]
    turn = {"PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"}
    seen = set()
    for event, entries in hooks.items():
        if event not in turn:
            continue
        for entry in entries:
            # Hooks resolve the script from the activated release at runtime.
            m = re.search(
                r"\$CLAUDLOBBY_NATIVE_DIR/([\w.-]+\.sh)(?:\s|$)", entry["command"]
            )
            if m and EMITS.search(_code(LIB / m.group(1))):
                seen.add(m.group(1))
                assert CLASSES.get(m.group(1)) == "hook", m.group(1)
    assert "bot-vitals.sh" in seen, (
        "the derivation found nothing: it is broken, not clean"
    )
