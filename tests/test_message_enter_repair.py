"""The receipt-gated idle Enter repair the transport defers to its owner (#2105).

A CLI delivery presses Enter once with verification off. When the receiver had
not submitted the message after the receipt wait, its box may still hold it: the
messaging operation owner then looks, and presses one Enter only when the box
holds this message's text (its trailer names it), or one paste chip in a box that
was empty just before the send, in a pane where pane_is_busy sees no running turn
and no menu is open; a second, only after another receipt wait, and only on the
same match. Never a third, never the payload again, and the repair is a Plane
fact.

The box is faked where the decision under test is the owner's (how many looks,
which receipt gates them); the native look itself runs against real tmux.
"""

import json
import shutil
from contextlib import contextmanager
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import pytest

from claudlobby import assignment_delivery, message_operations, message_queries, message_transport
from claudlobby.message_queries import MessageIdentity, ReceiptObservation
from claudlobby.message_transport import TransportOutcome
from claudlobby.plane.db import db_file
from claudlobby.recording_alerts import ChannelOutcome, RecordingAlertOutcome
from tests.package_fixtures import source_package
from tests.plane_fixtures import F as SCENE_FLEET, _scene
from tests.plane_setup import initialize_plane
from tests.test_activation import cold, tmp_path  # noqa: F401 — short private activation root
from tests.test_releases import installed  # noqa: F401 — cold dependency
from tests.test_task_read_cli import active  # noqa: F401 — active private Plane fixture
from tests.test_message_write_cli import _assigned, _call, _delivery_call, _generated
from tests.test_plane_events_door import _events_cmd, _rows


class _Box:
    """The worker's input box: it holds the message until `needs` Enters reach it.

    press stands in for message_transport.press_held_enter (the native look),
    receipt for message_queries.receipt, which reads what the receiver submitted."""

    def __init__(self, *, needs=1, verdict="text", match="text", before="empty"):
        self.needs, self.verdict, self.match, self.before = needs, verdict, match, before
        self.enters, self.expects, self.befores, self.waits = 0, [], [], []

    @property
    def submitted(self):
        return self.verdict in ("text", "chip") and self.enters >= self.needs

    def read_box(self, package, destination, *, timeout=5, runner=None):
        return self.before

    def press(self, package, destination, *, message_id, expect=None, before=None, timeout=15,
              runner=None):
        self.expects.append(expect)
        self.befores.append(before)
        if self.verdict not in ("text", "chip"):
            return SimpleNamespace(verdict=self.verdict, pressed=False, match=None, reason=None)
        if self.submitted:
            return SimpleNamespace(verdict="not-held", pressed=False, match=None, reason=None)
        if expect is not None and expect != self.match:
            return SimpleNamespace(verdict="changed", pressed=False, match=self.match, reason=None)
        self.enters += 1
        return SimpleNamespace(verdict=self.verdict, pressed=True, match=self.match, reason=None)

    def receipt(self, ctx, message_id, *, destination, wait):
        assert destination == "bot:example/worker"
        self.waits.append(wait)
        sender = MessageIdentity(ctx.caller.uid, ctx.caller.alias, ctx.caller_fleet_uid)
        peer = ctx.bots["worker"]
        recipient = MessageIdentity(peer.uid, peer.alias, ctx.fleet_uid)
        if self.submitted:
            return ReceiptObservation(message_id, str(ctx.root), sender, recipient, "received",
                                      "delivered", 0, None, None)
        return ReceiptObservation(message_id, str(ctx.root), sender, recipient, "missing",
                                  "unconfirmed", 8, "timeout", None)


def _held(monkeypatch, box, *, rc=0):
    """The send is submitted (rc 0) or withheld (rc 3, #1236); the box decides the rest."""
    real = message_operations.send_message
    def wrapped(*args, **kwargs):
        def transport(*_, **native):
            status = "submitted" if rc == 0 else "unknown"
            return TransportOutcome(status, "sha256:" + "a" * 64, 99, rc)
        return real(*args, transport=transport,
                    notify=lambda *a, **k: RecordingAlertOutcome(
                        ChannelOutcome("submitted", True), ChannelOutcome("unconfigured", True)),
                    clear=lambda *a, **k: None, **kwargs)
    monkeypatch.setattr(message_operations, "send_message", wrapped)
    monkeypatch.setattr(message_transport, "press_held_enter", box.press, raising=False)
    monkeypatch.setattr(message_transport, "read_box", box.read_box, raising=False)
    monkeypatch.setattr(message_queries, "receipt", box.receipt)


def _send(capsys, root, *, expected):
    return _call(capsys, root, "--to", "worker", "--text", "Private held body",
                 "--request-id", str(uuid4()), expected=expected)


def test_a_held_delivery_gets_one_enter_and_is_received(active, monkeypatch, capsys):
    root, host = active
    _generated(monkeypatch, root, host.release)
    box = _Box(needs=1)
    _held(monkeypatch, box)
    out = _send(capsys, root, expected=0)
    assert box.enters == 1, "the held delivery stayed held: no Enter reached the box"
    assert out["data"]["delivery"] == "received"
    repair = out["data"]["enter_repair"]
    assert (repair["pressed"], repair["match"], len(repair["attempts"])) == (1, "text", 1)
    assert box.expects == [None]


def test_the_recipe_a_second_enter_only_for_the_same_match(active, monkeypatch, capsys):
    # The first Enter can only strip the CR a swallowed Enter left (#1236); the
    # second submits. It is pressed only on the match the first look saw.
    root, host = active
    _generated(monkeypatch, root, host.release)
    box = _Box(needs=2, verdict="chip", match="chip#1")
    _held(monkeypatch, box)
    out = _send(capsys, root, expected=0)
    assert box.enters == 2 and box.expects == [None, "chip#1"]
    # The receipt wait between the looks is the owner's 12 s, asked for after each press.
    assert box.waits[1:] == [message_operations._REPAIR_RECHECK_S] * 2 == [12, 12]
    assert box.befores == ["empty", "empty"]
    assert out["data"]["delivery"] == "received"
    assert (out["data"]["enter_repair"]["pressed"], out["data"]["enter_repair"]["match"]) == (2, "chip")


def test_never_a_third_enter(active, monkeypatch, capsys):
    root, host = active
    _generated(monkeypatch, root, host.release)
    box = _Box(needs=99)
    _held(monkeypatch, box)
    out = _send(capsys, root, expected=5)
    assert box.enters == 2 and len(box.expects) == 2
    assert out["error"]["code"] == "delivery_unknown"
    assert out["data"]["enter_repair"]["pressed"] == 2


@pytest.mark.parametrize("verdict", ["busy", "not-held", "glued", "not-shown", "chips"])
def test_no_enter_when_the_box_is_busy_menued_or_holds_other_text(active, monkeypatch, capsys, verdict):
    root, host = active
    _generated(monkeypatch, root, host.release)
    box = _Box(verdict=verdict)
    _held(monkeypatch, box)
    out = _send(capsys, root, expected=5)
    assert box.enters == 0 and len(box.expects) == 1
    repair = out["data"]["enter_repair"]
    assert repair["pressed"] == 0 and repair["attempts"][0]["verdict"] == verdict
    assert repair["recording"] == "not_requested"


def test_no_look_while_the_receipt_proof_is_still_queued(active, monkeypatch, capsys):
    root, host = active
    _generated(monkeypatch, root, host.release)
    box = _Box(needs=1)
    _held(monkeypatch, box)
    def queued(ctx, message_id, *, destination, wait):
        observed = box.receipt(ctx, message_id, destination=destination, wait=wait)
        return replace(observed, receipt_observation="unavailable")
    monkeypatch.setattr(message_queries, "receipt", queued)
    out = _send(capsys, root, expected=5)
    assert box.expects == [] and "enter_repair" not in out["data"]


def test_a_withheld_enter_rc3_is_repaired_once_the_box_holds_it(active, monkeypatch, capsys):
    # #1236's rc 3: the box never showed the payload, so the transport withheld
    # its Enter. On main the command stops there; the text can still land.
    root, host = active
    _generated(monkeypatch, root, host.release)
    box = _Box(needs=1)
    _held(monkeypatch, box, rc=3)
    out = _send(capsys, root, expected=0)
    assert box.enters == 1 and out["data"]["delivery"] == "received"


def test_an_assignment_delivery_held_in_the_box_is_repaired(active, monkeypatch, capsys, tmp_path):  # noqa: F811
    root, host = active
    _generated(monkeypatch, root, host.release)
    ctx, task, assigned = _assigned(root)
    body = tmp_path / "assignment.txt"
    body.write_text("Private assignment delivery", encoding="utf-8")
    box = _Box(needs=2)
    real = assignment_delivery.deliver
    def injected(*args, **kwargs):
        def transport(*_, **native):
            return TransportOutcome("submitted", "sha256:" + "a" * 64, 99, 0)
        return real(*args, transport=transport, **kwargs)
    monkeypatch.setattr(assignment_delivery, "deliver", injected)
    monkeypatch.setattr(message_transport, "press_held_enter", box.press, raising=False)
    monkeypatch.setattr(message_transport, "read_box", box.read_box, raising=False)
    monkeypatch.setattr(message_queries, "receipt", box.receipt)
    out = _delivery_call(capsys, root, assigned.assignment_id, "--file", str(body),
                         "--request-id", str(uuid4()), expected=0)
    assert box.enters == 2 and out["data"]["delivery"] == "received"


def test_the_box_read_before_the_send_reaches_every_look(active, monkeypatch, capsys):
    """Option (c): the owner reads the recipient's box once, just before its send,
    and gives that read to each look, which presses a chip only on an empty one."""
    root, host = active
    _generated(monkeypatch, root, host.release)
    box = _Box(needs=99, before="held")
    _held(monkeypatch, box)
    _send(capsys, root, expected=5)
    assert box.befores == ["held", "held"]


def test_a_press_is_recorded_even_when_the_receipt_read_after_it_raises():
    """vera's item 2: a receipt read that raises after the press (her probe: a
    locked database) used to leave the press unrecorded. It is recorded, then the
    error goes on up."""
    recorded = []
    first = SimpleNamespace(receipt_observation="missing")

    def press(package, destination, *, message_id, expect=None, before=None):
        return SimpleNamespace(verdict="text", pressed=True, match="text", reason=None)

    def observe(wait):
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        message_operations.repair_held_delivery(
            SimpleNamespace(peer_destination=None), None, "msg_" + "e" * 32, observe=observe,
            first=first, press=press, record=lambda *args: recorded.append(args) or "committed")
    assert len(recorded) == 1
    _route, message_id, attempts, observed = recorded[0]
    assert message_id == "msg_" + "e" * 32 and [a.pressed for a in attempts] == [True]
    assert observed is first


def test_the_repair_is_a_fleet_event_with_its_match_and_every_look(active, monkeypatch, capsys):
    root, host = active
    _generated(monkeypatch, root, host.release)
    box = _Box(needs=2, verdict="chip", match="chip#3")
    _held(monkeypatch, box)
    out = _send(capsys, root, expected=0)
    assert out["data"]["enter_repair"]["recording"] == "committed"
    with sqlite3.connect(db_file(root)) as conn:
        rows = conn.execute("SELECT subject_alias, detail FROM events WHERE event = 'delivery_enter_repaired'"
                            ).fetchall()
    assert len(rows) == 1
    alias, detail = rows[0]
    data = json.loads(detail)["data"]
    assert alias == "bot:example/worker"
    assert (data["msg_id"], data["match"], data["chip"], data["enters"]) == \
        (out["data"]["message_id"], "chip", "chip#3", 2)
    assert [look["pressed"] for look in data["attempts"]] == [True, True]
    assert data["receipt"] == "received"


def test_the_repair_fact_is_listed_where_an_operator_looks(tmp_path):
    """`event list --type delivery_enter_repaired` shows the fact on the recipient,
    with its match and both looks. The door reads the plane through the active
    release's matcher, which the private plane scene installs and the activation
    fixture above does not, so the door is asked here."""
    root, _paths, _, _ = _scene(tmp_path)
    initialize_plane(root)
    route = SimpleNamespace(peer_destination=SimpleNamespace(fleet=SCENE_FLEET),
                            peer=SimpleNamespace(alias=f"bot:{SCENE_FLEET}/w1"),
                            caller=SimpleNamespace(alias=f"bot:{SCENE_FLEET}/w2"),
                            selected=SimpleNamespace(paths=SimpleNamespace(root=root)))
    looks = tuple(message_operations.EnterRepairAttempt(at, "chip", True, "chip#3", None)
                  for at in ("2026-10-03T00:00:00+00:00", "2026-10-03T00:00:12+00:00"))
    received = SimpleNamespace(receipt_observation="received")
    message_id = "msg_" + "d" * 32
    assert message_operations._record_enter_repair(route, message_id, looks, received) == "committed"
    items = _rows(_events_cmd(root, "--json", "--type", "delivery_enter_repaired"))
    assert [(item["bot"], item["type"]) for item in items] == [("w1", "delivery_enter_repaired")]
    fact = items[0]["data"]
    assert (fact["msg_id"], fact["match"], fact["chip"], fact["enters"], fact["receipt"]) == \
        (message_id, "chip", "chip#3", 2, "received")


# --- the native look, on real tmux ---------------------------------------------------

MSG = "msg_" + "c" * 32
NBSP = " "
RULE = "─" * 60


def _frame(*box_lines, above=("● Done.",)):
    first, *rest = box_lines
    return "\n".join([*above, "", RULE, f"❯{NBSP}{first}", *(f"  {line}" for line in rest),
                      RULE, "  ⏵⏵ auto mode on (shift+tab to cycle)"]) + "\n"


HELD = _frame(f"[Claudlobby ordinary message] Message: {MSG} From: bot:f/dara", f"⟦plane:{MSG}⟧")
BUSY = _frame(f"[Claudlobby ordinary message] Message: {MSG} body", f"⟦plane:{MSG}⟧",
              above=("✻ Cogitating… (12s · esc to interrupt)",))
MENU = _frame("1. Yes, try it", "2. Not now", "Enter to confirm · Esc to cancel")
OTHER = _frame("[Claudlobby ordinary message] Message: msg_" + "d" * 32 + " body", "⟦plane:msg_" + "d" * 32 + "⟧")
# claude 2.1.285 draws a running turn's activity line and, mostly, no interrupt hint.
LIVE_BUSY = _frame(f"[Claudlobby ordinary message] Message: {MSG} body", f"⟦plane:{MSG}⟧",
                   above=("✻ Transmogrifying…",))
CHIP3 = _frame("[Pasted text #3 +2 lines]")
EMPTY = _frame("")


@contextmanager
def _stub_pane(frame, *, session="worker"):
    """A real tmux pane drawing <frame> through the held-box stub, which logs every
    key it receives. Yields a destination maker (a session name), the key log and
    the package whose native helper is this checkout's."""
    tmux = shutil.which("tmux")
    assert tmux, "the native look requires tmux"
    stub = Path(__file__).resolve().parent / "fixtures" / "held-box-stub.py"
    with TemporaryDirectory(prefix="cl-rep-", dir="/tmp") as scratch:
        root = Path(scratch)
        sockets, home = root / "s", root / "h"
        sockets.mkdir()
        home.mkdir()
        (root / "frame.txt").write_text(frame, encoding="utf-8")
        log = root / "keys.log"
        log.write_bytes(b"")
        env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "HOME": str(home),
               "TMUX_TMPDIR": str(sockets), "TMPDIR": str(sockets), "LANG": "C.UTF-8"}
        socket = "private-held"
        subprocess.run([tmux, "-L", socket, "-f", "/dev/null", "new-session", "-d", "-x", "200", "-y", "40",
                        "-s", session, f"python3 {stub} {root / 'frame.txt'} {log}"], env=env, check=True)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                shown = subprocess.run([tmux, "-L", socket, "capture-pane", "-p", "-t", session], env=env,
                                       capture_output=True, text=True).stdout
                if "auto mode on" in shown:
                    break
                time.sleep(0.1)
            package = replace(source_package(),
                              native=Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts")
            yield ((lambda name=session: message_transport.TransportDestination(
                root, "fleet", socket, name, sockets)), log, package)
        finally:
            subprocess.run([tmux, "-L", socket, "kill-server"], env=env, capture_output=True, timeout=5)


@pytest.mark.parametrize(("frame", "expect", "verdict", "keys"), [
    (HELD, None, "text", b"\r"),
    (HELD, "chip#2", "changed", b""),
    (BUSY, None, "busy", b""),
    (MENU, None, "not-held", b""),
    (OTHER, None, "not-shown", b""),
    (LIVE_BUSY, None, "busy", b""),
], ids=["held", "held-but-not-the-earlier-match", "busy", "menu", "another-message",
        "busy-live-frame-no-hint"])
def test_the_native_look_presses_one_enter_only_on_a_box_holding_this_message(frame, expect, verdict, keys):
    with _stub_pane(frame) as (destination, log, package):
        outcome = message_transport.press_held_enter(package, destination(), message_id=MSG,
                                                     expect=expect, timeout=10)
        time.sleep(0.5)
        assert (outcome.verdict, outcome.pressed) == (verdict, keys == b"\r"), outcome
        assert log.read_bytes() == keys


_HOLD_LOCK = """import fcntl, os, sys, time
f = open(sys.argv[1], "a+")
fcntl.flock(f, fcntl.LOCK_EX)
f.seek(0)
f.truncate()
f.write("pid=%d since=test bot=external-holder door=test what=payload\\n" % os.getpid())
f.flush()
open(sys.argv[2], "w").close()
time.sleep(float(sys.argv[3]))
"""


def test_the_native_look_waits_for_the_recipients_send_lock():
    """#2036: the owner's repair Enter is a keystroke like any other. While another
    sender holds the pane's send lock, the look presses nothing; once the lock is
    free it looks and presses under it, so its Enter cannot land inside another
    sender's chunks. The lock file is the shipped helper's, for the bare session."""
    with _stub_pane(HELD) as (destination, log, package):
        dest = destination()
        lock = subprocess.run(
            ["bash", "-c", '. "$1/lib-common.sh" >/dev/null 2>&1; _pane_send_lock_file "$2" "$3"; '
             'printf %s "${_PANE_SEND_LOCK_FILE:-}"', "lock-file", str(package.native), dest.socket,
             dest.session],
            env={"PATH": "/usr/bin:/bin", "CLAUDLOBBY_ROOT": str(dest.root), "LC_ALL": "C"},
            capture_output=True, text=True, timeout=10).stdout
        assert lock.startswith(str(dest.root)), f"no lock file named under the destination root: {lock!r}"
        Path(lock).parent.mkdir(parents=True, exist_ok=True)
        held = Path(lock).parent / "held"
        holder = subprocess.Popen([sys.executable, "-c", _HOLD_LOCK, lock, str(held), "30"])
        try:
            deadline = time.monotonic() + 10
            while not held.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert held.exists(), "the holder never took the lock"
            outcome = message_transport.press_held_enter(package, dest, message_id=MSG, timeout=15)
            time.sleep(0.5)
            assert (outcome.verdict, outcome.pressed) == ("unknown", False), outcome
            assert log.read_bytes() == b""
        finally:
            holder.kill()
            holder.wait(timeout=5)
        outcome = message_transport.press_held_enter(package, dest, message_id=MSG, timeout=15)
        time.sleep(0.5)
        assert (outcome.verdict, outcome.pressed) == ("text", True), outcome
        assert log.read_bytes() == b"\r"


def test_the_native_look_names_the_chip_it_presses():
    """vera's (a): the match names the chip's own number, the one a second look must see."""
    with _stub_pane(CHIP3) as (destination, log, package):
        outcome = message_transport.press_held_enter(package, destination(), message_id=MSG,
                                                     before="empty", timeout=10)
        time.sleep(0.5)
        assert (outcome.verdict, outcome.pressed, outcome.match) == ("chip", True, "chip#3"), outcome
        assert log.read_bytes() == b"\r"


@pytest.mark.parametrize("before", ["held", "unknown", None])
def test_a_lone_chip_is_pressed_only_when_the_box_was_empty_before_the_send(before):
    """Option (c): a chip carries no message id, so the look presses one only when
    the owner's read just before its send found the box empty."""
    with _stub_pane(CHIP3) as (destination, log, package):
        outcome = message_transport.press_held_enter(package, destination(), message_id=MSG,
                                                     before=before, timeout=10)
        time.sleep(0.5)
        assert (outcome.verdict, outcome.pressed) == ("chip-unproven", False), outcome
        assert log.read_bytes() == b""


def test_the_native_look_reads_only_the_exact_session():
    """vera's (b): asked for `work` while only `worker` exists, the look reads and
    presses nothing; a target matched by prefix would read the other pane."""
    with _stub_pane(HELD, session="worker") as (destination, log, package):
        outcome = message_transport.press_held_enter(package, destination("work"), message_id=MSG,
                                                     timeout=10)
        time.sleep(0.5)
        assert (outcome.verdict, outcome.pressed) == ("unknown", False), outcome
        assert log.read_bytes() == b""


@pytest.mark.parametrize(("frame", "state"), [(HELD, "held"), (EMPTY, "empty")], ids=["held", "empty"])
def test_read_box_says_whether_the_box_holds_text_before_a_send(frame, state):
    with _stub_pane(frame) as (destination, log, package):
        assert message_transport.read_box(package, destination(), timeout=10) == state
        assert message_transport.read_box(package, destination("absent"), timeout=10) == "unknown"
        assert log.read_bytes() == b""


def test_the_box_is_read_before_the_send_and_handed_to_every_look_on_message_send(
    active, monkeypatch, capsys
):
    """Option (c)'s ORDER and hand-over on the message path: the stub box answers the read, the wrapped send marks the send."""
    root, host = active
    _generated(monkeypatch, root, host.release)
    events = []
    box = _Box(needs=2, verdict="chip", match="chip#1", before="empty")
    real_read = box.read_box

    def read_box(*args, **kwargs):
        events.append("read")
        return real_read(*args, **kwargs)

    box.read_box = read_box
    _held(monkeypatch, box)
    inner = message_operations.send_message

    def ordered(*args, **kwargs):
        events.append("send")
        return inner(*args, **kwargs)

    monkeypatch.setattr(message_operations, "send_message", ordered)
    out = _send(capsys, root, expected=0)
    assert events == ["read", "send"], "the box is read once, before the send"
    assert box.befores == ["empty", "empty"] and out["data"]["delivery"] == "received"


def test_the_box_is_read_before_the_send_and_handed_to_every_look_on_assignment_delivery(
    active, monkeypatch, capsys, tmp_path
):  # noqa: F811
    """The same pin on the assignment path, the one that carries long bodies (the chips): the suite's other test there never reads what the look was given."""
    root, host = active
    _generated(monkeypatch, root, host.release)
    ctx, task, assigned = _assigned(root)
    body = tmp_path / "assignment.txt"
    body.write_text("Private assignment delivery", encoding="utf-8")
    events = []
    box = _Box(needs=2, verdict="chip", match="chip#1", before="empty")
    real_read = box.read_box

    def read_box(*args, **kwargs):
        events.append("read")
        return real_read(*args, **kwargs)

    box.read_box = read_box
    real = assignment_delivery.deliver

    def injected(*args, **kwargs):
        events.append("send")

        def transport(*_, **native):
            return TransportOutcome("submitted", "sha256:" + "a" * 64, 99, 0)

        return real(*args, transport=transport, **kwargs)

    monkeypatch.setattr(assignment_delivery, "deliver", injected)
    monkeypatch.setattr(message_transport, "press_held_enter", box.press, raising=False)
    monkeypatch.setattr(message_transport, "read_box", box.read_box, raising=False)
    monkeypatch.setattr(message_queries, "receipt", box.receipt)
    out = _delivery_call(
        capsys,
        root,
        assigned.assignment_id,
        "--file",
        str(body),
        "--request-id",
        str(uuid4()),
        expected=0,
    )
    assert events == ["read", "send"], "the box is read once, before the send"
    assert box.befores == ["empty", "empty"] and out["data"]["delivery"] == "received"


def test_read_box_answers_unknown_on_every_failure(tmp_path):  # noqa: F811
    """read_box is the proof a chip press rests on: a timeout, a launch error, a failed observation and a garbled answer must all read unknown, never empty."""
    package = replace(
        source_package(),
        native=Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts",
    )
    destination = message_transport.TransportDestination(
        tmp_path, "fleet", "private-none", "worker", tmp_path
    )

    def raising(exc):
        def runner(*args, **kwargs):
            raise exc

        return runner

    for failure in (
        subprocess.TimeoutExpired("native", 5),
        OSError("boom"),
        message_transport._StartedFailure("lost"),
    ):
        assert (
            message_transport.read_box(package, destination, runner=raising(failure))
            == "unknown"
        ), failure
    for stdout in (b"box-v1\tmaybe\n", b"", b"empty\n", b"box-v1\tempty"):
        runner = lambda *args, _out=stdout, **kwargs: SimpleNamespace(stdout=_out)
        assert (
            message_transport.read_box(package, destination, runner=runner) == "unknown"
        ), stdout
    assert (
        message_transport.read_box(
            package,
            destination,
            runner=lambda *a, **k: SimpleNamespace(stdout=b"box-v1\tempty\n"),
        )
        == "empty"
    )
