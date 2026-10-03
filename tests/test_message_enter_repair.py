"""The receipt-gated idle Enter repair the transport defers to its owner (#2105).

A CLI delivery presses Enter once with verification off. When the receiver had
not submitted the message after the receipt wait, its box may still hold it: the
messaging operation owner then looks, and presses one Enter only when the box
holds exactly this message in a pane with no turn running and no menu open; a
second, only after another receipt wait, and only on the same match. Never a
third, never the payload again, and the repair is a Plane fact.

The box is faked where the decision under test is the owner's (how many looks,
which receipt gates them); the native look itself runs against real tmux.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import pytest

from claudlobby import assignment_delivery, message_operations, message_queries, message_transport
from claudlobby.__main__ import main
from claudlobby.message_queries import MessageIdentity, ReceiptObservation
from claudlobby.message_transport import TransportOutcome
from claudlobby.plane.db import db_file
from claudlobby.recording_alerts import ChannelOutcome, RecordingAlertOutcome
from tests.package_fixtures import source_package
from tests.test_activation import cold, tmp_path  # noqa: F401 — short private activation root
from tests.test_releases import installed  # noqa: F401 — cold dependency
from tests.test_task_read_cli import active  # noqa: F401 — active private Plane fixture
from tests.test_message_write_cli import _assigned, _call, _delivery_call, _generated


class _Box:
    """The worker's input box: it holds the message until `needs` Enters reach it.

    press stands in for message_transport.press_held_enter (the native look),
    receipt for message_queries.receipt, which reads what the receiver submitted."""

    def __init__(self, *, needs=1, verdict="text", match="text"):
        self.needs, self.verdict, self.match = needs, verdict, match
        self.enters, self.expects, self.waits = 0, [], []

    @property
    def submitted(self):
        return self.verdict in ("text", "chip") and self.enters >= self.needs

    def press(self, package, destination, *, message_id, expect=None, timeout=15, runner=None):
        self.expects.append(expect)
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
    monkeypatch.setattr(message_queries, "receipt", box.receipt)
    monkeypatch.setattr(message_operations, "_REPAIR_RECHECK_S", 0, raising=False)


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
    monkeypatch.setattr(message_queries, "receipt", box.receipt)
    monkeypatch.setattr(message_operations, "_REPAIR_RECHECK_S", 0, raising=False)
    out = _delivery_call(capsys, root, assigned.assignment_id, "--file", str(body),
                         "--request-id", str(uuid4()), expected=0)
    assert box.enters == 2 and out["data"]["delivery"] == "received"


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
    # visible where an operator looks for it
    assert main(["--root", str(root), "--json", "event", "list", "--type", "delivery_enter_repaired"]) == 0
    listed = json.loads(capsys.readouterr().out)["data"]["items"]
    assert [item["bot"] for item in listed] == ["worker"]


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


@pytest.mark.parametrize(("frame", "expect", "verdict", "keys"), [
    (HELD, None, "text", b"\r"),
    (HELD, "chip#2", "changed", b""),
    (BUSY, None, "busy", b""),
    (MENU, None, "not-held", b""),
    (OTHER, None, "not-shown", b""),
], ids=["held", "held-but-not-the-earlier-match", "busy", "menu", "another-message"])
def test_the_native_look_presses_one_enter_only_on_a_box_holding_this_message(frame, expect, verdict, keys):
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
                        "-s", "worker", f"python3 {stub} {root / 'frame.txt'} {log}"], env=env, check=True)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                shown = subprocess.run([tmux, "-L", socket, "capture-pane", "-p", "-t", "worker"], env=env,
                                       capture_output=True, text=True).stdout
                if "auto mode on" in shown:
                    break
                time.sleep(0.1)
            destination = message_transport.TransportDestination(root, "fleet", socket, "worker", sockets)
            package = replace(source_package(),
                              native=Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts")
            outcome = message_transport.press_held_enter(package, destination, message_id=MSG,
                                                         expect=expect, timeout=10)
            time.sleep(0.5)
            assert (outcome.verdict, outcome.pressed) == (verdict, keys == b"\r"), outcome
            assert log.read_bytes() == keys
        finally:
            subprocess.run([tmux, "-L", socket, "kill-server"], env=env, capture_output=True, timeout=5)
